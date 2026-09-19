"""网络控制台主窗口与托盘（通用版）。

四张功能卡：基础网络 / 网络认证 / 代理连接 / 本地服务。
规则：界面不直接修改系统设置（写入一律经执行器审计）；未配置的功能显示
「未配置」并禁用管理按钮；启停是异步操作，完成与否以核验结果为准。
isolated=True 供视觉测试：不启动定时器/真实检查/持久化。
关闭窗口 = 缩到托盘；退出 = 停止常驻监测，不等于关闭任何服务。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import webbrowser
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QListWidget, QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QPushButton,
    QScrollArea, QSystemTrayIcon, QVBoxLayout, QWidget,
)

from netconsole import appconfig, present
from netconsole.diagnostics import inventory
from netconsole.executor import Executor
from netconsole.logutil import get_logger
from netconsole.models import AUTH, BASIC, LINK_ORDER, LINK_TITLES, PROXY, SERVICE
from netconsole.notify_policy import NotifyPolicy
from . import theme
from .widgets import FlowLayout

SWITCH_TEXTS = {
    AUTH: ("关闭网络认证管理", "开启网络认证管理"),
    PROXY: ("关闭代理程序", "开启代理程序"),
    SERVICE: ("关闭本地服务", "开启本地服务"),
}


def make_icon(mood: str = "ok") -> QIcon:
    pm = QPixmap(64, 64)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#243240"))
    p.drawRoundedRect(2, 2, 60, 60, 14, 14)
    palettes = {
        "ok": ("#66bb6a", "#4caf50", "#43a047", "#2e7d32"),
        "attention": ("#ffb300", "#fb8c00", "#e53935", "#c62828"),
        "pending": ("#9e9e9e", "#8d8d8d", "#7b7b7b", "#6d6d6d"),
    }
    for i, color in enumerate(palettes.get(mood, palettes["ok"])):
        p.setBrush(QColor(color))
        p.drawRoundedRect(12 + (i % 2) * 22, 12 + (i // 2) * 22, 16, 16, 5, 5)
    p.end()
    return QIcon(pm)


class CollectThread(QThread):
    done = Signal(dict)
    failed = Signal(str)

    def run(self) -> None:
        try:
            self.done.emit(inventory.collect_all())
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class FnThread(QThread):
    done = Signal(object)

    def __init__(self, fn) -> None:
        super().__init__()
        self._fn = fn

    def run(self) -> None:
        try:
            self.done.emit(self._fn())
        except Exception as exc:  # noqa: BLE001
            self.done.emit({"ok": False, "decision": "exception",
                            "reason": f"{type(exc).__name__}: {exc}"})


class LinkCard(QFrame):
    def __init__(self, link: str, cfg: dict, isolated: bool = False) -> None:
        super().__init__()
        self.link = link
        self.isolated = isolated
        self.fcfg = cfg
        self.setObjectName("card")
        self.setMinimumHeight(150)

        root = QVBoxLayout(self)
        root.setContentsMargins(theme.PADDING_CARD, theme.PADDING_CARD - 4,
                                theme.PADDING_CARD, theme.PADDING_CARD - 6)
        root.setSpacing(8)

        head = QHBoxLayout()
        head.setSpacing(8)
        self.title = QLabel(LINK_TITLES[link])
        self.title.setObjectName("cardTitle")
        self.pill = QLabel("未验证")
        self.pill.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.pill.setStyleSheet(self._pill_css("未验证"))
        head.addWidget(self.title)
        head.addStretch(1)
        head.addWidget(self.pill)
        root.addLayout(head)

        self.last_check = QLabel("最后检查：—")
        self.last_check.setObjectName("aux")
        root.addWidget(self.last_check)

        self.reason = QLabel("尚未检查。点击「检查网络」开始诊断。")
        self.reason.setObjectName("reason")
        self.reason.setWordWrap(True)
        self.reason.setMaximumHeight(58)
        root.addWidget(self.reason)

        btns_widget = QWidget()
        btns_widget.setObjectName("cardBtns")
        self.btns = FlowLayout(btns_widget, spacing=8)
        self.switch_btn: QPushButton | None = None
        self.primary_btn: QPushButton | None = None
        self.next_action: tuple[str, str] | None = None
        self.on_action = None   # MainWindow 注入：action_id -> 行为
        self.on_toggle = None   # MainWindow 注入：link -> 开关处理

        managed = bool((cfg.get("program") or {}).get("path"))
        if link in (AUTH, PROXY, SERVICE):
            self.switch_btn = QPushButton()
            self.switch_btn.clicked.connect(self._toggle_requested)
            self.btns.addWidget(self.switch_btn)
            if link == AUTH and cfg.get("login_url"):
                btn = QPushButton("打开认证页")
                btn.clicked.connect(lambda: webbrowser.open(cfg["login_url"]))
                self.btns.addWidget(btn)
            if link == PROXY:
                fix = QPushButton("清理残留代理")
                fix.setToolTip("清理指向受控端点的死代理残留；其他地址的代理一律不修改")
                fix.clicked.connect(lambda: self.on_action and self.on_action("cleanup_stale_proxy"))
                self.btns.addWidget(fix)
                bypass = QPushButton("恢复直连例外")
                bypass.setToolTip("按配置合并直连例外（不覆盖已有条目）")
                bypass.clicked.connect(lambda: self.on_action and self.on_action("restore_bypass"))
                self.btns.addWidget(bypass)
            elif link == SERVICE:
                health = QPushButton("健康检查")
                health.setToolTip("重新探测服务端口与健康路径")
                health.clicked.connect(lambda: self.on_action and self.on_action("recheck"))
                self.btns.addWidget(health)
            if not managed:
                note = QLabel("未配置")
                note.setObjectName("aux")
                self.btns.addWidget(note)
                if self.switch_btn is not None:
                    self.switch_btn.setEnabled(False)
                    self.switch_btn.setToolTip("未配置可执行程序；请在配置文件中设置")
        root.addWidget(btns_widget)

        self.details_btn = QPushButton("详情 ▾")
        self.details_btn.setFlat(True)
        self.details_btn.setProperty("flat", True)
        self.details_btn.setCheckable(True)
        self.details_btn.clicked.connect(self._toggle_details)
        detail_row = QHBoxLayout()
        detail_row.addStretch(1)
        detail_row.addWidget(self.details_btn)
        root.addLayout(detail_row)

        self.details = QPlainTextEdit()
        self.details.setObjectName("details")
        self.details.setReadOnly(True)
        self.details.setMaximumHeight(150)
        self.details.setPlaceholderText("技术详情（端口/耗时/完整错误/证据）")
        self.details.hide()
        root.addWidget(self.details)

        # 最近一次操作结果：保留到用户关闭或下一次操作
        result_row = QHBoxLayout()
        result_row.setSpacing(4)
        self.result_label = QLabel("")
        self.result_label.setObjectName("aux")
        self.result_label.setWordWrap(True)
        self.result_next = QPushButton("")
        self.result_next.setFlat(True)
        self.result_next.hide()
        self.result_dismiss = QPushButton("✕")
        self.result_dismiss.setFlat(True)
        self.result_dismiss.hide()
        self.result_dismiss.clicked.connect(self.clear_op_result)
        self.result_next.clicked.connect(self._emit_next)
        result_row.addWidget(self.result_label, 1)
        result_row.addWidget(self.result_next)
        result_row.addWidget(self.result_dismiss)
        root.addLayout(result_row)

        self._last_result: dict | None = None

    def set_op_result(self, text: str, next_step: tuple[str, str] | None = None) -> None:
        """保留最近一次操作结果，直到用户关闭或下一次操作。"""
        self.result_label.setText(text)
        if next_step:
            self._next_action = next_step
            self.result_next.setText(next_step[1])
            self.result_next.show()
        else:
            self._next_action = None
            self.result_next.hide()
        self.result_dismiss.show()

    def clear_op_result(self) -> None:
        self.result_label.setText("")
        self._next_action = None
        self.result_next.hide()
        self.result_dismiss.hide()

    def _emit_next(self) -> None:
        if self._next_action and self.on_action:
            self.on_action(self._next_action[0])

    def _toggle_requested(self) -> None:
        if self.on_toggle:
            self.on_toggle(self.link)

    def set_toggle_enabled(self, enabled: bool) -> None:
        if self.switch_btn is not None:
            self.switch_btn.setEnabled(enabled)

    def set_transition(self, text: str | None) -> None:
        if text:
            self.pill.setText(text)
            self.pill.setStyleSheet(self._pill_css(text))
        elif self._last_result:
            self.apply_result(self._last_result, keep_pill=True)

    def reapply_theme(self) -> None:
        self.pill.setStyleSheet(self._pill_css(self.pill.text()))
        if self._last_result:
            self.apply_result(self._last_result, keep_pill=True)

    def _toggle_details(self) -> None:
        visible = not self.details.isVisible()
        self.details.setVisible(visible)
        self.details_btn.setText("详情 ▴" if visible else "详情 ▾")

    @staticmethod
    def _pill_css(status: str) -> str:
        bg, fg = theme.theme_manager.status_colors(status)
        return f"background:{bg}; color:{fg}; border-radius:9px; padding:2px 12px; font-weight:600;"

    def apply_result(self, r: dict, keep_pill: bool = False) -> None:
        self._last_result = r
        if not keep_pill:
            status = r.get("status", "未验证")
            self.pill.setText(status)
            self.pill.setStyleSheet(self._pill_css(status))
        checked = time.strftime("%H:%M:%S", time.localtime(r.get("checked_at", time.time())))
        self.last_check.setText(f"最后检查：{checked}（探测 {r.get('probe_ms', '—')}ms）")
        self.reason.setText(r.get("reason", "—"))
        evidence = json.dumps(r.get("evidence", {}), ensure_ascii=False, indent=1)
        self.details.setPlainText(
            f"结论：{r.get('reason', '—')}\n探测耗时：{r.get('probe_ms', '—')}ms\n"
            f"状态：{r.get('status')}\n\n证据（原始，未改写）：\n{evidence}")

    @property
    def status(self) -> str:
        return self.pill.text()


class MainWindow(QMainWindow):
    def __init__(self, isolated: bool = False) -> None:
        super().__init__()
        self.isolated = isolated
        self.setWindowTitle("网络控制台")
        self.resize(960, 700)
        self._thread: CollectThread | None = None
        self._fix_thread: FnThread | None = None
        self._fixing = False
        self._generation = 0
        self._collect_gen = 0
        self._snapshot: dict | None = None
        self._collect_source = "auto"
        self._show_event_handle = 0
        self.log = get_logger()
        self.executor = None if isolated else Executor()
        if isolated:
            self.policy = NotifyPolicy(
                state_path=Path(tempfile.gettempdir()) / "netconsole-visual-state.json",
                logger=self.log)
        else:
            self.policy = NotifyPolicy(logger=self.log)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.scroll_area = scroll
        self.setCentralWidget(scroll)
        central = QWidget()
        scroll.setWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(theme.MARGIN_WINDOW, theme.MARGIN_WINDOW - 6,
                                theme.MARGIN_WINDOW, theme.MARGIN_WINDOW - 10)
        root.setSpacing(theme.SPACING_SECTION)

        header = QHBoxLayout()
        title = QLabel("网络控制台")
        title.setObjectName("windowTitle")
        header.addWidget(title)
        header.addStretch(1)
        aux = QLabel("每 3 分钟静默检查 · 仅在需要处理时提醒")
        aux.setObjectName("aux")
        header.addWidget(aux)
        root.addLayout(header)

        summary_row = QHBoxLayout()
        self.summary_label = QLabel("当前状态待确认")
        self.summary_label.setObjectName("summaryTitle")
        self.summary_label.setWordWrap(True)
        summary_row.addWidget(self.summary_label, 1)
        self.summary_time = QLabel("")
        self.summary_time.setObjectName("aux")
        summary_row.addWidget(self.summary_time)
        root.addLayout(summary_row)

        cfg = appconfig.load_config()
        grid = QGridLayout()
        grid.setSpacing(theme.SPACING_SECTION)
        self.cards: dict[str, LinkCard] = {}
        for i, link in enumerate(LINK_ORDER):
            card = LinkCard(link, cfg["features"][link], isolated=isolated)
            self.cards[link] = card
            grid.addWidget(card, i // 2, i % 2)
        root.addLayout(grid)
        for c in self.cards.values():
            c.on_action = self._run_action_id
        self._wire_switches()
        self._update_switch_texts()

        actions_widget = QWidget()
        actions = FlowLayout(actions_widget, spacing=8)
        self.refresh_btn = QPushButton("检查网络")
        self.refresh_btn.setObjectName("primaryBtn")
        self.refresh_btn.clicked.connect(lambda: self.start_collect())
        self.export_btn = QPushButton("导出诊断")
        self.export_btn.clicked.connect(self.export_report)
        self.restore_btn = QPushButton("恢复上次配置")
        self.restore_btn.setToolTip("恢复到最近一次健康的系统代理快照（写前读旧值、写后回读）")
        self.restore_btn.clicked.connect(self._restore_snapshot)
        for b in (self.refresh_btn, self.export_btn, self.restore_btn):
            actions.addWidget(b)
        root.addWidget(actions_widget)

        self.findings_toggle = QPushButton("诊断备注（0）")
        self.findings_toggle.setFlat(True)
        self.findings_toggle.setProperty("flat", True)
        self.findings_toggle.setCheckable(True)
        self.findings_toggle.clicked.connect(self._toggle_findings)
        head = QHBoxLayout()
        head.addWidget(self.findings_toggle)
        head.addStretch(1)
        root.addLayout(head)

        self.findings_box = QWidget()
        fb = QVBoxLayout(self.findings_box)
        fb.setContentsMargins(0, 0, 0, 0)
        self.findings = QListWidget()
        self.findings.setObjectName("findings")
        self.findings.setMaximumHeight(130)
        fb.addWidget(self.findings)
        self.findings_box.hide()
        root.addWidget(self.findings_box)
        root.addStretch(1)

        self.status = QLabel("就绪。")
        self.statusBar().addWidget(self.status, 1)

        self._init_tray()
        self.show_timer = QTimer(self)
        self.show_timer.setInterval(1500)
        self.show_timer.timeout.connect(self._poll_show_event)
        self.show_timer.start()
        self.timer = QTimer(self)
        self.timer.setInterval(180_000)
        self.timer.timeout.connect(lambda: self.start_collect(silent=True))
        if not isolated:
            self.timer.start()
            self.start_collect(silent=True)

        if not isolated:
            theme.theme_manager.changed.connect(self._apply_theme)
            self.theme_signal_registered()
        self._apply_theme()

    # ---- 主题 ----
    def theme_signal_registered(self) -> bool:
        app = QApplication.instance()
        if app is None:
            return False
        ok = theme.theme_manager.register_system_signal(app)
        if not ok:
            self.log.warning("colorScheme 信号不可用，退回窗口激活时复核")
        return ok

    def _apply_theme(self) -> None:
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(theme.theme_manager.stylesheet())
        for btn in self.findChildren(QPushButton):
            if btn.isFlat():
                kind = "flat"
            elif btn is self.refresh_btn:
                kind = "accent"
            else:
                kind = "normal"
            btn.setStyleSheet(theme.theme_manager.button_css(kind))
        for card in self.cards.values():
            card.reapply_theme()

    def changeEvent(self, event) -> None:  # noqa: N802
        super().changeEvent(event)
        if self.isolated:
            return
        if event.type() == event.Type.ActivationChange and self.isActiveWindow():
            theme.theme_manager.refresh(QApplication.instance())

    # ---- 托盘 ----
    def _init_tray(self) -> None:
        self.tray = QSystemTrayIcon(make_icon(), self)
        menu = QMenu()
        act_show = QAction("打开控制台", self)
        act_show.triggered.connect(self.show)
        menu.addAction(act_show)
        act_check = QAction("检查网络（后台）", self)
        act_check.triggered.connect(lambda: self.start_collect(silent=True))
        menu.addAction(act_check)
        act_pending = QAction("查看待处理问题", self)
        act_pending.triggered.connect(self._open_pending)
        menu.addAction(act_pending)
        menu.addSeparator()
        act_quit = QAction("退出", self)
        act_quit.triggered.connect(self._really_quit)
        menu.addAction(act_quit)
        self.tray.setContextMenu(menu)
        self.tray.setToolTip("网络控制台")
        self.tray.messageClicked.connect(self._on_message_clicked)
        self.tray.activated.connect(
            lambda reason: self.show()
            if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
        self.tray.show()

    def _really_quit(self) -> None:
        self.timer.stop()
        self.tray.hide()
        QApplication.quit()

    def set_show_event(self, handle: int) -> None:
        self._show_event_handle = handle

    def _poll_show_event(self) -> None:
        from netconsole.ui import single_instance

        if self._show_event_handle and single_instance.pop_show_signal(self._show_event_handle):
            self.show()
            self.raise_()
            self.activateWindow()

    # ---- 采集 ----
    def start_collect(self, silent: bool = False) -> None:
        if self.isolated:
            return
        if silent and (self._fixing or (self._thread is not None and self._thread.isRunning())):
            return
        if self._thread is not None and self._thread.isRunning():
            self.status.setText("有检查或修复正在进行，已忽略本次请求。")
            return
        self._collect_source = "auto" if silent else "manual"
        self._collect_gen = self._generation
        self.refresh_btn.setEnabled(False)
        self.status.setText("正在检查…")
        self.log.info("check start source=%s", self._collect_source)
        self._thread = CollectThread()
        self._thread.done.connect(self.apply_snapshot)
        self._thread.failed.connect(self._collect_failed)
        self._thread.start()

    def _collect_failed(self, msg: str) -> None:
        self.refresh_btn.setEnabled(True)
        self.status.setText(f"检查失败：{msg}")
        self.log.warning("check failed source=%s: %s", self._collect_source, msg)
        for n in self.policy.observe_checker_failure(self._collect_source):
            self.tray.showMessage(n["title"], n["message"],
                                  QSystemTrayIcon.MessageIcon.Warning, 8000)

    def apply_snapshot(self, snap: dict) -> None:
        self._snapshot = snap
        self.policy.observe_checker_success()
        for key in LINK_ORDER:
            r = snap.get("links", {}).get(key)
            if r:
                self.cards[key].apply_result(r)
        findings = snap.get("findings", [])
        self.findings.clear()
        for f in findings:
            prefix = "⚠" if f.get("level") == "warn" else "·"
            self.findings.addItem(f"{prefix} [{f.get('code')}] {f.get('text')}")
        self.findings_toggle.setText(f"诊断备注（{len(findings)}）")
        ts = time.strftime("%H:%M:%S", time.localtime(snap.get("generated_at", time.time())))
        self.refresh_btn.setEnabled(True)
        self.status.setText(f"上次检查 {ts}。自动复查间隔 3 分钟。")
        self.log.info("check done source=%s", self._collect_source)
        # 摘要始终展示；策略/通知/自动清理仅在真实（非隔离）且未过代际时执行
        summ = present.summarize(snap)
        self._apply_summary(summ)
        stale = self.isolated or self._collect_gen != self._generation
        if stale:
            self.log.info("stale snapshot skipped (isolated=%s generation %s vs %s)",
                          self.isolated, self._collect_gen, self._generation)
        if not stale:
            ns = self.policy.observe(snap.get("links", {}), source=self._collect_source)
            if ns:
                self._last_pending_keys = [n["key"] for n in ns]
                self.log.info("notify merged %s", self._last_pending_keys)
                msg = (f"需要处理（{len(ns)} 项）\n"
                       + "\n".join(f"· {n['title']}：{n['message']}" for n in ns))
                self.tray.showMessage("网络控制台", msg,
                                      QSystemTrayIcon.MessageIcon.Warning, 8000)
            daily = snap.get("links", {}).get(PROXY, {})
            if (daily.get("status") == "故障" and daily.get("kind") == "stale_residue"
                    and self.policy.allow_autofix(PROXY)):
                QTimer.singleShot(300, lambda: self._run_fix(
                    self.executor.cleanup_stale_proxy, "自动清理代理残留"))

    def _apply_summary(self, summ: dict) -> None:
        t = theme.theme_manager.tokens()
        self.summary_label.setText(summ["text"])
        colors = {"ok": t["text"], "attention": t["status"]["故障"][1], "pending": t["text3"]}
        self.summary_label.setStyleSheet(f"color:{colors.get(summ['level'], t['text'])};")
        ts = time.strftime("%H:%M:%S", time.localtime(summ.get("at") or time.time()))
        self.summary_time.setText(f"检查 {ts}")
        mood = {"ok": "ok", "attention": "attention", "pending": "pending"}.get(summ["level"], "ok")
        self.tray.setIcon(make_icon(mood))
        self.tray.setToolTip(f"{summ['text']}\n检查 {ts}")

    def _on_message_clicked(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()
        if not self.findings_box.isVisible():
            self.findings_toggle.setChecked(True)
            self._toggle_findings()
        for key in getattr(self, "_last_pending_keys", []):
            card = self.cards.get(key)
            if card:
                card.title.setStyleSheet(f"color:{theme.theme_manager.tokens()['accent']};")
        QTimer.singleShot(2500, self._reset_card_titles)

    def _reset_card_titles(self) -> None:
        for c in self.cards.values():
            c.title.setStyleSheet("")

    def _open_pending(self) -> None:
        self.show()
        self.raise_()
        if not self.findings_box.isVisible():
            self.findings_toggle.setChecked(True)
            self._toggle_findings()

    # ---- 操作 ----
    def _run_fix(self, fn, label: str, card_link: str | None = None) -> None:
        if self.isolated or self.executor is None:
            return
        if self._fixing or (self._thread is not None and self._thread.isRunning()):
            self.status.setText("有检查或修复正在进行，已忽略本次请求。")
            return
        self._fixing = True
        self._generation += 1
        self.status.setText(f"正在执行：{label}（经执行器审计）…")
        self._fix_thread = FnThread(fn)
        self._fix_thread.done.connect(lambda res: self._fix_done(label, card_link, res))
        self._fix_thread.start()

    def _fix_done(self, label: str, card_link: str | None, res: object) -> None:
        self._fixing = False
        if hasattr(res, "to_dict"):
            res = res.to_dict()
        res = res if isinstance(res, dict) else {}
        ok = bool(res.get("ok"))
        decision = str(res.get("decision", ""))
        reason = str(res.get("reason", ""))
        fb = present.operation_feedback(ok, decision, reason)
        text = f"{label}：{fb['state']}（{decision}）{('；' + reason) if reason else ''} · {fb['verify']}"
        self.status.setText(text)
        self.log.info("fix done label=%s ok=%s decision=%s", label, ok, decision)
        if label.startswith("自动") and card_link:
            self.policy.note_autofix(card_link, ok, decision, reason)
        elif card_link:
            card = self.cards.get(card_link)
            if card is not None and not ok and fb["steps"]:
                card.set_op_result(f"上次操作：未完成 · {reason}",
                                   (fb["steps"][0]["action"], fb["steps"][0]["label"]))
        self.start_collect(silent=True)

    def _wire_switches(self) -> None:
        for key in (AUTH, PROXY, SERVICE):
            card = self.cards[key]
            if card.switch_btn is not None:
                card.on_toggle = self._toggle_feature

    def _update_switch_texts(self) -> None:
        for key, (on_text, off_text) in SWITCH_TEXTS.items():
            card = self.cards[key]
            if card.switch_btn is not None:
                enabled = bool(appconfig.feature(key).get("enabled"))
                card.switch_btn.setText(on_text if enabled else off_text)

    def _toggle_findings(self) -> None:
        self.findings_box.setVisible(not self.findings_box.isVisible())

    def _run_action_id(self, action_id: str) -> None:
        """动作标识 → 界面/执行器行为（展示层只给标识，不拼接命令）。"""
        self.show()
        self.raise_()
        if action_id == present.ACTION_RECHECK:
            self.start_collect()
        elif action_id == present.ACTION_OPEN_AUTH:
            url = appconfig.feature("auth").get("login_url")
            if url:
                webbrowser.open(url)
        elif action_id == present.ACTION_OPEN_PROGRAM:
            exe = (appconfig.feature("proxy").get("program") or {}).get("path")
            if exe and os.path.exists(exe):
                os.startfile(exe)  # noqa: S606
        elif action_id == present.ACTION_SHOW_SERVICE:
            card = self.cards.get(SERVICE)
            if card:
                self.scroll_area.ensureWidgetVisible(card)
                if not card.details.isVisible():
                    card._toggle_details()
        elif action_id == present.ACTION_SHOW_DETAILS:
            self._toggle_findings()
        elif action_id == "cleanup_stale_proxy":
            self._run_fix(self.executor.cleanup_stale_proxy, "清理代理残留", PROXY)
        elif action_id == "restore_bypass":
            self._run_fix(self.executor.restore_bypass, "恢复直连例外", PROXY)

    def _toggle_feature(self, link: str) -> None:
        if self.isolated or self.executor is None:
            return
        if self._fixing:
            self.status.setText("有操作正在进行，请稍候。")
            return
        target_on = not bool(appconfig.feature(link).get("enabled"))
        if link in (PROXY, SERVICE) and not target_on:
            confirm = QMessageBox.question(
                self, "关闭功能",
                "将停止对应程序并核验端口下线，同时清理指向受控端点的系统代理/环境变量残留。\n"
                "使用该服务的应用可能断开（可能触发一次 UAC）。继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes)
            if confirm != QMessageBox.StandardButton.Yes:
                return
        self._fixing = True
        self._generation += 1
        for card in self.cards.values():
            card.set_toggle_enabled(False)
        self.cards[link].set_transition("开启中" if target_on else "关闭中")
        self.status.setText(f"正在{'开启' if target_on else '关闭'}{LINK_TITLES[link]}…")
        fn = self.executor.feature_enable if target_on else self.executor.feature_disable
        self._fix_thread = FnThread(lambda: fn(link))
        self._fix_thread.done.connect(lambda res, l=link, on=target_on:
                                      self._toggle_done(l, on, res))
        self._fix_thread.start()

    def _toggle_done(self, link: str, on: bool, res: object) -> None:
        self._fixing = False
        if hasattr(res, "to_dict"):
            res = res.to_dict()
        res = res if isinstance(res, dict) else {}
        ok = bool(res.get("ok"))
        decision = str(res.get("decision", ""))
        reason = str(res.get("reason", ""))
        fb = present.operation_feedback(ok, decision, reason)
        for card in self.cards.values():
            card.set_toggle_enabled(True)
        self.cards[link].set_transition(None)
        self.status.setText(f"{'开启' if on else '关闭'}{fb['state']}（{decision}）：{reason}")
        if not ok:
            self.tray.showMessage(f"{'开启' if on else '关闭'}未完成",
                                  f"{LINK_TITLES[link]}：{reason}",
                                  QSystemTrayIcon.MessageIcon.Warning, 8000)
        self.start_collect(silent=True)

    def _restore_snapshot(self) -> None:
        confirm = QMessageBox.question(
            self, "恢复上次配置",
            "将恢复系统代理设置，影响使用系统代理的应用。\n\n继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes)
        if confirm == QMessageBox.StandardButton.Yes:
            self._run_fix(self.executor.restore_snapshot, "恢复上次配置")

    # ---- 工具 ----
    def export_report(self) -> None:
        if not self._snapshot:
            QMessageBox.information(self, "导出", "请先点「检查网络」生成快照。")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "导出诊断", str(Path.home() / "诊断导出.json"), "JSON (*.json)")
        if not path:
            return
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self._snapshot, fh, ensure_ascii=False, indent=1)
        QMessageBox.information(self, "导出完成", f"已导出：\n{path}")

    def closeEvent(self, event) -> None:  # noqa: N802
        event.ignore()
        self.hide()
