from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar
from unittest import mock

from bilitranscript_app.asr import AsrAvailability, AsrResult
from bilitranscript_app.asr_api import (
    ASR_API_BACKEND,
    AsrApiSettings,
    DEFAULT_ASR_API_TIMEOUT,
    OpenAICompatibleAsrRuntime,
)
from bilitranscript_app.extractor import ExtractionOptions, TranscriptExtractor
from bilitranscript_app.models import Segment, VideoInfo, VideoPart


class ApiHandler(BaseHTTPRequestHandler):
    requests: ClassVar[list[tuple[str, dict[str, str], bytes]]] = []

    def log_message(self, _format: str, *_args) -> None:
        return

    def _body(self) -> bytes:
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def do_GET(self) -> None:
        if self.path != "/health":
            self.send_error(404)
            return
        payload = json.dumps({"status": "ok"}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:
        body = self._body()
        headers = {key.lower(): value for key, value in self.headers.items()}
        self.requests.append((self.path, headers, body))
        payload = json.dumps(
            {
                "text": "完整 API 文稿",
                "language": "zh",
                "model": "mimo-asr",
                "segments": [
                    {"start": 0, "end": 1.5, "text": "完整 API"},
                    {"start": 1.5, "end": 3, "text": "文稿"},
                ],
            },
            ensure_ascii=False,
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class FakeApiRuntime:
    def __init__(self) -> None:
        self.calls: list[tuple[Path, AsrApiSettings, str, str]] = []

    def transcribe(self, audio_path: Path, settings: AsrApiSettings, **kwargs) -> AsrResult:
        self.calls.append((audio_path, settings, kwargs["model"], kwargs["language"]))
        return AsrResult(ASR_API_BACKEND, kwargs["model"], kwargs["language"], (Segment(0, 2, "API 识别"),))

    def health(self, _settings: AsrApiSettings) -> str:
        return "ok"


class UnavailableLocalRuntime:
    def detect(self) -> AsrAvailability:
        return AsrAvailability(None, ())


class FakeAudioClient:
    def download_audio(self, _video, _part, destination: Path, **_kwargs) -> None:
        destination.write_bytes(b"fake audio")


class AsrApiTests(unittest.TestCase):
    server: ClassVar[ThreadingHTTPServer]
    thread: ClassVar[threading.Thread]
    base_url: ClassVar[str]

    @classmethod
    def setUpClass(cls) -> None:
        ApiHandler.requests.clear()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), ApiHandler)
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}/v1"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def test_normalizes_root_and_v1_urls(self) -> None:
        runtime = OpenAICompatibleAsrRuntime()
        self.assertEqual(runtime.normalize_base_url("127.0.0.1:8765"), "http://127.0.0.1:8765/v1")
        self.assertEqual(runtime._endpoint(self.base_url, "health", root=True), self.base_url[:-2] + "health")

    def test_default_transcription_timeout_is_one_hour(self) -> None:
        self.assertEqual(DEFAULT_ASR_API_TIMEOUT, 3600.0)
        self.assertEqual(AsrApiSettings().timeout_seconds, 3600.0)

    def test_connection_refused_has_actionable_message(self) -> None:
        error = OpenAICompatibleAsrRuntime._connection_failure(
            "http://127.0.0.1:8765/health",
            ConnectionRefusedError(10061, "refused"),
        )
        self.assertIn("端口没有服务监听", str(error))
        self.assertIn("curl.exe", str(error))

    def test_health_and_multipart_transcription(self) -> None:
        runtime = OpenAICompatibleAsrRuntime()
        settings = AsrApiSettings(self.base_url, "test-key", timeout_seconds=30)
        self.assertEqual(runtime.health(settings), "ok")
        with tempfile.TemporaryDirectory() as temporary:
            audio = Path(temporary) / "clip.wav"
            audio.write_bytes(b"audio bytes")
            result = runtime.transcribe(
                audio,
                settings,
                model="mimo-asr",
                language="zh",
                duration=3,
                cancelled=lambda: False,
                progress=lambda _value, _message: None,
                log=lambda _message: None,
            )
        self.assertEqual(result.backend, ASR_API_BACKEND)
        self.assertEqual(result.segments[0].text, "完整 API")
        path, headers, body = ApiHandler.requests[-1]
        self.assertEqual(path, "/v1/audio/transcriptions")
        self.assertEqual(headers["authorization"], "Bearer test-key")
        self.assertIn(b'name="model"', body)
        self.assertIn(b"mimo-asr", body)
        self.assertIn(b'name="language"', body)
        self.assertIn(b"audio bytes", body)

    def test_extractor_api_backend_skips_local_runtime(self) -> None:
        video = VideoInfo("BV1abcdefghij", 1, "测试", "UP", 3, "", 0, "", (VideoPart(1, 1, "正文", 3),))
        api = FakeApiRuntime()
        local = mock.Mock()
        extractor = TranscriptExtractor(client=FakeAudioClient(), asr_runtime=local, api_runtime=api)
        options = ExtractionOptions(
            mode="asr",
            asr_backend=ASR_API_BACKEND,
            asr_model="mimo-asr",
            asr_api_base_url=self.base_url,
            asr_api_key="local",
        )
        with mock.patch.object(TranscriptExtractor, "_convert_to_wav", side_effect=lambda audio, _out, _cancelled: audio):
            bundle = extractor.extract(
                video,
                list(video.parts),
                options,
                cancelled=lambda: False,
                progress=lambda _value, _message: None,
                log=lambda _message: None,
            )
        self.assertEqual(bundle.parts[0].source, "OpenAI 兼容 API ASR")
        self.assertEqual(bundle.parts[0].text, "API 识别")
        self.assertEqual(len(api.calls), 1)
        local.detect.assert_not_called()

    def test_auto_uses_api_when_no_local_engine_is_available(self) -> None:
        video = VideoInfo("BV1abcdefghij", 1, "测试", "UP", 3, "", 0, "", (VideoPart(1, 1, "正文", 3),))
        api = FakeApiRuntime()
        extractor = TranscriptExtractor(client=FakeAudioClient(), asr_runtime=UnavailableLocalRuntime(), api_runtime=api)
        options = ExtractionOptions(mode="asr", asr_backend="auto", asr_api_base_url=self.base_url)
        with mock.patch.object(TranscriptExtractor, "_convert_to_wav", side_effect=lambda audio, _out, _cancelled: audio):
            bundle = extractor.extract(
                video,
                list(video.parts),
                options,
                cancelled=lambda: False,
                progress=lambda _value, _message: None,
                log=lambda _message: None,
            )
        self.assertEqual(bundle.parts[0].source, "OpenAI 兼容 API ASR")
        self.assertEqual(len(api.calls), 1)


if __name__ == "__main__":
    unittest.main()
