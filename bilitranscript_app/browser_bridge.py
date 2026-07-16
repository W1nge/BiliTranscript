from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import socket
import struct
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .bilibili import SubtitleTrack
from .models import VideoInfo, VideoPart


DEBUG_PORT = 39271
WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
BILIBILI_LOGIN_URL = "https://passport.bilibili.com/login"


class BrowserBridgeError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class BrowserInstallation:
    name: str
    executable: Path


@dataclass(frozen=True, slots=True)
class BrowserTarget:
    target_id: str
    url: str
    title: str
    websocket_url: str


class _BufferedSocket:
    def __init__(self, connection: socket.socket) -> None:
        self.connection = connection
        self.buffer = bytearray()

    def read_exact(self, size: int) -> bytes:
        while len(self.buffer) < size:
            chunk = self.connection.recv(max(4096, size - len(self.buffer)))
            if not chunk:
                raise BrowserBridgeError("浏览器调试连接意外关闭")
            self.buffer.extend(chunk)
        result = bytes(self.buffer[:size])
        del self.buffer[:size]
        return result

    def read_until(self, marker: bytes, limit: int = 65536) -> bytes:
        while marker not in self.buffer:
            if len(self.buffer) > limit:
                raise BrowserBridgeError("浏览器返回的握手数据过大")
            chunk = self.connection.recv(4096)
            if not chunk:
                raise BrowserBridgeError("浏览器调试连接意外关闭")
            self.buffer.extend(chunk)
        end = self.buffer.index(marker) + len(marker)
        result = bytes(self.buffer[:end])
        del self.buffer[:end]
        return result


