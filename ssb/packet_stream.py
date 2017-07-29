import logging
import struct
from asyncio import Queue
from enum import Enum
from time import time

from secret_handshake import SHSClient, SHSServer

import simplejson


logger = logging.getLogger('packet_stream')


class PSMessageType(Enum):
    BUFFER = 0
    TEXT = 1
    JSON = 2


class PSStreamHandler(object):
    def __init__(self, req):
        super(PSStreamHandler).__init__()
        self.req = req
        self.queue = Queue()

    async def process(self, msg):
        await self.queue.put(msg)

    async def stop(self):
        await self.queue.put(None)

    async def __aiter__(self):
        while True:
            elem = await self.queue.get()
            if not elem:
                return
            yield elem


class PSMessage(object):

    @classmethod
    def from_header_body(cls, header, body):
        flags, length, req = struct.unpack('>BIi', header)
        type_ = PSMessageType(flags & 0x03)

        if type_ == PSMessageType.TEXT:
            body = body.decode('utf-8')
        elif type_ == PSMessageType.JSON:
            body = simplejson.loads(body)

        return cls(type_, body, bool(flags & 0x08), bool(flags & 0x04), req=req)

    @property
    def data(self):
        if self.type == PSMessageType.TEXT:
            return self.body.encode('utf-8')
        elif self.type == PSMessageType.JSON:
            return simplejson.dumps(self.body)

    def __init__(self, type_, body, stream, end_err, req=None):
        self.stream = stream
        self.end_err = end_err
        self.type = type_
        self.body = body
        self.req = req

    def __repr__(self):
        return '<PSMessage ({}): {}{} {}{}>'.format(self.type.name, self.body,
                                                    '' if self.req is None else ' [{}]'.format(self.req),
                                                    '~' if self.stream else '', '!' if self.end_err else '')


class PSConnection(object):
    def __init__(self):
        self._event_map = {}
        self.req_counter = 1

    async def read(self):
        try:
            header = await self.connection.read()
            if not header:
                return
            body = await self.connection.read()
            logger.debug('READ %s %s', header, body)
            return PSMessage.from_header_body(header, body)
        except StopAsyncIteration:
            logger.debug('DISCONNECT')
            await self.connection.disconnect()
            return None

    async def __await__(self):
        async for data in self:
            logger.info('RECV: %r', data)
            if data is None:
                return

    def register_handler(self, handler):
        self._event_map[handler.req] = (time(), handler)

    async def __aiter__(self):
        while True:
            msg = await self.read()
            if not msg:
                return
            if msg.req < 0:
                t, handler = self._event_map[-msg.req]
                if msg.end_err:
                    await handler.stop()
                    del self._event_map[-msg.req]
                    logger.info('REQ: %d END', msg.req)
                else:
                    logger.info('REQ: %d ELEM: %r', msg.req, msg)
                    await handler.process(msg)
            else:
                yield msg

    def write(self, msg):
        logger.info('SEND: %r (%d)', msg, msg.req)
        header = struct.pack('>BIi', (int(msg.stream) << 3) | (int(msg.end_err) << 2) | msg.type.value, len(msg.data),
                             msg.req)
        self.connection.write(header)
        self.connection.write(msg.data.encode('utf-8'))
        logger.info('WRITE: %s', header)

    def on_connect(self, cb):
        async def _on_connect():
            await cb()
        self.connection.on_connect(_on_connect)

    def stream(self, data):
        msg = PSMessage(PSMessageType.JSON, data, stream=True, end_err=False, req=self.req_counter)
        self.write(msg)
        handler = PSStreamHandler(self.req_counter)
        self.register_handler(handler)
        return handler


class PSClient(PSConnection):
    def __init__(self, host, port, client_kp, server_pub_key, ephemeral_key=None, application_key=None, loop=None):
        super(PSClient, self).__init__()
        self.connection = SHSClient(host, port, client_kp, server_pub_key, ephemeral_key=ephemeral_key,
                                    application_key=application_key, loop=loop)
        self.loop = loop

    def connect(self):
        self.connection.connect()


class PSServer(PSConnection):
    def __init__(self, host, port, client_kp, application_key=None, loop=None):
        super(PSClient, self).__init__()
        self.connection = SHSServer(host, port, client_kp, application_key=application_key, loop=loop)
        self.loop = loop

    def listen(self):
        self.connection.listen()
