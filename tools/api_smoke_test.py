#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}
JOB_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


class ApiCallError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, code: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.code = code


class ApiClient:
    def __init__(self, base_url: str, api_key: str, http_timeout: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.http_timeout = http_timeout

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> tuple[int, str, bytes]:
        body = None
        headers = {
            "Accept": "application/json, text/markdown, text/plain, application/x-subrip",
            "Authorization": f"Bearer {self.api_key}",
        }
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.base_url + path,
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.http_timeout) as response:
                return (
                    int(response.status),
                    str(response.headers.get("Content-Type") or ""),
                    response.read(),
                )
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                error = json.loads(raw.decode("utf-8"))["error"]
                code = str(error.get("code") or "")
                message = str(error.get("message") or "API request failed")
            except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError):
                code = ""
                message = raw.decode("utf-8", errors="replace") or str(exc)
            raise ApiCallError(message, status=exc.code, code=code) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ApiCallError(f"Cannot connect to {self.base_url}: {exc}") from exc

    def request_json(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        status, content_type, raw = self.request(method, path, payload)
        if status < 200 or status >= 300:
            raise ApiCallError(f"Unexpected HTTP status {status}", status=status)
        if not content_type.lower().startswith("application/json"):
            raise ApiCallError(f"Expected JSON response, got {content_type or 'no Content-Type'}")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiCallError("The API returned invalid UTF-8 JSON") from exc
        if not isinstance(value, dict):
            raise ApiCallError("The API returned a JSON value that is not an object")
        return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a real end-to-end smoke test against the BiliTranscript extraction API."
    )
    parser.add_argument("source", help="One Bilibili BV/av ID, full URL, or b23.tv URL")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("BILITRANSCRIPT_API_URL", "http://127.0.0.1:8766"),
        help="API base URL (default: %(default)s; env: BILITRANSCRIPT_API_URL)",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("BILITRANSCRIPT_API_KEY", ""),
        help="Bearer API Key (prefer env: BILITRANSCRIPT_API_KEY)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("api-smoke-result.md"),
        help="Markdown output path (default: %(default)s)",
    )
    parser.add_argument(
        "--job-timeout",
        type=float,
        default=3600.0,
        help="Maximum seconds to wait for extraction (default: %(default)s)",
    )
    parser.add_argument(
        "--http-timeout",
        type=float,
        default=30.0,
        help="Timeout for each HTTP call (default: %(default)s)",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=1.0,
        help="Status polling interval in seconds (default: %(default)s)",
    )
    parser.add_argument(
        "--no-timestamps",
        action="store_true",
        help="Do not include timestamps in the downloaded Markdown",
    )
    return parser


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ApiCallError(message)


def cancel_quietly(client: ApiClient, job_id: str) -> None:
    try:
        client.request_json("POST", f"/v1/extractions/{job_id}/cancel")
        print("Cancellation requested for the unfinished job.", file=sys.stderr)
    except ApiCallError:
        pass


