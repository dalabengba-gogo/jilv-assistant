# -*- coding: utf-8 -*-
"""启动免责声明弹窗 + 主播推荐(可勾选"永久不显示")

2026-10-15 新增:
    用户要求 —— 每次启动先弹一次免责声明, 结尾推荐主播; 提供"永久不显示"
    选项, 结果持久化到 gui_settings.json(与 gui_timer.json 同层, 绿色版拷走后
    设置跟着走)。托盘菜单与「帮助」菜单里可随时重新打开, 也能取消勾选恢复。

    声明正文与 assets/docs/使用说明.txt 的【免责声明】章节一致(精简版),
    改内容时记得同步两边, 并把 DISCLAIMER_VERSION +1 让老用户重新弹一次。
"""
from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QLabel, QPlainTextEdit, QVBoxLayout,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SETTINGS_PATH = PROJECT_ROOT / "gui_settings.json"

# 声明正文大改时 +1: 老用户(已勾选不再显示)也会重新弹一次, 避免看过旧版就算数
DISCLAIMER_VERSION = 1

DISCLAIMER_TEXT = """【免责声明】

1. 软件性质
本工具仅通过 Android 调试桥（ADB）截取设备屏幕画面，经图像识别后模拟点击完成
重复性操作。它不修改游戏客户端，不读取或写入游戏内存，不逆向通信协议，不绕过
任何付费与安全机制，也不涉及任何账号密码的获取。

2. 使用风险自担
《大话西游》手游的用户协议通常禁止使用第三方自动化工具。使用本工具可能导致账
号警告、限制、封停或角色数据清空等后果，全部由使用者自行承担。请自行评估风险，
重要账号建议先在无关紧要的小号上试用。

3. 责任限制
本软件按“现状”提供，作者不提供任何明示或默示的担保。无论何种原因造成的直接或
间接损失，作者均不承担任何责任。

4. 知识产权
《大话西游》相关名称、图片与素材的版权归网易公司所有。本工具与网易公司无任何
隶属、授权或合作关系。

5. 禁止用途
仅限个人学习与非商业使用；严禁用于任何商业牟利、破坏游戏公平或干扰他人正常游
戏的行为。

6. 合规使用
请遵守中华人民共和国法律法规及所在地区的有关规定；所在地区禁止使用的，请勿
使用。

7. 分发说明
对外分发时须完整保留本声明。本声明不构成法律意见，如需商用请咨询专业律师。

————————————————————————————

【主播推荐】

    大话西游归来 · 缘起服 —— 达拉崩吧

看大话西游直播，推荐上面这位主播。
（纯属个人喜好推荐，与主播本人无任何商业合作。）
"""


def load_disclaimer_settings() -> dict:
    """读 gui_settings.json(失败返回空 dict, 即"从未设置过")"""
    try:
        return json.loads(Path(SETTINGS_PATH).read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def save_disclaimer_settings(st: dict):
    """写回 gui_settings.json(与 gui_timer.json 同风格, 出错不影响主流程)"""
    try:
        Path(SETTINGS_PATH).write_text(
            json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def should_show_disclaimer() -> bool:
    """启动时是否要弹: 勾过"不再显示"且声明版本没变过 → 不弹"""
    st = load_disclaimer_settings()
    if not st.get("dont_show", False):
        return True
    return int(st.get("version", 0)) < DISCLAIMER_VERSION


class DisclaimerDialog(QDialog):
    """免责声明弹窗: 只读正文 + 「不再显示」勾选 + 「我已阅读并同意」"""

    def __init__(self, parent=None, dont_show: bool = False):
        super().__init__(parent)
        self.setWindowTitle("免责声明")
        self.setModal(True)
        self.resize(580, 560)

        lay = QVBoxLayout(self)

        tip = QLabel("使用本工具前，请先阅读以下声明：")
        tip.setStyleSheet("font-weight: 600;")
        lay.addWidget(tip)

        box = QPlainTextEdit()
        box.setPlainText(DISCLAIMER_TEXT)
        box.setReadOnly(True)
        box.setLineWrapMode(QPlainTextEdit.NoWrap)
        lay.addWidget(box, 1)

        self.ck_dont_show = QCheckBox("不再显示此提示（下次启动直接跳过）")
        self.ck_dont_show.setChecked(bool(dont_show))
        lay.addWidget(self.ck_dont_show)

        bb = QDialogButtonBox(QDialogButtonBox.Ok)
        btn = bb.button(QDialogButtonBox.Ok)
        btn.setText("我已阅读并同意")
        btn.setDefault(True)
        bb.accepted.connect(self.accept)
        lay.addWidget(bb)

    def dont_show_checked(self) -> bool:
        return self.ck_dont_show.isChecked()
