from __future__ import annotations

from PySide6.QtCore import QTimer

from .workers import BrowserLaunchTask, BrowserStatusTask


class LoginController:
    """Automatic dedicated-browser login behavior used by the main window."""

    def _auto_open_login_browser(self) -> None:
        if self._close_requested:
            return
        if self.browser_task or self.browser_status_task or self.metadata_task or self.availability_task or self.extraction_task:
            QTimer.singleShot(1000, self._auto_open_login_browser)
            return
        self._login_watch_active = True
        self._login_watch_checks_remaining = 200
        self._launch_login_browser(automatic=True)

    def _open_login_browser(self) -> None:
        self._login_watch_active = True
        self._login_watch_checks_remaining = 200
        self._launch_login_browser(automatic=False)

    def _launch_login_browser(self, *, automatic: bool) -> None:
        if self.browser_task or self.browser_status_task or self.metadata_task or self.availability_task or self.extraction_task:
            return
        destination = self.video.url if self.video else None
        self._browser_launch_automatic = automatic
        self.login_browser_button.setEnabled(False)
        self.login_browser_button.setText("正在打开…")
        self.status_label.setText("正在启动专用登录浏览器…")
        task = BrowserLaunchTask(destination, self)
        self.browser_task = task
        task.succeeded.connect(self._browser_opened)
        task.failed.connect(self._browser_open_failed)
        task.finished.connect(task.deleteLater)
        task.start()
        self._update_actions()

    def _browser_opened(self, browser_name: str) -> None:
        automatic = self._browser_launch_automatic
        self._browser_launch_automatic = False
        self.browser_task = None
        self.login_browser_button.setText("登录浏览器")
        self.login_browser_button.setEnabled(True)
        self._set_login_status("自动检查中…", "working")
        prefix = "已自动打开" if automatic else "已打开"
        self.status_label.setText(f"{prefix} {browser_name} 专用登录窗口，正在检查登录")
        self._schedule_login_check(500)
        self._update_actions()

    def _browser_open_failed(self, message: str) -> None:
        automatic = self._browser_launch_automatic
        self._browser_launch_automatic = False
        self.browser_task = None
        self.login_browser_button.setText("登录浏览器")
        self.login_browser_button.setEnabled(True)
        self._set_login_status("启动失败", "error")
        self.status_label.setText("登录浏览器启动失败")
        self.login_status_label.setToolTip(message)
        if automatic and self._startup_browser_retries_remaining > 0 and not self._close_requested:
            self._startup_browser_retries_remaining -= 1
            QTimer.singleShot(2000, self._auto_open_login_browser)
        elif not automatic:
            self._show_error(message)
        self._update_actions()

    def _set_login_status(self, text: str, kind: str = "muted") -> None:
        colors = {
            "muted": "#969EAC",
            "working": "#F3BE62",
            "success": "#45D1A3",
            "error": "#FF6E72",
        }
        self.login_status_label.setText(f"登录：{text}")
        self.login_status_label.setStyleSheet(f"color: {colors.get(kind, colors['muted'])};")

    def _schedule_login_check(self, delay_ms: int = 3000) -> None:
        if not self._login_watch_active or self._login_watch_checks_remaining <= 0 or self._close_requested:
            return
        self._login_check_timer.start(max(100, int(delay_ms)))

    def _check_login_status(self) -> None:
        if self.browser_status_task or self.browser_task or self.metadata_task or self.availability_task or self.extraction_task:
            self._schedule_login_check(1500)
            return
        if self._login_watch_active:
            self._login_watch_checks_remaining -= 1
        self.check_login_button.setEnabled(False)
        self._set_login_status("检查中…", "working")
        task = BrowserStatusTask(self)
        self.browser_status_task = task
        task.succeeded.connect(self._login_status_ready)
        task.failed.connect(self._login_status_failed)
        task.finished.connect(task.deleteLater)
        task.start()
        self._update_actions()

    def _login_status_ready(self, logged_in: bool, detail: str) -> None:
        self.browser_status_task = None
        self.check_login_button.setEnabled(True)
        if logged_in:
            self._login_watch_active = False
            self._login_check_timer.stop()
            self._set_login_status(detail or "已登录", "success")
            self.login_status_label.setToolTip("专用浏览器登录状态已自动确认")
            self.status_label.setText(f"B站已登录：{detail or '账号可用'}")
        else:
            if "未启动" in detail or "没有打开" in detail:
                self._set_login_status("浏览器未启动", "muted")
            else:
                self._set_login_status("未登录", "error")
            self.login_status_label.setToolTip(detail)
            self.status_label.setText(detail)
            if "未启动" in detail:
                self._login_watch_active = False
            else:
                self._schedule_login_check()
        self._update_actions()

    def _login_status_failed(self, message: str) -> None:
        self.browser_status_task = None
        self.check_login_button.setEnabled(True)
        self._set_login_status("检查失败", "error")
        self.login_status_label.setToolTip(message)
        self.status_label.setText(message)
        self._schedule_login_check()
        self._update_actions()


__all__ = ["LoginController"]
