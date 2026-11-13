#!/usr/bin/env python3
"""Local Nginx → websockify → TCP echo integration test (never touches VPS config)."""
from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
import re
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_port(port: int, process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"service exited early with status {process.returncode}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                return
        except OSError:
            time.sleep(0.1)
    raise TimeoutError(f"service did not listen on 127.0.0.1:{port}")


def recv_exact(sock: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise RuntimeError("connection closed before the expected WebSocket bytes arrived")
        data.extend(chunk)
    return bytes(data)


class EchoServer:
    def __init__(self, port: int):
        self.port = port
        self.stop = threading.Event()
        self.listener = socket.socket()
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", port))
        self.listener.listen(8)
        self.listener.settimeout(0.2)
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self.stop.is_set():
            try:
                client, _ = self.listener.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            with client:
                client.settimeout(0.5)
                while not self.stop.is_set():
                    try:
                        data = client.recv(4096)
                    except TimeoutError:
                        continue
                    except OSError:
                        break
                    if not data:
                        break
                    try:
                        client.sendall(data)
                    except OSError:
                        break

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stop.set()
        self.listener.close()
        self.thread.join(timeout=1)


def websocket_roundtrip(front_port: int, *, tls: bool) -> None:
    raw = socket.create_connection(("127.0.0.1", front_port), timeout=4)
    if tls:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        client = context.wrap_socket(raw, server_hostname="test.invalid")
    else:
        client = raw
    client.settimeout(4)
    nonce = base64.b64encode(os.urandom(16)).decode("ascii")
    request = (
        "GET /sshws HTTP/1.1\r\n"
        "Host: test.invalid\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {nonce}\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n"
    ).encode("ascii")
    client.sendall(request)
    response = bytearray()
    while b"\r\n\r\n" not in response:
        chunk = client.recv(4096)
        if not chunk:
            raise RuntimeError("connection closed before WebSocket response headers")
        response.extend(chunk)
    headers, trailing = bytes(response).split(b"\r\n\r\n", 1)
    header_text = headers.decode("latin-1")
    if " 101 " not in header_text.splitlines()[0]:
        raise RuntimeError(f"WebSocket handshake failed: {header_text.splitlines()[0]}")
    expected = base64.b64encode(hashlib.sha1((nonce + GUID).encode("ascii")).digest()).decode("ascii")
    if f"Sec-WebSocket-Accept: {expected}".lower() not in header_text.lower():
        raise RuntimeError("WebSocket Sec-WebSocket-Accept value did not match the RFC handshake")
    if trailing:
        raise RuntimeError("unexpected bytes arrived before the test frame")

    payload = b"neverbandits-sshws-probe"
    mask = os.urandom(4)
    masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
    client.sendall(bytes((0x82, 0x80 | len(payload))) + mask + masked)
    frame_header = recv_exact(client, 2)
    first, second = frame_header
    if first & 0x0F != 2 or second & 0x80:
        raise RuntimeError("echo was not an unmasked binary WebSocket frame")
    size = second & 0x7F
    if size == 126:
        size = int.from_bytes(recv_exact(client, 2), "big")
    elif size == 127:
        size = int.from_bytes(recv_exact(client, 8), "big")
    echoed = recv_exact(client, size)
    if echoed != payload:
        raise RuntimeError(f"TCP bridge returned unexpected bytes: {echoed!r}")
    client.close()


def main() -> int:
    nginx = shutil.which("nginx")
    websockify = "/usr/bin/websockify"
    openssl = shutil.which("openssl")
    if not nginx or not Path(websockify).is_file() or not openssl:
        raise SystemExit("Install Nginx, distro websockify, and OpenSSL before running this optional integration test.")

    ports = []
    while len(ports) < 4:
        candidate = free_port()
        if candidate not in ports:
            ports.append(candidate)
    http_port, tls_port, bridge_port, echo_port = ports
    with tempfile.TemporaryDirectory(prefix="neverbandits-sshws-test-") as temp:
        directory = Path(temp)
        for name in ("body", "proxy", "fastcgi", "uwsgi", "scgi"):
            (directory / name).mkdir()
        cert = directory / "fullchain.pem"
        key = directory / "privkey.pem"
        subprocess.run([
            openssl, "req", "-x509", "-nodes", "-newkey", "rsa:2048", "-days", "1",
            "-subj", "/CN=test.invalid", "-addext", "subjectAltName=DNS:test.invalid",
            "-keyout", str(key), "-out", str(cert),
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        template = (ROOT / "nginx" / "neverbandits.conf.in").read_text(encoding="utf-8")
        rendered = template.replace("@DOMAIN@", "test.invalid")
        rendered = rendered.replace("listen 80;", f"listen {http_port};")
        rendered = rendered.replace("listen 443 ssl;", f"listen {tls_port} ssl;")
        rendered = re.sub(r"^\s*listen \[::\]:\d+.*\n", "", rendered, flags=re.MULTILINE)
        rendered = rendered.replace("127.0.0.1:6080", f"127.0.0.1:{bridge_port}")
        rendered = rendered.replace("/etc/ispbandits-dev/tls/current/fullchain.pem", str(cert))
        rendered = rendered.replace("/etc/ispbandits-dev/tls/current/privkey.pem", str(key))
        (directory / "site.conf").write_text(rendered, encoding="utf-8")
        config = directory / "nginx.conf"
        config.write_text(
            f"worker_processes 1;\nerror_log {directory}/error.log notice;\n"
            f"pid {directory}/nginx.pid;\nevents {{ worker_connections 128; }}\n"
            f"http {{ access_log off; client_body_temp_path {directory}/body; "
            f"proxy_temp_path {directory}/proxy; fastcgi_temp_path {directory}/fastcgi; "
            f"uwsgi_temp_path {directory}/uwsgi; scgi_temp_path {directory}/scgi; "
            f"include {directory}/site.conf; }}\n",
            encoding="utf-8",
        )
        subprocess.run([nginx, "-t", "-p", f"{directory}/", "-c", str(config)], check=True)

        with EchoServer(echo_port):
            web_log = (directory / "websockify.log").open("wb")
            nginx_log = (directory / "nginx.log").open("wb")
            bridge = subprocess.Popen(
                [websockify, f"127.0.0.1:{bridge_port}", f"127.0.0.1:{echo_port}"],
                stdout=web_log, stderr=subprocess.STDOUT,
            )
            frontend = subprocess.Popen(
                [nginx, "-p", f"{directory}/", "-c", str(config), "-g", "daemon off;"],
                stdout=nginx_log, stderr=subprocess.STDOUT,
            )
            try:
                wait_port(bridge_port, bridge)
                wait_port(http_port, frontend)
                websocket_roundtrip(http_port, tls=False)
                websocket_roundtrip(tls_port, tls=True)
                print("PASS: Nginx HTTP WebSocket and WSS handshakes returned 101.")
                print("PASS: masked binary data traversed websockify to TCP and echoed back over both transports.")
            finally:
                frontend.terminate()
                bridge.terminate()
                for process in (frontend, bridge):
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=2)
                web_log.close()
                nginx_log.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
