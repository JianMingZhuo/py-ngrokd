#!/usr/bin/env python
# -*- coding: UTF-8 -*-
import asyncio
import ssl
import json
from ngrok.err import ERR_SUCCESS, ERR_UNKNOWN_REQUEST, ERR_UNSUPPORTED_PROTOCOL, ERR_URL_EXISTED, ERR_CLOSE_SOCKET, \
    get_err_msg
from ngrok.logger import logger
from ngrok.global_cache import GLOBAL_CACHE
from ngrok.util import tolen, generate_auth_resp, generate_new_tunnel, generate_pong, generate_req_proxy, \
    generate_start_proxy
from ngrok.config import DEFAULT_BUF_SIZE
from ngrok.controler.ngrok_controller import NgrokController


class NgrokHandler:

    def __init__(self, conn, loop):

        self.conn = conn
        self.loop = loop
        self.fd = conn.fileno()

        # 从conn中接收到的二进制数据
        self.binary_data = None

        # 准备返回给客户端的响应队列
        self.resp_list = []
        # 正在返回给客户端的响应
        self.writing_resp = None

        self.client_id = None

        # 表示是否是proxy连接
        self.is_proxy = False

        # 表示是否已经开始proxy连接
        self.proxy_started = False

        # 浏览器客户端的网络地址
        self.browser_addr = None
        # 代理的url
        self.url = None

        # 用来与 相关联的 http socket 在消息队列(Queue, redis...)中进行通信的标识
        self.communicate_identify = None

        # 用来发送给http_handler的控制信息的消息队列, 仅proxy连接使用
        self.control_http_queue = None

        # 用来接受来之http_handler的控制信息的消息队列, 仅proxy连接使用
        self.control_proxy_queue = None

    def read_handler(self):
        """
        对外的read回调, 将处理read扔给协程
        :return:
        """
        asyncio.ensure_future(self.__read_handler(), loop=self.loop)

    async def __read_handler(self):
        """
        协程，真正处理read回调。
        :return:
        """
        try:
            data = self.conn.recv(DEFAULT_BUF_SIZE)
        except ssl.SSLWantReadError:
            return

        if not data:
            asyncio.ensure_future(self.process_error(), loop=self.loop)
        else:

            if self.is_proxy is True:
                # 如果是Proxy连接，将数据直接传给http handler
                await self.insert_data_to_http_resp_queue(data)
            else:

                request_size = tolen(data[:8])

                if request_size > len(data[8:]):
                    # 请求没接收全，继续接受

                    self.binary_data = bytearray(data)

                    # 移除旧的read回调
                    self.loop.remove_reader(self.fd)
                    # 因为请求未接受完, 使用继续接收的read回调
                    self.loop.add_reader(self.fd, self.continue_read_handler)
                elif request_size == len(data[8:]):
                    # 请求接受全

                    request_data = data[8:]
                    logger.debug("receive control request: %s", request_data)

                    await self.process_request(request_data)

                    self.loop.remove_reader(self.fd)
                else:

                    request_data = data[8:request_size + 8]

                    await self.process_request(request_data)

                    # 有TCP粘包
                    self.binary_data = bytearray(data[request_size + 8:])

    def continue_read_handler(self):
        """
        处理之前请求过大没接收完的请求。扔给协程处理
        :return:
        """
        asyncio.ensure_future(self.__continue_read_handler(), loop=self.loop)

    async def __continue_read_handler(self):
        """
        处理read回调。用来处理请求过大没有一次接收完的。
        :return:
        """
        try:
            data = self.conn.recv(DEFAULT_BUF_SIZE)

        except ssl.SSLWantReadError as ex:
            logger.debug("SSLWantReadError")
            return

        if not data:
            self.loop.remove_reader(self.conn.fileno())
            self.conn.close()
        else:
            request_size = tolen(self.binary_data[:8])
            try:
                self.binary_data.extend(data)
            except Exception as ex:
                logger.exception("test:", exc_info=ex)
            if request_size > len(self.binary_data[8:]):
                # 请求没接收全，继续接受
                pass
            elif request_size < len(self.binary_data[8:]):
                # 请求的大小，小于收到的大小，有TCP粘包

                # 获取本次请求
                request_data = self.binary_data[8: 8 + request_size]

                logger.debug("receive control request: %s", request_data)

                await self.process_request(request_data)

                # 移除已处理请求的数据
                self.binary_data = self.binary_data[8 + request_size:]

                # 移除继续读的read回调
                self.loop.remove_reader(self.fd)
            else:
                # 请求接受全
                request_data = self.binary_data[8:]
                logger.debug("receive control request: %s", request_data)

                await self.process_request(request_data)

                self.binary_data = None

                # 移除继续读的read回调
                self.loop.remove_reader(self.fd)

    def write_handler(self):
        """

        :return:
        """
        asyncio.ensure_future(self.__write_handler(), loop=self.loop)

    async def __write_handler(self):
        """
        处理写回调。
        :return:
        """

        if len(self.resp_list) == 0 and self.writing_resp is None:
            self.loop.remove_writer(self.fd)
            asyncio.ensure_future(self.process_error(), loop=self.loop)
            return

        try:

            if self.writing_resp is None:
                self.writing_resp = self.resp_list[0]
                self.resp_list = self.resp_list[1:]

            sent_bytes = self.conn.send(self.writing_resp)
            if sent_bytes < len(self.writing_resp):
                self.writing_resp = self.writing_resp[sent_bytes:]
            else:
                self.writing_resp = None
                if len(self.resp_list) == 0:
                    self.loop.remove_writer(self.fd)
                    self.loop.add_reader(self.fd, self.read_handler)

        except ssl.SSLWantReadError as ex:
            logger.debug("SSLWantReadError")
            return

    async def process_request(self, request_data):
        """
        处理读取到的请求命令
        :param request_data: 读取到的请求数据，会在本函数中转为json格式
        :return:
        """
        try:
            request = json.loads(str(request_data, 'utf-8'))
        except Exception as ex:
            logger.exception("Exception in process_request, load request:", exc_info=ex)
            asyncio.ensure_future(self.process_error(), loop=self.loop)
            return

        req_type = request.get('Type', None)

        if req_type == 'Auth':
            err, msg, resp = self.auth_process(request)
        elif req_type == 'ReqTunnel':
            err, msg, resp = self.req_tunnel_process(request)
        elif req_type == 'RegProxy':
            err, msg, resp = await self.reg_proxy_process(request)
        elif req_type == 'Ping':
            err, msg, resp = self.ping_process()
        else:
            # unknown req type, close this connection
            err, msg, resp = ERR_UNKNOWN_REQUEST, get_err_msg(ERR_UNKNOWN_REQUEST), None

        if err in (ERR_UNKNOWN_REQUEST, ERR_CLOSE_SOCKET):
            asyncio.ensure_future(self.process_error(), loop=self.loop)
        elif err == ERR_SUCCESS:
            self.resp_list.append(resp)

            if req_type == 'RegProxy':
                # self.http_start_proxy()
                http_req = await self.get_http_req_from_queue()
                self.resp_list.append(http_req)

            self.loop.remove_reader(self.fd)
            self.loop.add_writer(self.fd, self.write_handler)

    def auth_process(self, request):
        """
        process auth
        :param request:
        :return: (err_code, msg, binary response data)
        """
        user = request['Payload'].get('User')
        pwd = request['Payload'].get('Password')
        version = request['Payload'].get('Version')
        mm_version = request['Payload'].get('MmVersion')
        os_type = request['Payload'].get('OS')
        arch = request['Payload'].get('Arch')

        err, msg, client_id = NgrokController.auth(user, pwd, version, mm_version, os_type, arch)
        logger.debug('auth process: err[%d], msg[%s], client_id[%s]', err, msg, client_id)

        if err != ERR_SUCCESS:
            resp = generate_auth_resp(error=msg)
        else:
            self.client_id = client_id
            GLOBAL_CACHE.add_client_id(client_id)

            # 准备接收 req_proxy 的命令
            asyncio.ensure_future(self.waiting_send_req_proxy(), loop=self.loop)

            resp = generate_auth_resp(client_id=client_id)

        return err, msg, resp

    def req_tunnel_process(self, request):
        """
        Process ReqTunnel request
        :param request:
        :return: (err_code, msg, binary response data)
        """

        if self.client_id is None:
            # 没有登录，调用req_tunnel，不规范或者恶意的客户端，关闭连接
            err = ERR_CLOSE_SOCKET
            msg = get_err_msg(ERR_CLOSE_SOCKET)
            return err, msg, None

        req_id = request['Payload'].get('ReqId')
        protocol = request['Payload'].get('Protocol')

        if protocol in ('http', 'https'):
            hostname = request['Payload'].get('Hostname')
            subdomain = request['Payload'].get('Subdomain')
            http_auth = request['Payload'].get('HttpAuth')

            err, msg, url = NgrokController.req_tunnel_http(req_id, protocol, hostname, subdomain, http_auth)

            if err != ERR_SUCCESS:
                return err, msg, generate_new_tunnel(msg)

            if url in GLOBAL_CACHE.HOSTS:
                err = ERR_URL_EXISTED
                msg = get_err_msg(ERR_URL_EXISTED)
                return err, msg, generate_new_tunnel(msg)

            GLOBAL_CACHE.add_host(url, self.fd, self.client_id, self.prepare_send_req_proxy)
            GLOBAL_CACHE.add_tunnel(self.client_id, protocol, url)

            return err, msg, generate_new_tunnel(req_id=req_id, url=url, protocol=protocol)
        elif protocol == 'tcp':
            # TODO: Fixed me ! TCP not support!!
            remote_port = request['Payload'].get('RemotePort')

            err = ERR_UNSUPPORTED_PROTOCOL
            msg = get_err_msg(ERR_UNSUPPORTED_PROTOCOL)

            return err, msg, generate_new_tunnel(msg)

    async def reg_proxy_process(self, request):
        """
        处理reg_proxy请求。一定不能在控制连接（指的是接收其他请求的连接）中接收到此请求。
        收到reg_proxy，表示此连接是用来做代理的连接。
        一般流程是，服务器发送req_proxy请求到客户端，客户端收到后，另起一个连接，并发送reg_proxy,
        请求中的ClientId表示，该代理连接属于哪个客户端。
        :param request:
        :return:
        """
        err = ERR_CLOSE_SOCKET
        msg = get_err_msg(ERR_CLOSE_SOCKET)
        if self.client_id is not None:
            # 该连接已经存在client_id，可能是不规范或者恶意的客户端，关闭连接
            return err, msg, None

        client_id = request['Payload'].get('ClientId')

        # 请求中client_id为None，可能是不规范或者恶意的客户端，关闭连接
        if client_id is None or client_id == "":
            return err, msg, None

        err, msg, resp = NgrokController.reg_proxy(client_id)

        if err == ERR_SUCCESS:
            self.is_proxy = True
            self.client_id = client_id

            url_and_addr = await GLOBAL_CACHE.PROXY_URL_ADDR_LIST[self.client_id].get()
            if url_and_addr == 'close':
                asyncio.ensure_future(self.process_error(), loop=self.loop)
                return

            # 获取 communicate_identify
            # 使用 communicate_identify 在消息队列(Queue, redis..)中进行交换数据
            self.url, self.browser_addr, self.communicate_identify = (url_and_addr['url'], url_and_addr['addr'],
                                                                      url_and_addr['communicate_identify'])

            resp = generate_start_proxy(self.url, self.browser_addr)

            queue_map = GLOBAL_CACHE.HTTP_COMMU_QUEUE_MAP.get(self.communicate_identify)
            if queue_map:
                self.control_http_queue = queue_map.get('control_http_queue')
                self.control_proxy_queue = queue_map.get('control_proxy_queue')

                # 添加处理proxy对应的http连接意外断开的处理事件
                asyncio.ensure_future(self.close_by_http(), loop=self.loop)
            else:
                asyncio.ensure_future(self.process_error(), loop=self.loop)

        return err, msg, resp

    def ping_process(self):
        """
        处理ping请求.
        :return:
        """
        if self.client_id is None:
            # 应该登录后，在发送ping保持连接。不规范或者恶意的客户端，关闭连接
            err = ERR_CLOSE_SOCKET
            msg = get_err_msg(ERR_CLOSE_SOCKET)
            return err, msg, None
        else:
            err = ERR_SUCCESS
            msg = get_err_msg(ERR_SUCCESS)
            return err, msg, generate_pong()

    async def process_error(self):
        """
        处理错误，关闭客户端连接，移除所有事件监听。比如：解析命令出错等
        :return:
        """
        try:
            self.loop.remove_reader(self.fd)
            self.loop.remove_writer(self.fd)

            # GLOBAL_CACHE.pop_client_id(self.client_id)

            if not self.is_proxy:
                tunnels = GLOBAL_CACHE.pop_tunnel(self.client_id)

                if self.client_id in GLOBAL_CACHE.SEND_REQ_PROXY_LIST:
                    asyncio.ensure_future(GLOBAL_CACHE.SEND_REQ_PROXY_LIST[self.client_id].put('close'), loop=self.loop)

                if tunnels is not None:
                    for url in tunnels['http']:
                        GLOBAL_CACHE.pop_host(url)

                    for url in tunnels['https']:
                        GLOBAL_CACHE.pop_host(url)

                if self.client_id in GLOBAL_CACHE.SEND_REQ_PROXY_LIST:
                    send_req_proxy_queue = GLOBAL_CACHE.SEND_REQ_PROXY_LIST.pop(self.client_id)
                    await send_req_proxy_queue.put('close')

                if self.client_id in GLOBAL_CACHE.PROXY_URL_ADDR_LIST:
                    queue = GLOBAL_CACHE.PROXY_URL_ADDR_LIST.pop(self.client_id)

                    if queue is not None:
                        queue.put('close')
            else:
                # 检查queue是否存在并尝试通知http连接关闭
                if self.control_http_queue:
                    await self.control_http_queue.put('close')

                GLOBAL_CACHE.del_http_commu_queue_map(self.communicate_identify)

            self.conn.close()
        except Exception as ex:
            logger.exception("Exception in process error:", exc_info=ex)

    async def waiting_send_req_proxy(self):
        """
        等待发送req_proxy命令给客户端
        :return:
        """

        while True:
            queue = GLOBAL_CACHE.SEND_REQ_PROXY_LIST[self.client_id]
            value = await queue.get()

            if value == 'close':
                break
            else:
                asyncio.ensure_future(self.prepare_send_req_proxy(), loop=self.loop)

    async def prepare_send_req_proxy(self):
        """
        协程函数，让http/https handler调用，将一个req proxy放到发送队列头部，并设置add_writer，发送给client。
        :return:
        """

        # Remove the event listener
        self.loop.remove_reader(self.fd)
        self.loop.remove_writer(self.fd)

        req_proxy = generate_req_proxy()

        self.resp_list.insert(0, req_proxy)

        self.loop.add_writer(self.fd, self.write_handler)

    async def insert_data_to_http_resp_queue(self, resp_data):
        """
        将客户端返回的 http response 插入到对应的消息队列（Queue, redis...）中，等待http_handler返回给浏览器
        :param resp_data:
        :return:
        """
        queue_map = GLOBAL_CACHE.HTTP_COMMU_QUEUE_MAP[self.communicate_identify]
        if queue_map:
            http_resp_queue = queue_map.get('http_resp_queue')
            if http_resp_queue:
                await http_resp_queue.put(resp_data)

    async def get_http_req_from_queue(self):
        """
        从消息队列中(Queue, redis...)获取http request
        :return:
        """
        queue_map = GLOBAL_CACHE.HTTP_COMMU_QUEUE_MAP[self.communicate_identify]
        if queue_map:
            http_req_queue = queue_map.get('http_req_queue')
            if http_req_queue:
                return await http_req_queue.get()

    async def close_by_http(self):
        """
        通过 control_ngrok_queue 获取 close 的消息，接收到之后关闭连接。
        当连接是proxy的时候，如果对应的http断开，则将该proxy连接也断开
        :return:
        """

        if self.control_proxy_queue:
            signal = await self.control_proxy_queue.get()
            if signal == 'close':
                asyncio.ensure_future(self.process_error(), loop=self.loop)