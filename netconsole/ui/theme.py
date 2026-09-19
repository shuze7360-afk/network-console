"""主题模块：跟随 Windows 应用主题的明暗双主题。

- 颜色/字体/间距/控件样式集中于此，替换界面里分散的硬编码颜色。
- 系统主题读取：优先 Qt styleHints.colorScheme，不可用回退注册表 AppsUseLightTheme；
  变化经 colorSchemeChanged 信号推送，窗口重新激活时复核。
- 明暗切换只更新样式，不触碰任何网络操作与状态。
"""
from __future__ import annotations

import winreg

from PySide6.QtCore import QObject, Signal

FONT_FAMILY = "Microsoft YaHei UI"
FT_TITLE = 20      # 窗口标题
FT_CARD = 12       # 卡片标题
FT_BODY = 10       # 正文
FT_AUX = 9         # 辅助文字

MARGIN_WINDOW = 24
SPACING_SECTION = 16
PADDING_CARD = 20
RADIUS_CARD = 14
RADIUS_BUTTON = 8
BUTTON_MIN_H = 36

STATUS_TOKENS = ("正常", "故障", "未验证", "主动关闭", "不适用", "外部已启动", "开启中", "关闭中")

LIGHT = {
    "window": "#f3f4f6", "card": "#ffffff", "border": "#e2e6ea", "detail_bg": "#f8f9fb",
    "text": "#1f262e", "text2": "#4c5661", "text3": "#6f7883",
    "accent": "#1a66c2", "accent_hover": "#1559ab", "accent_pressed": "#124d97",
    "button_bg": "#ffffff", "button_border": "#c9d0d8", "button_disabled_bg": "#eef0f2",
    "status": {
        "正常": ("#e6f4ea", "#176d31"),
        "故障": ("#fdeaea", "#b3261e"),
        "未验证": ("#fdf2dc", "#8a5a06"),
        "主动关闭": ("#eceff1", "#546e7a"),
        "不适用": ("#eceff1", "#546e7a"),
        "外部已启动": ("#e7eef8", "#3d5a80"),
        "开启中": ("#fdf2dc", "#8a5a06"),
        "关闭中": ("#fdf2dc", "#8a5a06"),
    },
}

DARK = {
    "window": "#1f2328", "card": "#282d34", "border": "#3a4149", "detail_bg": "#242930",
    "text": "#e6e9ec", "text2": "#b7bfc7", "text3": "#8b95a0",
    "accent": "#2f6cb3", "accent_hover": "#3a7cc4", "accent_pressed": "#275d99",
    "button_bg": "#323941", "button_border": "#454d56", "button_disabled_bg": "#2a2f36",
    "status": {
        "正常": ("#1d3626", "#8fd6a4"),
        "故障": ("#46231f", "#ff9d92"),
        "未验证": ("#3f3417", "#e8c268"),
        "主动关闭": ("#2c333a", "#9fb0bd"),
        "不适用": ("#2c333a", "#9fb0bd"),
        "外部已启动": ("#24344d", "#9dc0ea"),
        "开启中": ("#3f3417", "#e8c268"),
        "关闭中": ("#3f3417", "#e8c268"),
    },
}


def system_prefers_dark() -> bool | None:
    """Windows 应用主题：True=深色。读取失败返回 None（由调用方回退浅色）。"""
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as key:
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            return value == 0
    except OSError:
        return None


