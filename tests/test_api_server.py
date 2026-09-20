from __future__ import annotations

import http.client
import json
import threading
import time
import unittest
from http import HTTPStatus

from bilitranscript_app.api_server import (
    ApiRequestError,
    ExtractionApiServer,
    ExtractionJobManager,
    MAX_REQUEST_BYTES,
)
from bilitranscript_app.bilibili import CancelledError
from bilitranscript_app.extractor import ExtractionOptions
from bilitranscript_app.models import (
    ExtractionIssue,
    PartTranscript,
    Segment,
    TranscriptBundle,
    VideoInfo,
    VideoPart,
)


def api_video() -> VideoInfo:
    parts = (
        VideoPart(1, 101, "第一部分", 6),
        VideoPart(2, 102, "第二部分", 7),
    )
    return VideoInfo("BV1abcdefghij", 99, "接口测试视频", "测试 UP", 13, "", 0, "", parts)


class FakeClient:
    def fetch_video(self, source: str) -> VideoInfo:
        if source == "BV1failfailfail":
            raise RuntimeError("读取失败")
        return api_video()


class RecordingExtractor:
    calls: list[tuple[list[VideoPart], ExtractionOptions]] = []

    def extract(self, video, parts, options, **kwargs) -> TranscriptBundle:
        type(self).calls.append((list(parts), options))
        kwargs["progress"](25, "正在提取中文文稿")
        transcripts = [
            PartTranscript(
                part,
                "B站中文 AI 字幕",
                "zh-CN",
                (Segment(0, 1.5, f"{part.title}的中文内容"),),
            )
            for part in parts[:1]
        ]
        return TranscriptBundle(
            video=video,
            parts=transcripts,
            issues=[ExtractionIssue(2, "第二部分", "测试中的部分失败")],
        )


class FailingExtractor:
    def extract(self, video, parts, options, **kwargs):
        raise RuntimeError(f"连接 {options.asr_api_base_url} 时使用 {options.asr_api_key} 失败")


class BlockingExtractor:
    def __init__(self, started: threading.Event, release: threading.Event) -> None:
        self.started = started
        self.release = release

    def extract(self, video, parts, options, **kwargs):
        self.started.set()
        while not self.release.wait(0.01):
            if kwargs["cancelled"]():
                raise CancelledError("操作已取消")
        transcript = PartTranscript(parts[0], "测试字幕", "zh-CN", (Segment(0, 1, "完成"),))
        return TranscriptBundle(video=video, parts=[transcript])


