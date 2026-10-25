# -*- coding: utf-8 -*-
"""
Тест RCON без внешних библиотек.
Только встроенный Python: ничего ставить не нужно.
Этот же код потом станет частью Discord-бота.
"""
import getpass
import re
import socket
import struct
import sys

DEFAULT_HOST = "localhost"
DEFAULT_PORT = 25575

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

    # --- низкий уровень: пакеты ---
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

    # --- то, что нужно снаружи ---
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
        return re.sub(r"\u00a7.", "", text)  # убрать цветовые коды §

    def close(self):
        if self.sock:
            self.sock.close()
            self.sock = None


def ask(prompt, default):
    value = input(f"{prompt} [Enter = {default}]: ").strip()
    return value or str(default)


def main():
    print("=" * 50)
    print(" Тест RCON для Minecraft-сервера")
    print("=" * 50)
    host = ask("Адрес сервера", DEFAULT_HOST)
    port_text = ask("Порт RCON", DEFAULT_PORT)
    try:
        port = int(port_text)
    except ValueError:
        print("Порт должен быть числом.")
        return
    password = getpass.getpass("Пароль RCON (буквы не видны при вводе, это нормально): ")

    print(f"\nПодключаюсь к {host}:{port} ...")
    rcon = Rcon(host, port, password)
    try:
        rcon.connect()
        print("Подключено! Пароль принят.\n")
        print("Отправляю команду: list")
        print("Ответ сервера:", rcon.command("list") or "(пустой ответ)")
        print("\nМожно ввести свои команды (без /). Пустая строка = выход.")
        while True:
            cmd = input("> ").strip()
            if not cmd:
                break
            print(rcon.command(cmd) or "(пустой ответ)")
    except socket.timeout:
        print("\nОШИБКА: время ожидания вышло.")
        print("- Сервер на Falix сейчас спит или выключен? Запусти его в панели.")
        print("- Порт RCON не открыт наружу или указан неправильно.")
    except ConnectionRefusedError:
        print("\nОШИБКА: соединение отклонено.")
        print("- На этом порту ничего не слушает. Проверь rcon.port и порт в панели Falix.")
    except socket.gaierror:
        print("\nОШИБКА: адрес сервера не найден. Проверь, правильно ли он написан.")
    except RconError as e:
        print("\nОШИБКА:", e)
    except OSError as e:
        print("\nОШИБКА сети:", e)
    finally:
        rcon.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    sys.exit(0)
