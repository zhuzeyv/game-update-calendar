#!/usr/bin/env python3
"""Render a page with the local Chrome and return its final HTML.

Some sites (SteamDB among them) sit behind a Cloudflare managed challenge
that plain HTTP clients cannot pass.  A real browser can, so this module
drives the Chrome already installed on the machine through the DevTools
protocol and reads ``document.documentElement.outerHTML`` once the page has
settled.

Only the Python standard library is used: the WebSocket framing needed by
CDP is implemented here in ~120 lines instead of pulling in a dependency.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import quote, urlparse
from urllib.request import ProxyHandler, Request, build_opener


class BrowserError(RuntimeError):
    """The local browser could not be started or did not return a page."""


# DevTools lives on 127.0.0.1 and must never go through an HTTP proxy.
_LOCAL = build_opener(ProxyHandler({}))


def _local_get(url: str, timeout: float) -> bytes:
    with _LOCAL.open(url, timeout=timeout) as response:
        return response.read()


WINDOWS_CHROME_PATHS = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
)


def find_browser() -> str | None:
    """Locate a Chromium-based browser on this machine."""

    explicit = os.environ.get("CHROME_PATH", "").strip()
    if explicit and Path(explicit).exists():
        return explicit
    local = os.environ.get("LOCALAPPDATA", "")
    candidates = list(WINDOWS_CHROME_PATHS)
    if local:
        candidates.insert(0, str(Path(local) / "Google/Chrome/Application/chrome.exe"))
        candidates.insert(1, str(Path(local) / "Microsoft/Edge/Application/msedge.exe"))
    for candidate in candidates:
        if Path(candidate).exists():
            return candidate
    for name in ("chrome", "chrome.exe", "msedge", "msedge.exe", "chromium"):
        found = _which(name)
        if found:
            return found
    return None


def _which(name: str) -> str | None:
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory:
            continue
        candidate = Path(directory) / name
        if candidate.exists():
            return str(candidate)
    return None


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class _WebSocket:
    """Just enough RFC 6455 for CDP: text frames, ping/pong, close."""

    def __init__(self, url: str, timeout: float = 60.0) -> None:
        parsed = urlparse(url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 80
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        self.timeout = timeout
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        self._buffer = b""
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        handshake = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(handshake.encode("ascii"))
        head = self._read_until(b"\r\n\r\n")
        status_line = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
        if " 101" not in status_line:
            raise BrowserError(f"WebSocket 握手失败: {status_line}")

    # -- low level ---------------------------------------------------
    def _read_until(self, separator: bytes) -> bytes:
        while separator not in self._buffer:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise BrowserError("WebSocket 连接被关闭")
            self._buffer += chunk
        head, self._buffer = self._buffer.split(separator, 1)
        return head

    def _read_exact(self, size: int) -> bytes:
        while len(self._buffer) < size:
            chunk = self.sock.recv(max(65536, size))
            if not chunk:
                raise BrowserError("WebSocket 连接被关闭")
            self._buffer += chunk
        data, self._buffer = self._buffer[:size], self._buffer[size:]
        return data

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        header = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", length)
        mask = os.urandom(4)
        header += mask
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def _read_frame(self) -> tuple[bool, int, bytes]:
        header = self._read_exact(2)
        fin = bool(header[0] & 0x80)
        opcode = header[0] & 0x0F
        masked = bool(header[1] & 0x80)
        length = header[1] & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._read_exact(8))[0]
        mask = self._read_exact(4) if masked else b""
        data = self._read_exact(length) if length else b""
        if masked and mask:
            data = bytes(byte ^ mask[index % 4] for index, byte in enumerate(data))
        return fin, opcode, data

    # -- public ------------------------------------------------------
    def send(self, text: str) -> None:
        self._send_frame(0x1, text.encode("utf-8"))

    def recv(self) -> str | None:
        buffer = b""
        while True:
            fin, opcode, data = self._read_frame()
            if opcode == 0x8:
                return None
            if opcode == 0x9:
                self._send_frame(0xA, data)
                continue
            if opcode == 0xA:
                continue
            if opcode == 0x0:
                buffer += data
            elif opcode in (0x1, 0x2):
                buffer = data
            else:
                continue
            if fin:
                return buffer.decode("utf-8", errors="replace")

    def close(self) -> None:
        try:
            self._send_frame(0x8, b"")
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


class ChromePage:
    """Start Chrome, open one page, return its HTML, shut Chrome down."""

    def __init__(
        self,
        profile_dir: Path,
        headless: bool = True,
        timeout: float = 45.0,
        settle: float = 1.5,
    ) -> None:
        self.profile_dir = Path(profile_dir).resolve()
        self.headless = headless
        self.timeout = timeout
        self.settle = settle
        self.process: subprocess.Popen[bytes] | None = None
        self.port = 0
        self.executable = ""

    # -- lifecycle ---------------------------------------------------
    def __enter__(self) -> "ChromePage":
        self.executable = find_browser() or ""
        if not self.executable:
            raise BrowserError("未找到本机 Chrome / Edge")
        self.port = _free_port()
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        args = [
            self.executable,
            f"--remote-debugging-port={self.port}",
            f"--user-data-dir={self.profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--disable-blink-features=AutomationControlled",
            "--disable-features=Translate,MediaRouter",
            "--window-size=1440,960",
            "--lang=en-US",
            "about:blank",
        ]
        if self.headless:
            args.insert(1, "--headless=new")
            args.insert(2, "--disable-gpu")
        creation = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" and self.headless else 0
        self.process = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation,
        )
        try:
            self._wait_for_devtools()
        except BrowserError:
            self._shutdown()
            raise
        return self

    def __exit__(self, *_exc: Any) -> None:
        self._shutdown()

    def _shutdown(self) -> None:
        if not self.process:
            return
        pid = self.process.pid
        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    capture_output=True,
                    timeout=15,
                    check=False,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            else:
                self.process.terminate()
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                self.process.kill()
            except OSError:
                pass
        self.process = None

    def _wait_for_devtools(self) -> None:
        deadline = time.monotonic() + 25
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                self._version()
                return
            except (URLError, OSError, ValueError) as error:
                last_error = error
                time.sleep(0.25)
        raise BrowserError(f"Chrome 调试端口未就绪: {last_error}")

    def _version(self) -> dict[str, Any]:
        return json.loads(_local_get(f"http://127.0.0.1:{self.port}/json/version", 3).decode("utf-8"))

    # -- page --------------------------------------------------------
    def open(self, url: str) -> str:
        target = self._new_target(url)
        websocket_url = target.get("webSocketDebuggerUrl")
        if not websocket_url:
            raise BrowserError("Chrome 未返回调试 WebSocket 地址")
        client = _WebSocket(websocket_url, timeout=self.timeout + 20)
        try:
            rpc = _RPC(client)
            rpc.call("Page.enable", {})
            rpc.call("Runtime.enable", {})
            self._wait_ready(rpc)
            time.sleep(self.settle)
            value = rpc.evaluate(
                "document.documentElement ? document.documentElement.outerHTML : ''"
            )
            return str(value or "")
        finally:
            client.close()
            self._close_target(target.get("id") or "")

    def _new_target(self, url: str) -> dict[str, Any]:
        # Chrome >= 111 only accepts PUT here, and it needs an explicit body.
        endpoint = f"http://127.0.0.1:{self.port}/json/new?{quote(url, safe='')}"
        attempts = (
            Request(endpoint, data=b"", method="PUT"),
            Request(endpoint, method="GET"),
        )
        last_error: Exception | None = None
        for request in attempts:
            try:
                with _LOCAL.open(request, timeout=10) as response:
                    return json.loads(response.read().decode("utf-8"))
            except (URLError, OSError, ValueError) as error:
                last_error = error
        raise BrowserError(f"无法在 Chrome 中新建标签页: {last_error}")

    def _close_target(self, target_id: str) -> None:
        if not target_id:
            return
        try:
            _local_get(f"http://127.0.0.1:{self.port}/json/close/{target_id}", 5)
        except (URLError, OSError):
            pass

    def _wait_ready(self, rpc: "_RPC") -> None:
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            try:
                state = rpc.evaluate("document.readyState")
            except BrowserError:
                time.sleep(0.3)
                continue
            if state == "complete":
                return
            time.sleep(0.35)
        raise BrowserError("页面加载超时")


class _RPC:
    def __init__(self, client: _WebSocket) -> None:
        self.client = client
        self.counter = 0

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.counter += 1
        message_id = self.counter
        self.client.send(json.dumps({"id": message_id, "method": method, "params": params or {}}))
        while True:
            raw = self.client.recv()
            if raw is None:
                raise BrowserError(f"CDP 连接中断（{method}）")
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if payload.get("id") != message_id:
                continue
            if "error" in payload:
                raise BrowserError(f"CDP {method} 失败: {payload['error'].get('message')}")
            return payload.get("result") or {}

    def evaluate(self, expression: str) -> Any:
        result = self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": False,
            },
        )
        if result.get("exceptionDetails"):
            detail = result["exceptionDetails"].get("text") or "脚本执行失败"
            raise BrowserError(f"CDP 执行失败: {detail}")
        return (result.get("result") or {}).get("value")


def fetch_html(
    url: str,
    profile_dir: Path,
    headless: bool = True,
    timeout: float = 45.0,
) -> str:
    """Convenience wrapper: open ``url`` in Chrome and return the final HTML."""

    with ChromePage(profile_dir=profile_dir, headless=headless, timeout=timeout) as page:
        return page.open(url)


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "https://example.com/"
    profile = Path(__file__).resolve().parent / "data" / "chrome-profile"
    started = time.monotonic()
    html_text = fetch_html(target, profile)
    print(f"{len(html_text)} 字符 / {time.monotonic() - started:.1f}s")
    print(html_text[:400])