class ApiServerTestCase(unittest.TestCase):
    api_key = "test-api-key-0123456789"

    def setUp(self) -> None:
        RecordingExtractor.calls.clear()
        self.options = ExtractionOptions(
            mode="auto",
            asr_backend="openai-compatible",
            asr_model="mimo-asr",
            asr_api_base_url="http://secret-asr-host:8765/v1",
            asr_api_key="upstream-secret-key",
        )
        self.manager = ExtractionJobManager(
            client_factory=FakeClient,
            extractor_factory=RecordingExtractor,
        )
        self.server = ExtractionApiServer(
            host="127.0.0.1",
            port=0,
            api_key=self.api_key,
            options_provider=lambda: self.options,
            version="0.6.0-test",
            job_manager=self.manager,
        )
        self.server.start()

    def tearDown(self) -> None:
        self.server.stop(wait_for_jobs=True)

    def request(
        self,
        method: str,
        path: str,
        *,
        payload=None,
        headers: dict[str, str] | None = None,
        raw_body: bytes | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        request_headers = dict(headers or {})
        body = raw_body
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        connection = http.client.HTTPConnection("127.0.0.1", self.server.bound_port, timeout=5)
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        raw = response.read()
        result_headers = {key.lower(): value for key, value in response.getheaders()}
        connection.close()
        return response.status, result_headers, raw

    def authorized(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    def submit(self, source: str = "BV1abcdefghij") -> dict:
        status, _, raw = self.request(
            "POST",
            "/v1/extractions",
            payload={"source": source},
            headers=self.authorized(),
        )
        self.assertEqual(status, HTTPStatus.ACCEPTED)
        return json.loads(raw)

    def wait_for_status(self, job_id: str, expected: set[str], timeout: float = 3) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status, _, raw = self.request(
                "GET",
                f"/v1/extractions/{job_id}",
                headers=self.authorized(),
            )
            self.assertEqual(status, HTTPStatus.OK)
            payload = json.loads(raw)
            if payload["status"] in expected:
                return payload
            time.sleep(0.01)
        self.fail(f"任务未进入状态：{expected}")

    def test_health_and_openapi_are_public_but_jobs_require_auth(self) -> None:
        status, headers, raw = self.request("GET", "/health")
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(json.loads(raw)["version"], "0.6.0-test")
        self.assertNotIn("access-control-allow-origin", headers)

        status, _, raw = self.request("GET", "/openapi.json")
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(json.loads(raw)["openapi"], "3.1.0")

        status, headers, raw = self.request("GET", "/v1/capabilities")
        self.assertEqual(status, HTTPStatus.UNAUTHORIZED)
        self.assertIn("bearer", headers["www-authenticate"].lower())
        self.assertEqual(json.loads(raw)["error"]["code"], "unauthorized")

    def test_capabilities_expose_current_non_secret_settings(self) -> None:
        status, _, raw = self.request("GET", "/v1/capabilities", headers=self.authorized())
        self.assertEqual(status, HTTPStatus.OK)
        payload = json.loads(raw)
        self.assertEqual(payload["settings"]["mode"], "auto")
        self.assertEqual(payload["settings"]["asr_model"], "mimo-asr")
        self.assertEqual(payload["result_formats"], ["json", "markdown", "text", "srt"])
        serialized = raw.decode("utf-8")
        self.assertNotIn("secret-asr-host", serialized)
        self.assertNotIn("upstream-secret-key", serialized)

    def test_rotating_key_immediately_invalidates_the_old_key(self) -> None:
        self.server.update_api_key("replacement-api-key-0123456789")
        status, _, _ = self.request("GET", "/v1/capabilities", headers=self.authorized())
        self.assertEqual(status, HTTPStatus.UNAUTHORIZED)
        status, _, _ = self.request(
            "GET",
            "/v1/capabilities",
            headers={"Authorization": "Bearer replacement-api-key-0123456789"},
        )
        self.assertEqual(status, HTTPStatus.OK)

    def test_submit_extracts_all_parts_with_a_settings_snapshot(self) -> None:
        created = self.submit()
        self.options = ExtractionOptions(mode="public")
        finished = self.wait_for_status(created["id"], {"succeeded"})
        self.assertTrue(finished["has_issues"])
        self.assertEqual(finished["settings"]["mode"], "auto")
        self.assertEqual([part.page for part in RecordingExtractor.calls[0][0]], [1, 2])
        self.assertEqual(RecordingExtractor.calls[0][1].asr_model, "mimo-asr")

    def test_result_formats_preserve_chinese_and_content_types(self) -> None:
        job_id = self.submit()["id"]
        self.wait_for_status(job_id, {"succeeded"})

        status, headers, raw = self.request(
            "GET", f"/v1/extractions/{job_id}/result", headers=self.authorized()
        )
        payload = json.loads(raw)
        self.assertEqual(status, HTTPStatus.OK)
        self.assertTrue(payload["has_issues"])
        self.assertIn("中文内容", payload["parts"][0]["text"])
        self.assertTrue(headers["content-type"].startswith("application/json"))

        cases = {
            "markdown&timestamps=true": ("text/markdown", "[00:00] 第一部分的中文内容"),
            "text&timestamps=true": ("text/plain", "[00:00] 第一部分的中文内容"),
            "srt": ("application/x-subrip", "00:00:00,000 --> 00:00:01,500"),
        }
        for query, (content_type, expected) in cases.items():
            with self.subTest(query=query):
                status, headers, raw = self.request(
                    "GET",
                    f"/v1/extractions/{job_id}/result?format={query}",
                    headers=self.authorized(),
                )
                self.assertEqual(status, HTTPStatus.OK)
                self.assertTrue(headers["content-type"].startswith(content_type))
                self.assertIn(expected, raw.decode("utf-8"))

    def test_request_validation_and_method_errors_are_json(self) -> None:
        status, _, raw = self.request(
            "POST",
            "/v1/extractions",
            payload={"source": "BV1abcdefghij", "mode": "public"},
            headers=self.authorized(),
        )
        self.assertEqual(status, HTTPStatus.BAD_REQUEST)
        self.assertEqual(json.loads(raw)["error"]["code"], "invalid_request")

        status, _, raw = self.request(
            "POST",
            "/v1/extractions",
            payload={"source": "BV1abcdefghij 和 BV1zyxwvutsr"},
            headers=self.authorized(),
        )
        self.assertEqual(status, HTTPStatus.BAD_REQUEST)
        self.assertEqual(json.loads(raw)["error"]["code"], "invalid_source")

        status, _, raw = self.request("PUT", "/v1/extractions", headers=self.authorized())
        self.assertEqual(status, HTTPStatus.METHOD_NOT_ALLOWED)
        self.assertEqual(json.loads(raw)["error"]["code"], "method_not_allowed")

        status, _, raw = self.request("TRACE", "/v1/extractions", headers=self.authorized())
        self.assertEqual(status, HTTPStatus.METHOD_NOT_ALLOWED)
        self.assertEqual(json.loads(raw)["error"]["code"], "method_not_allowed")

        status, _, raw = self.request(
            "POST",
            "/v1/extractions",
            raw_body=b"x" * (MAX_REQUEST_BYTES + 1),
            headers={**self.authorized(), "Content-Type": "application/json"},
        )
        self.assertEqual(status, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        self.assertEqual(json.loads(raw)["error"]["code"], "request_too_large")

    def test_port_conflict_keeps_second_server_stopped(self) -> None:
        second_manager = ExtractionJobManager(client_factory=FakeClient, extractor_factory=RecordingExtractor)
        second = ExtractionApiServer(
            host="127.0.0.1",
            port=self.server.bound_port,
            api_key="another-key",
            options_provider=lambda: self.options,
            version="test",
            job_manager=second_manager,
        )
        with self.assertRaises(OSError):
            second.start()
        self.assertFalse(second.running)
        second.stop()


class JobManagerTests(unittest.TestCase):
    def test_cancel_is_idempotent_and_running_work_observes_event(self) -> None:
        started = threading.Event()
        release = threading.Event()
        manager = ExtractionJobManager(
            client_factory=FakeClient,
            extractor_factory=lambda: BlockingExtractor(started, release),
            max_workers=1,
        )
        try:
            created = manager.submit("BV1abcdefghij", ExtractionOptions())
            self.assertTrue(started.wait(1))
            with self.assertRaises(ApiRequestError) as context:
                manager.result(created["id"])
            self.assertEqual(context.exception.status, HTTPStatus.CONFLICT)
            _, requested = manager.cancel(created["id"])
            self.assertTrue(requested)
            deadline = time.monotonic() + 2
            while manager.snapshot(created["id"])["status"] != "cancelled" and time.monotonic() < deadline:
                time.sleep(0.01)
            snapshot, requested = manager.cancel(created["id"])
            self.assertFalse(requested)
            self.assertEqual(snapshot["status"], "cancelled")
        finally:
            release.set()
        manager.shutdown(wait=True)

    def test_submission_and_completion_callbacks_are_called_once(self) -> None:
        submitted: list[str] = []
        finished: list[tuple[str, str]] = []

        class CallbackClient:
            def fetch_video(self, source: str) -> VideoInfo:
                return api_video()

        class CallbackExtractor:
            def extract(self, video: VideoInfo, parts: list[VideoPart], options: ExtractionOptions, **kwargs) -> TranscriptBundle:
                return TranscriptBundle(video, [PartTranscript(parts[0], "测试", "zh", (Segment(0, 1, "完成"),))])

        manager = ExtractionJobManager(
            client_factory=CallbackClient,
            extractor_factory=CallbackExtractor,
            on_submitted=lambda job: submitted.append(job.job_id),
            on_finished=lambda job: finished.append((job.job_id, job.status)),
        )
        created = manager.submit("BV1abcdefghij", ExtractionOptions(mode="public"))
        deadline = time.time() + 2
        while time.time() < deadline and not finished:
            time.sleep(0.01)
        self.assertEqual(submitted, [created["id"]])
        self.assertEqual(finished, [(created["id"], "succeeded")])
        manager.shutdown(wait=True)

    def test_queue_limit_rejects_new_jobs(self) -> None:
        started = threading.Event()
        release = threading.Event()
        manager = ExtractionJobManager(
            client_factory=FakeClient,
            extractor_factory=lambda: BlockingExtractor(started, release),
            max_workers=1,
            max_pending=1,
        )
        try:
            manager.submit("BV1abcdefghij", ExtractionOptions())
            self.assertTrue(started.wait(1))
            with self.assertRaises(ApiRequestError) as context:
                manager.submit("BV1zyxwvutsr", ExtractionOptions())
            self.assertEqual(context.exception.status, HTTPStatus.TOO_MANY_REQUESTS)
        finally:
            release.set()
            manager.shutdown(wait=True)

    def test_finished_jobs_expire(self) -> None:
        now = [100.0]
        manager = ExtractionJobManager(
            client_factory=FakeClient,
            extractor_factory=RecordingExtractor,
            retention_seconds=10,
            clock=lambda: now[0],
        )
        try:
            created = manager.submit("BV1abcdefghij", ExtractionOptions())
            deadline = time.monotonic() + 2
            while manager.snapshot(created["id"])["status"] not in {"succeeded", "failed"} and time.monotonic() < deadline:
                time.sleep(0.01)
            now[0] += 10
            with self.assertRaises(ApiRequestError) as context:
                manager.snapshot(created["id"])
            self.assertEqual(context.exception.status, HTTPStatus.NOT_FOUND)
        finally:
            manager.shutdown(wait=True)

    def test_failure_redacts_upstream_asr_address_and_key(self) -> None:
        options = ExtractionOptions(
            asr_api_base_url="http://secret-asr-host:8765/v1",
            asr_api_key="upstream-secret-key",
        )
        manager = ExtractionJobManager(client_factory=FakeClient, extractor_factory=FailingExtractor)
        try:
            created = manager.submit("BV1abcdefghij", options)
            deadline = time.monotonic() + 2
            snapshot = manager.snapshot(created["id"])
            while snapshot["status"] not in {"failed", "succeeded"} and time.monotonic() < deadline:
                time.sleep(0.01)
                snapshot = manager.snapshot(created["id"])
            self.assertEqual(snapshot["status"], "failed")
            self.assertNotIn("secret-asr-host", snapshot["error"])
            self.assertNotIn("upstream-secret-key", snapshot["error"])
        finally:
            manager.shutdown(wait=True)


if __name__ == "__main__":
    unittest.main()
