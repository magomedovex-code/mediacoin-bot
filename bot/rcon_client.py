# -*- coding: utf-8 -*-
"""Простой RCON-клиент для Minecraft. Без внешних библиотек."""
import re
import socket
import struct

TYPE_LOGIN = 3
TYPE_COMMAND = 2
TYPE_AUTH_RESPONSE = 2


class RconError(Exception):
    pass


class Rcon:
    def __init__(self, host, port, password, timeout=10):
        self.host = host
        self.port = port
        self.password = password
        self.timeout = timeout
        self.sock = None
        self.next_id = 1

    def _send(self, ptype, payload):
        req_id = self.next_id
        self.next_id += 1
        body = struct.pack("<ii", req_id, ptype) + payload.encode("utf-8") + b"\x00\x00"
        self.sock.sendall(struct.pack("<i", len(body)) + body)
        return req_id

    def _recv_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise RconError("Сервер закрыл соединение.")
            buf += chunk
        return buf

    def _recv(self):
        length = struct.unpack("<i", self._recv_exact(4))[0]
        data = self._recv_exact(length)
        req_id, ptype = struct.unpack("<ii", data[:8])
        text = data[8:-2].decode("utf-8", errors="replace")
        return req_id, ptype, text

    def connect(self):
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.sock.settimeout(self.timeout)
        self._send(TYPE_LOGIN, self.password)
        while True:
            req_id, ptype, _ = self._recv()
            if ptype == TYPE_AUTH_RESPONSE:
                if req_id == -1:
                    raise RconError("Неверный пароль RCON.")
                return

    def command(self, cmd):
        self._send(TYPE_COMMAND, cmd)
        _, _, text = self._recv()
        return re.sub(r"§.", "", text)  # убрать цветовые коды §

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            finally:
                self.sock = None
