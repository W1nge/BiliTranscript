from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QSettings, QTimer
from PySide6.QtWidgets import QApplication

from bilitranscript_app.main_window import MainWindow


TEST_PORT = 18766
TIMEOUT_SECONDS = 20


def main() -> int:
    application = QApplication([])
    application.setOrganizationName("Winge")
    application.setApplicationName("BiliTranscriptStartupSmoke")
    settings = QSettings()
    settings.setValue("extraction_api/port", TEST_PORT)
    settings.setValue("extraction_api/key", "startup-smoke-test-key-00000000")
    settings.setValue("extraction_api/auto_start", True)

    window = MainWindow(autostart_services=True)
    deadline = time.monotonic() + TIMEOUT_SECONDS
    result: dict[str, object] = {
        "api_running": False,
        "api_version": "",
        "browser_started": False,
        "login_checked": False,
        "login_status": "",
    }

    poll_timer = QTimer()
    poll_timer.setInterval(150)

    def finish(exit_code: int) -> None:
        poll_timer.stop()
        window._login_watch_active = False
        window._login_check_timer.stop()
        if window.extraction_api_server:
            server = window.extraction_api_server
            window.extraction_api_server = None
            server.stop(cancel_jobs=True, wait_for_jobs=False)
        settings.clear()
        print(json.dumps(result, ensure_ascii=False))
        application.exit(exit_code)

    def poll() -> None:
        server = window.extraction_api_server
        result["api_running"] = bool(server and server.running and server.bound_port == TEST_PORT)
        if result["api_running"] and not result["api_version"]:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{TEST_PORT}/health", timeout=2) as response:
                    result["api_version"] = str(json.load(response).get("version") or "")
            except Exception:
                pass

        result["browser_started"] = window.browser_task is None and window.login_status_label.text() != "登录：未检查"
        login_finished = window.browser_task is None and window.browser_status_task is None
        result["login_checked"] = bool(login_finished and not window._login_watch_active)
        result["login_status"] = window.login_status_label.text()

        if result["api_running"] and result["api_version"] and result["browser_started"] and result["login_checked"]:
            finish(0)
        elif time.monotonic() >= deadline:
            finish(1)

    poll_timer.timeout.connect(poll)
    poll_timer.start()
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