class _LocalWebSocket:
    """Minimal RFC 6455 client sufficient for local Chrome DevTools traffic."""

    def __init__(self, url: str, timeout: float = 45) -> None:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "ws" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise BrowserBridgeError("只允许连接本机浏览器调试端口")
        port = parsed.port or 80
        connection = socket.create_connection((parsed.hostname, port), timeout=timeout)
        connection.settimeout(timeout)
        self.connection = connection
        self.stream = _BufferedSocket(connection)
        self._handshake(parsed, port)

    def _handshake(self, parsed: urllib.parse.ParseResult, port: int) -> None:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        request = "\r\n".join(
            [
                f"GET {path} HTTP/1.1",
                f"Host: {parsed.hostname}:{port}",
                "Upgrade: websocket",
                "Connection: Upgrade",
                f"Sec-WebSocket-Key: {key}",
                "Sec-WebSocket-Version: 13",
                f"Origin: http://127.0.0.1:{port}",
                "",
                "",
            ]
        ).encode("ascii")
        self.connection.sendall(request)
        response = self.stream.read_until(b"\r\n\r\n")
        header_text = response.decode("iso-8859-1")
        lines = header_text.split("\r\n")
        if not lines or " 101 " not in f" {lines[0]} ":
            raise BrowserBridgeError(f"浏览器拒绝调试连接：{lines[0] if lines else 'unknown'}")
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if ":" in line:
                name, value = line.split(":", 1)
                headers[name.strip().lower()] = value.strip()
        expected = base64.b64encode(hashlib.sha1((key + WEBSOCKET_GUID).encode("ascii")).digest()).decode("ascii")
        if headers.get("sec-websocket-accept") != expected:
            raise BrowserBridgeError("浏览器 WebSocket 握手校验失败")

    def _send_frame(self, opcode: int, payload: bytes = b"") -> None:
        first = 0x80 | (opcode & 0x0F)
        size = len(payload)
        if size < 126:
            header = bytes([first, 0x80 | size])
        elif size <= 0xFFFF:
            header = bytes([first, 0x80 | 126]) + struct.pack("!H", size)
        else:
            header = bytes([first, 0x80 | 127]) + struct.pack("!Q", size)
        mask = os.urandom(4)
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        self.connection.sendall(header + mask + masked)

    def send_json(self, payload: dict[str, Any]) -> None:
        self._send_frame(0x1, json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def _read_frame(self) -> tuple[bool, int, bytes]:
        first, second = self.stream.read_exact(2)
        final = bool(first & 0x80)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        size = second & 0x7F
        if size == 126:
            size = struct.unpack("!H", self.stream.read_exact(2))[0]
        elif size == 127:
            size = struct.unpack("!Q", self.stream.read_exact(8))[0]
        if size > 32 * 1024 * 1024:
            raise BrowserBridgeError("浏览器调试消息超过 32 MB 限制")
        mask = self.stream.read_exact(4) if masked else b""
        payload = self.stream.read_exact(size)
        if masked:
            payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        return final, opcode, payload

    def receive_text(self) -> str:
        fragments: list[bytes] = []
        message_opcode = 0
        while True:
            final, opcode, payload = self._read_frame()
            if opcode == 0x8:
                raise BrowserBridgeError("浏览器关闭了调试连接")
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode in {0x1, 0x2}:
                fragments = [payload]
                message_opcode = opcode
            elif opcode == 0x0 and fragments:
                fragments.append(payload)
            else:
                continue
            if final and message_opcode:
                raw = b"".join(fragments)
                if message_opcode != 0x1:
                    continue
                return raw.decode("utf-8")

    def close(self) -> None:
        try:
            self._send_frame(0x8, struct.pack("!H", 1000))
        except OSError:
            pass
        try:
            self.connection.close()
        except OSError:
            pass


class StandaloneBrowserBridge:
    """Independent Bilibili login browser backed by Edge/Chrome DevTools.

    The app launches a dedicated browser profile. Login cookies stay in that
    profile and are never copied into the Python process; the Bilibili page
    itself performs authenticated player API requests.
    """

    def __init__(
        self,
        *,
        port: int = DEBUG_PORT,
        profile_dir: Path | None = None,
        browser_executable: Path | None = None,
        timeout: float = 1.2,
    ) -> None:
        self.port = int(port)
        local_app_data = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / ".bilitranscript"))
        self.profile_dir = profile_dir or (local_app_data / "BiliTranscript" / "browser-profile")
        self.browser_executable = browser_executable
        self.timeout = timeout
        self.last_error = ""

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @staticmethod
    def discover_browsers() -> tuple[BrowserInstallation, ...]:
        program_files = [
            os.environ.get("PROGRAMFILES"),
            os.environ.get("PROGRAMFILES(X86)"),
            os.environ.get("LOCALAPPDATA"),
        ]
        relative_candidates = (
            ("Microsoft Edge", "Microsoft/Edge/Application/msedge.exe"),
            ("Google Chrome", "Google/Chrome/Application/chrome.exe"),
            ("Brave", "BraveSoftware/Brave-Browser/Application/brave.exe"),
        )
        found: list[BrowserInstallation] = []
        seen: set[str] = set()
        for name, relative in relative_candidates:
            for root in program_files:
                if not root:
                    continue
                path = Path(root) / Path(relative)
                key = os.path.normcase(str(path))
                if path.is_file() and key not in seen:
                    seen.add(key)
                    found.append(BrowserInstallation(name, path))
        for command, name in (("msedge", "Microsoft Edge"), ("chrome", "Google Chrome"), ("brave", "Brave")):
            executable = shutil.which(command)
            if executable:
                path = Path(executable)
                key = os.path.normcase(str(path.resolve()))
                if key not in seen:
                    seen.add(key)
                    found.append(BrowserInstallation(name, path))
        return tuple(found)

    def _http_json(self, path: str, *, method: str = "GET", timeout: float | None = None) -> Any:
        request = urllib.request.Request(
            self.base_url + path,
            method=method,
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def version(self) -> dict[str, Any] | None:
        try:
            payload = self._http_json("/json/version")
            return payload if isinstance(payload, dict) else None
        except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            return None

    def is_running(self) -> bool:
        version = self.version()
        websocket_url = str((version or {}).get("webSocketDebuggerUrl") or "")
        return websocket_url.startswith(("ws://127.0.0.1", "ws://localhost"))

    def _installation(self) -> BrowserInstallation:
        if self.browser_executable:
            path = Path(self.browser_executable)
            if path.is_file():
                return BrowserInstallation(path.stem, path)
            raise BrowserBridgeError(f"找不到浏览器：{path}")
        browsers = self.discover_browsers()
        if not browsers:
            raise BrowserBridgeError("没有找到 Microsoft Edge、Google Chrome 或 Brave")
        return browsers[0]

    def _new_target(self, url: str) -> None:
        encoded = urllib.parse.quote(url, safe="")
        try:
            self._http_json(f"/json/new?{encoded}", method="PUT", timeout=4)
        except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise BrowserBridgeError("无法在专用登录浏览器中打开页面") from exc

    def open_login_browser(self, video_url: str | None = None) -> str:
        destination = video_url or BILIBILI_LOGIN_URL
        if self.is_running():
            self._new_target(destination)
            version = self.version() or {}
            return str(version.get("Browser") or "专用浏览器")

        installation = self._installation()
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        command = [
            str(installation.executable),
            f"--remote-debugging-port={self.port}",
            "--remote-allow-origins=*",
            f"--user-data-dir={self.profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--new-window",
            destination,
        ]
        try:
            subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
        except OSError as exc:
            raise BrowserBridgeError(f"无法启动 {installation.name}：{exc}") from exc
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.is_running():
                return installation.name
            time.sleep(0.2)
        raise BrowserBridgeError(f"{installation.name} 已启动，但调试端口没有就绪")

    def list_targets(self) -> tuple[BrowserTarget, ...]:
        try:
            payload = self._http_json("/json/list")
        except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            return ()
        targets: list[BrowserTarget] = []
        for item in payload or []:
            if not isinstance(item, dict) or item.get("type") not in {None, "page"}:
                continue
            target_id = str(item.get("id") or "")
            websocket_url = str(item.get("webSocketDebuggerUrl") or "")
            if not target_id or not websocket_url:
                continue
            targets.append(
                BrowserTarget(
                    target_id=target_id,
                    url=str(item.get("url") or ""),
                    title=str(item.get("title") or ""),
                    websocket_url=websocket_url,
                )
            )
        return tuple(targets)

    def find_video_target(self, bvid: str) -> BrowserTarget | None:
        needle = bvid.lower()
        for target in self.list_targets():
            if needle in target.url.lower():
                return target
        return None

    def _wait_for_video_target(self, bvid: str, timeout: float = 12) -> BrowserTarget | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            target = self.find_video_target(bvid)
            if target:
                return target
            time.sleep(0.25)
        return None

    @staticmethod
    def _evaluate_target(target: BrowserTarget, javascript: str, timeout: float = 45) -> dict[str, Any]:
        connection = _LocalWebSocket(target.websocket_url, timeout=timeout)
        request_id = 1
        try:
            connection.send_json(
                {
                    "id": request_id,
                    "method": "Runtime.evaluate",
                    "params": {
                        "expression": javascript,
                        "awaitPromise": True,
                        "returnByValue": True,
                        "userGesture": True,
                    },
                }
            )
            while True:
                message = json.loads(connection.receive_text())
                if message.get("id") != request_id:
                    continue
                if message.get("error"):
                    raise BrowserBridgeError(str(message["error"].get("message") or message["error"]))
                result = message.get("result") or {}
                if result.get("exceptionDetails"):
                    detail = result["exceptionDetails"]
                    raise BrowserBridgeError(str(detail.get("text") or "页面脚本执行失败"))
                remote = result.get("result") or {}
                value = remote.get("value")
                return value if isinstance(value, dict) else {}
        except (OSError, socket.timeout, json.JSONDecodeError) as exc:
            raise BrowserBridgeError(f"浏览器页面通信失败：{exc}") from exc
        finally:
            connection.close()

    def login_status(self) -> tuple[bool, str]:
        if not self.is_running():
            return False, "专用登录浏览器未启动"
        target = next((item for item in self.list_targets() if "bilibili.com" in item.url.lower()), None)
        if not target:
            return False, "专用浏览器已启动，但没有打开 B站页面"
        javascript = """
(async () => {
  try {
    const response = await fetch('https://api.bilibili.com/x/web-interface/nav', { credentials: 'include' });
    const payload = await response.json();
    return { isLogin: Boolean(payload?.data?.isLogin), uname: payload?.data?.uname || '' };
  } catch (error) {
    return { isLogin: false, error: String(error) };
  }
})()
""".strip()
        value = self._evaluate_target(target, javascript)
        if value.get("isLogin"):
            return True, str(value.get("uname") or "已登录")
        return False, str(value.get("error") or "尚未登录 B站")

    def fetch_tracks(self, video: VideoInfo, part: VideoPart) -> tuple[SubtitleTrack, ...]:
        self.last_error = ""
        if not self.is_running():
            self.last_error = "专用登录浏览器未启动"
            return ()
        target = self.find_video_target(video.bvid)
        if not target:
            try:
                self._new_target(video.url)
            except BrowserBridgeError as exc:
                self.last_error = str(exc)
                return ()
            target = self._wait_for_video_target(video.bvid)
        if not target:
            self.last_error = "专用浏览器没有加载目标视频页面"
            return ()

        endpoint = (
            "https://api.bilibili.com/x/player/wbi/v2?"
            + urllib.parse.urlencode({"bvid": video.bvid, "cid": part.cid, "aid": video.aid})
        )
        javascript = f"""
(async () => {{
  try {{
    const navResponse = await fetch('https://api.bilibili.com/x/web-interface/nav', {{ credentials: 'include' }});
    const nav = await navResponse.json();
    const response = await fetch({json.dumps(endpoint)}, {{ credentials: 'include' }});
    const payload = await response.json();
    const subtitles = payload?.data?.subtitle?.subtitles || [];
    return {{
      isLogin: Boolean(nav?.data?.isLogin),
      uname: nav?.data?.uname || '',
      code: payload?.code,
      message: payload?.message,
      subtitles: subtitles.map(item => ({{
        lan: item.lan,
        lan_doc: item.lan_doc,
        ai_status: item.ai_status,
        type: item.type,
        id_str: item.id_str,
        subtitle_url: item.subtitle_url || '',
        subtitle_url_v2: item.subtitle_url_v2 || ''
      }}))
    }};
  }} catch (error) {{
    return {{ isLogin: false, error: String(error), subtitles: [] }};
  }}
}})()
""".strip()
        try:
            value = self._evaluate_target(target, javascript)
        except BrowserBridgeError as exc:
            self.last_error = str(exc)
            return ()
        if not value.get("isLogin"):
            self.last_error = str(value.get("error") or "专用浏览器尚未登录 B站")
            return ()
        tracks: list[SubtitleTrack] = []
        for raw in value.get("subtitles") or []:
            language = str(raw.get("lan") or raw.get("id_str") or "unknown")
            url = str(raw.get("subtitle_url") or raw.get("subtitle_url_v2") or "")
            if url.startswith("//"):
                url = "https:" + url
            tracks.append(
                SubtitleTrack(
                    language=language,
                    label=str(raw.get("lan_doc") or language),
                    url=url,
                    is_ai=bool(raw.get("ai_status")) or language.lower().startswith("ai-"),
                    raw=dict(raw),
                )
            )
        if not any(track.url for track in tracks):
            self.last_error = "账号已登录，但该视频没有返回可下载的 AI 字幕"
        return tuple(tracks)


# Kept as a compatibility alias for integrations built against 0.1.0.
BrowserSubtitleBridge = StandaloneBrowserBridge