class ThemeManager(QObject):
    """维护当前主题；优先 Qt colorScheme 信号，窗口激活时可用 refresh() 复核。"""

    changed = Signal(str)  # "light" / "dark"

    def __init__(self) -> None:
        super().__init__()
        self.mode = "light"
        self._registered = False

    def register_system_signal(self, app) -> bool:
        try:
            hints = app.styleHints()
            hints.colorSchemeChanged.connect(self._on_scheme_changed)
            self._registered = True
            self.refresh(app)
            return True
        except Exception:
            return False

    def _on_scheme_changed(self, scheme) -> None:
        self.refresh_from_scheme(scheme)

    def refresh_from_scheme(self, scheme) -> None:
        # Qt.ColorScheme: 0 Unknown / 1 Light / 2 Dark
        try:
            name = {1: "light", 2: "dark"}.get(int(scheme))
        except Exception:
            name = None
        if name is None:
            name = "dark" if system_prefers_dark() else "light"
        self._set(name)

    def refresh(self, app=None) -> None:
        name = None
        if app is not None:
            try:
                name = {1: "light", 2: "dark"}.get(int(app.styleHints().colorScheme()))
            except Exception:
                name = None
        if name is None:
            name = "dark" if system_prefers_dark() else "light"
        self._set(name)

    def _set(self, name: str) -> None:
        if name != self.mode:
            self.mode = name
            self.changed.emit(name)

    def tokens(self) -> dict:
        return DARK if self.mode == "dark" else LIGHT

    def status_colors(self, status: str) -> tuple[str, str]:
        return self.tokens()["status"].get(status, self.tokens()["status"]["主动关闭"])

    def button_css(self, kind: str) -> str:
        """按钮内联样式（本机应用级 QPushButton QSS 不可靠，实测内联有效）。

        kind: accent（主操作）/ flat（详情等扁平入口）/ normal。
        """
        t = self.tokens()
        base = (f"QPushButton {{ background:{t['button_bg']}; color:{t['text']};"
                f"border:1px solid {t['button_border']}; border-radius:{RADIUS_BUTTON}px;"
                f"min-height:{BUTTON_MIN_H}px; padding:4px 14px; }}"
                f"QPushButton:hover {{ border-color:{t['accent']}; color:{t['accent']}; }}"
                f"QPushButton:pressed {{ background:{t['detail_bg']}; }}"
                f"QPushButton:disabled {{ background:{t['button_disabled_bg']}; color:{t['text3']}; border-color:{t['border']}; }}"
                f"QPushButton:focus {{ border:2px solid {t['accent']}; }}")
        if kind == "accent":
            base = (f"QPushButton {{ background:{t['accent']}; color:#ffffff; border:none;"
                    f"border-radius:{RADIUS_BUTTON}px; min-height:{BUTTON_MIN_H}px; padding:4px 14px; font-weight:600; }}"
                    f"QPushButton:hover {{ background:{t['accent_hover']}; }}"
                    f"QPushButton:pressed {{ background:{t['accent_pressed']}; }}"
                    f"QPushButton:focus {{ border:2px solid {t['text']}; }}")
        elif kind == "flat":
            base = (f"QPushButton {{ background:transparent; color:{t['accent']}; border:none;"
                    f"min-height:24px; padding:2px 6px; }}"
                    f"QPushButton:hover {{ color:{t['accent_hover']}; }}"
                    f"QPushButton:checked {{ font-weight:600; }}"
                    f"QPushButton:focus {{ color:{t['accent_hover']}; }}")
        return base

    def stylesheet(self) -> str:
        t = self.tokens()
        pill = ";".join(
            f'QLabel[pill="{s}"] {{ background:{bg}; color:{fg}; }}'
            for s, (bg, fg) in t["status"].items()
        )
        return f"""
QWidget {{ background: {t['window']}; color: {t['text']}; font-family: "{FONT_FAMILY}"; font-size: {FT_BODY}pt; }}
QLabel {{ background: transparent; }}
QFrame#card {{ background: {t['card']}; border: 1px solid {t['border']}; border-radius: {RADIUS_CARD}px; }}
QWidget#cardBtns {{ background: transparent; }}QFrame#card QLabel#reason {{ color: {t['text2']}; font-size: {FT_BODY}pt; }}
QLabel#aux {{ color: {t['text3']}; font-size: {FT_AUX}pt; }}
QLabel#cardTitle {{ font-size: {FT_CARD}pt; font-weight: 600; color: {t['text']}; }}
QLabel#windowTitle {{ font-size: {FT_TITLE}pt; font-weight: 700; color: {t['text']}; }}
QLabel#summaryTitle {{ font-size: {FT_CARD}pt; font-weight: 600; color: {t['text']}; }}
{pill}
QPushButton {{
    background: {t['button_bg']}; color: {t['text']};
    border: 1px solid {t['button_border']}; border-radius: {RADIUS_BUTTON}px;
    min-height: {BUTTON_MIN_H}px; padding: 4px 14px;
}}
QPushButton:hover {{ border-color: {t['accent']}; color: {t['accent']}; }}
QPushButton:pressed {{ background: {t['detail_bg']}; }}
QPushButton:disabled {{ background: {t['button_disabled_bg']}; color: {t['text3']}; border-color: {t['border']}; }}
QPushButton:focus {{ border: 2px solid {t['accent']}; }}
QPushButton[accent="true"], QPushButton#primaryBtn {{
    background: {t['accent']}; color: #ffffff; border: none; font-weight: 600;
}}
QPushButton[accent="true"]:hover, QPushButton#primaryBtn:hover {{ background: {t['accent_hover']}; }}
QPushButton[accent="true"]:pressed, QPushButton#primaryBtn:pressed {{ background: {t['accent_pressed']}; }}
QPushButton[flat="true"] {{ border: none; background: transparent; color: {t['accent']}; min-height: 24px; padding: 2px 6px; }}
QPlainTextEdit#details, QListWidget#findings {{
    background: {t['detail_bg']}; color: {t['text2']};
    border: 1px solid {t['border']}; border-radius: 8px; font-size: {FT_AUX}pt;
}}
QScrollArea {{ border: none; background: {t['window']}; }}
QStatusBar {{ background: {t['window']}; color: {t['text2']}; }}
QMenu {{ background: {t['card']}; color: {t['text']}; border: 1px solid {t['border']}; }}
QMenu::item:selected {{ background: {t['detail_bg']}; }}
"""


theme_manager = ThemeManager()