def run(args: argparse.Namespace) -> int:
    if not args.api_key:
        raise ApiCallError(
            "No API Key was provided. Set BILITRANSCRIPT_API_KEY or pass --api-key."
        )
    if args.job_timeout <= 0 or args.http_timeout <= 0 or args.poll_interval <= 0:
        raise ApiCallError("Timeouts and poll interval must be greater than zero")

    client = ApiClient(args.base_url, args.api_key, args.http_timeout)

    print(f"[1/6] Health check: {client.base_url}/health")
    health = client.request_json("GET", "/health")
    require(health.get("status") == "ok", f"Unexpected health response: {health}")
    print(f"      BiliTranscript {health.get('version', 'unknown')} is reachable")

    print("[2/6] OpenAPI and authenticated capabilities")
    openapi = client.request_json("GET", "/openapi.json")
    require(openapi.get("openapi") == "3.1.0", "OpenAPI 3.1 document was not returned")
    capabilities = client.request_json("GET", "/v1/capabilities")
    require(capabilities.get("all_parts") is True, "The server does not report all-parts mode")
    formats = capabilities.get("result_formats")
    require(isinstance(formats, list) and "json" in formats and "markdown" in formats, "Required result formats are missing")
    settings = capabilities.get("settings") or {}
    print(
        "      mode={mode}, asr_backend={backend}, model={model}".format(
            mode=settings.get("mode", "unknown"),
            backend=settings.get("asr_backend", "unknown"),
            model=settings.get("asr_model", "") or "default",
        )
    )

    print(f"[3/6] Submit extraction: {args.source}")
    created = client.request_json("POST", "/v1/extractions", {"source": args.source})
    job_id = str(created.get("id") or "")
    require(bool(JOB_ID_PATTERN.fullmatch(job_id)), f"Invalid job ID in response: {job_id!r}")
    print(f"      job_id={job_id}")

    final_state: dict[str, Any] | None = None
    try:
        print("[4/6] Poll status")
        deadline = time.monotonic() + args.job_timeout
        previous: tuple[Any, ...] | None = None
        while time.monotonic() < deadline:
            state = client.request_json("GET", f"/v1/extractions/{job_id}")
            current = (state.get("status"), state.get("progress"), state.get("message"))
            if current != previous:
                print(f"      {current[0]} {current[1]}% - {current[2]}")
                previous = current
            if state.get("status") in TERMINAL_STATUSES:
                final_state = state
                break
            time.sleep(args.poll_interval)
        if final_state is None:
            raise ApiCallError(f"Job did not finish within {args.job_timeout:g} seconds")
        if final_state.get("status") != "succeeded":
            detail = final_state.get("error") or final_state.get("message") or "unknown error"
            raise ApiCallError(f"Extraction ended as {final_state.get('status')}: {detail}")

        print("[5/6] Validate JSON result")
        result = client.request_json("GET", f"/v1/extractions/{job_id}/result?format=json")
        require(result.get("schema_version") == 1, "Unexpected TranscriptBundle schema version")
        video = result.get("video")
        parts = result.get("parts")
        issues = result.get("issues")
        require(isinstance(video, dict) and bool(video.get("bvid")), "JSON result has no video metadata")
        require(isinstance(parts, list) and bool(parts), "JSON result contains no successful parts")
        require(isinstance(issues, list), "JSON result has an invalid issues field")
        segment_count = sum(len(part.get("segments") or []) for part in parts if isinstance(part, dict))
        require(segment_count > 0, "JSON result contains no transcript segments")
        print(
            f"      {video.get('bvid')} - {video.get('title')} | "
            f"parts={len(parts)}, segments={segment_count}, issues={len(issues)}"
        )

        print("[6/6] Download Markdown result")
        timestamps = "false" if args.no_timestamps else "true"
        status, content_type, markdown = client.request(
            "GET",
            f"/v1/extractions/{job_id}/result?format=markdown&timestamps={timestamps}",
        )
        require(status == 200, f"Unexpected result status: {status}")
        require(content_type.lower().startswith("text/markdown"), f"Unexpected result type: {content_type}")
        require(bool(markdown.strip()), "Markdown result is empty")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(markdown)
        print(f"      saved {len(markdown)} bytes to {args.output.resolve()}")
        print("PASS: the extraction API completed the full workflow.")
        return 0
    except BaseException:
        if final_state is None or final_state.get("status") not in TERMINAL_STATUSES:
            cancel_quietly(client, job_id)
        raise


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return run(args)
    except KeyboardInterrupt:
        print("FAIL: interrupted", file=sys.stderr)
        return 130
    except ApiCallError as exc:
        prefix = f"HTTP {exc.status} " if exc.status is not None else ""
        code = f"{exc.code}: " if exc.code else ""
        print(f"FAIL: {prefix}{code}{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
