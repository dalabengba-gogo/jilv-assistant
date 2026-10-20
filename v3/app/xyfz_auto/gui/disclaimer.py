# -*- coding: utf-8 -*-
"""启动免责声明弹窗 + 主播推荐(可勾选"永久不显示")

2026-10-15 新增:
    用户要求 —— 每次启动先弹一次免责声明, 结尾推荐主播; 提供"永久不显示"
    选项, 结果持久化到 gui_settings.json(与 gui_timer.json 同层, 绿色版拷走后
    设置跟着走)。托盘菜单与「帮助」菜单里可随时重新打开, 也能取消勾选恢复。

    声明正文与 assets/docs/使用说明.txt 的【免责声明】章节一致(这里是精简版),
    改内容时记得同步两边, 并把 DISCLAIMER_VERSION +1 让老用户重新弹一次。

2026-10-16 用户要求(二选一, 这里两条都做了):
    ①正文再精简, 让默认窗口不拖动就能看完(原来是长段落+NoWrap, 要横向拖);
    ②"必须读到最后才能点同意"——正文若因字号/缩放溢出, 「我已阅读并同意」
      在滚到底之前禁用, 并显示提示; 不溢出时按钮直接可用(不给人添堵)。
"""
from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QLabel, QPlainTextEdit, QVBoxLayout,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SETTINGS_PATH = PROJECT_ROOT / "gui_settings.json"

# 声明正文大改时 +1: 老用户(已勾选不再显示)也会重新弹一次, 避免看过旧版就算数
# 2026-10-16: 1 → 2(正文精简改写); 2 → 3(主播推荐格式改写, 用户指定写法)
# 2026-10-20: 3 → 4(新增第 8 条「使用前提」: 分辨率/模拟器/前置任务/固定队/顶队长/反馈渠道)
DISCLAIMER_VERSION = 4

DISCLAIMER_TEXT = """【免责声明】

1. 软件性质
本工具仅通过 ADB 截屏 + 图像识别模拟点击完成重复操作；不修改游戏客户端，不读写
游戏内存，不逆向通信协议，不绕过任何付费与安全机制，不涉及账号密码获取。

2. 风险自担
《大话西游》手游用户协议通常禁止第三方自动化工具，使用本工具可能导致账号警告、
限制、封停或角色数据清空，后果全部由使用者自行承担；重要账号建议先用小号试用。

3. 责任限制
本软件按“现状”提供，作者不作任何明示或默示担保，对任何直接或间接损失不承担
责任。

4. 知识产权
《大话西游》相关名称、图片与素材版权归网易公司所有；本工具与网易公司无任何隶属、
授权或合作关系。

5. 禁止用途
仅限个人学习与非商业使用；严禁商业牟利、破坏游戏公平或干扰他人正常游戏。

6. 合规使用
请遵守中华人民共和国法律法规及所在地区有关规定；所在地区禁止使用的，请勿使用。

7. 分发说明
对外分发须完整保留本声明。本声明不构成法律意见，如需商用请咨询专业律师。

8. 使用前提（不满足可能导致脚本失败）
① 模拟器分辨率须为 540×960（竖屏）。
② 推荐 MuMu 模拟器；用其他品牌请先在「多开管理」改为对应端口号。
③ 账号须已完成主线任务、坐骑任务等前置任务。
④ 账号须已加入固定队，脚本运行时全队在线入队；请勿顶替队长，否则会失败。
⑤ 固定队须能挂机通过地煞 10 星。
⑥ 遇到问题可到推荐主播的视频或直播间反馈。

————————————————————————————
【主播推荐】大话西游归来缘起服-达拉崩吧
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
    """免责声明弹窗: 只读正文 + 「不再显示」勾选 + 「我已阅读并同意」

    2026-10-16: 「我已阅读并同意」在正文滚到底之前禁用(仅当正文确实溢出时),
    滚到底自动解锁; 正文一屏放得下(默认窗口/字号)则按钮直接可用 —— 不给人添堵。
    """

    def __init__(self, parent=None, dont_show: bool = False):
        super().__init__(parent)
        self.setWindowTitle("免责声明")
        self.setModal(True)
        # 2026-10-20: 正文新增「使用前提」后变长, 高度 560 → 640 减少滚动
        self.resize(580, 640)

        lay = QVBoxLayout(self)

        tip = QLabel("使用本工具前，请先阅读以下声明：")
        tip.setStyleSheet("font-weight: 600;")
        lay.addWidget(tip)

        self.box = QPlainTextEdit()
        self.box.setPlainText(DISCLAIMER_TEXT)
        self.box.setReadOnly(True)
        # 2026-10-16: 改回按窗口宽度自动换行(原先 NoWrap, 长行要横向拖动才能看全)
        self.box.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        lay.addWidget(self.box, 1)

        self.lbl_hint = QLabel("请向下滚动，阅读到声明末尾后即可点击同意。")
        self.lbl_hint.setStyleSheet("color:#c60;")
        lay.addWidget(self.lbl_hint)

        self.ck_dont_show = QCheckBox("不再显示此提示（下次启动直接跳过）")
        self.ck_dont_show.setChecked(bool(dont_show))
        lay.addWidget(self.ck_dont_show)

        bb = QDialogButtonBox(QDialogButtonBox.Ok)
        self.btn_ok = bb.button(QDialogButtonBox.Ok)
        self.btn_ok.setText("我已阅读并同意")
        self.btn_ok.setDefault(True)
        bb.accepted.connect(self.accept)
        lay.addWidget(bb)

        sb = self.box.verticalScrollBar()
        sb.valueChanged.connect(self._update_accept_state)
        sb.rangeChanged.connect(lambda *_: self._update_accept_state())
        sb.setValue(0)
        self._update_accept_state()

    def showEvent(self, ev):
        super().showEvent(ev)
        # 换行/布局算完之后再判一次(首次构造时 range 可能还没定下来)
        QTimer.singleShot(0, self._update_accept_state)

    def _update_accept_state(self):
        """滚到底 → 解锁; 正文一屏放得下(没有滚动条)也直接解锁"""
        sb = self.box.verticalScrollBar()
        at_end = sb.maximum() <= 0 or sb.value() >= sb.maximum()
        self.btn_ok.setEnabled(at_end)
        self.lbl_hint.setVisible(not at_end)
        self.btn_ok.setToolTip("" if at_end else "请先滚动阅读到声明末尾")

    def dont_show_checked(self) -> bool:
        return self.ck_dont_show.isChecked()
