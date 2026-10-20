"""离线预检 (2026-10-20 v5.9.0)

验证:
  1. _is_port_offline 三态判定(无灯 / 在线 / 离线 / 未探测)
  2. _start_task 在所选账号离线时: 记错误日志 + 弹窗 + 不启动(不路由到子进程)
"""
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gui import main_window


class _Light:
    def __init__(self, online):
        self._online = online


class _Host:
    """最小宿主: 只提供 _start_task 离线分支需要的 self 属性"""
    _port_lights = {}
    _local_port = 99999

    def __init__(self):
        self._tp = 16384
        self.logs = []
        self.remote_called = False
        # 把真实的离线判定方法绑到宿主(供 _start_task 内部调用)
        self._is_port_offline = types.MethodType(
            main_window.MainWindow._is_port_offline, self)

    def _target_port(self):
        return self._tp

    def _acc_name(self, port):
        return "测试账号"

    def append_sys_log(self, text):
        self.logs.append(text)

    def _start_remote_task(self, *a, **k):
        self.remote_called = True


def _bind(host):
    return types.MethodType(main_window.MainWindow._start_task, host)


def test_is_port_offline_states():
    host = _Host()
    fn = types.MethodType(main_window.MainWindow._is_port_offline, host)

    host._port_lights = {}
    assert fn(16384) is False, "无灯应放行"

    host._port_lights = {16384: _Light(True)}
    assert fn(16384) is False, "在线应放行"

    host._port_lights = {16384: _Light(False)}
    assert fn(16384) is True, "离线应拦截"

    host._port_lights = {16384: _Light(None)}
    assert fn(16384) is False, "未探测(None)应放行"
    print("  ✅ _is_port_offline 三态判定全部通过")


def test_start_task_blocks_offline():
    # patch QMessageBox.warning 为记录器(避免真实弹窗/Qt依赖)
    warned = []
    main_window.QMessageBox = types.SimpleNamespace(
        warning=lambda *a, **k: warned.append(a))

    host = _Host()
    host._port_lights = {16384: _Light(False)}   # 离线
    _bind(host)(auto=False)                        # 手动启动

    assert host.remote_called is False, "离线时不应路由到子进程"
    assert any("[错误]" in s for s in host.logs), "离线应记错误日志"
    assert len(warned) == 1, "离线应弹一次警告窗"
    print("  ✅ _start_task 离线拦截(记错误日志+弹窗+不启动)通过")


if __name__ == "__main__":
    test_is_port_offline_states()
    test_start_task_blocks_offline()
    print("ALL PASS")
