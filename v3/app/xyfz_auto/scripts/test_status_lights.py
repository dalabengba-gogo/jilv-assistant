# -*- coding: utf-8 -*-
"""状态灯「后台探测」改造的确定性测试(不做轮询/事件循环, 避免 offscreen 时序干扰)

验证:
  1. MainWindow 构造后按 accounts.json 建出 5 盏灯
  2. _start_probe() 立即返回(不阻塞 GUI 线程), 且并发安全(busy 门闩)
  3. _apply_probe: 在线绿 / 离线灰; 只含单端口的结果不动其它灯
  4. pending: 探测中再点灯不叠加线程, 本轮结束后自动补一次
"""
import os, sys, time
from pathlib import Path

# 从工程根目录运行:  python scripts/test_status_lights.py
# (config.yaml / accounts.json 都是相对 CWD 读的, 与本目录其它测试脚本一致)
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtWidgets import QApplication
import gui.main_window as mw

class FakeAdb:
    def __init__(self, *a, **k):
        self.adb_source = "fake-adb"; self.serial = "127.0.0.1:16384"
    def current_activity(self): return "com.netease.xyfz_02/.Main"

class FakeMatcher:
    def __init__(self, *a, **k): pass
    def reload(self): pass

CALLS = []
def fake_probe_ports(adb_path, host, ports, **k):
    CALLS.append(sorted(int(p) for p in ports))
    return {int(p): (int(p) == 16384) for p in ports}, "fake-adb"

mw.AdbDevice = FakeAdb
mw.TemplateMatcher = FakeMatcher
mw.probe_ports = fake_probe_ports

app = QApplication(sys.argv)
win = mw.MainWindow()

fails = []
def check(name, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"  {extra}" if extra else ""))
    if not cond:
        fails.append(name)

def snap():
    return {p: l.text().startswith("●") for p, l in win._port_lights.items()}

want = sorted(int(a["port"]) for a in __import__("accounts").load_accounts())

print("\n[1] 灯与 accounts.json 一一对应", flush=True)
check("建出 5 盏灯", sorted(win._port_lights.keys()) == want,
      str(sorted(win._port_lights.keys())))

print("\n[2] _start_probe 非阻塞(探测跑在后台线程)", flush=True)
win._probe_busy = False
t0 = time.perf_counter()
win._start_probe()
dt = time.perf_counter() - t0
check("立即返回(<50ms)", dt < 0.05, f"实耗 {dt*1000:.1f} ms")
check("busy 门闩已置位", win._probe_busy is True)
check("后台线程确实发起了一轮探测", CALLS and CALLS[-1] == want, str(CALLS[-1] if CALLS else None))

print("\n[3] _apply_probe: 在线绿 / 离线灰", flush=True)
win._probe_busy = False
win._apply_probe({"ok": True, "states": {p: (p == 16384) for p in want},
                  "source": "fake-adb", "front": "com.netease.xyfz_02/.Main", "err": ""})
s = snap()
check("16384 在线→绿", s[16384] is True)
check("其余 4 个离线→灰", all(s[p] is False for p in want if p != 16384))
check("本机灯带上「前台」信息",
      "前台" in win._port_lights[16384].toolTip(),
      win._port_lights[16384].toolTip().splitlines()[0])

print("\n[4] 只含单端口的结果不碰其它灯", flush=True)
win._apply_probe({"ok": True, "states": {16384: True}, "source": "fake-adb",
                  "front": "", "err": ""})
check("16384 仍绿", snap()[16384] is True)
win._apply_probe({"ok": True, "states": {16416: False}, "source": "fake-adb",
                  "front": "", "err": ""})
s = snap()
check("16384 未被这轮改动", s[16384] is True)
check("16416 仍灰", s[16416] is False)

print("\n[5] adb 整体不可用 → 全灰 + 落错误说明", flush=True)
win._apply_probe({"ok": False, "states": {}, "source": "", "front": "",
                  "err": "找不到 adb 可执行文件"})
s = snap()
check("全部转灰", all(v is False for v in s.values()))
check("提示带上原因", "adb" in win._port_lights[16384].toolTip(),
      win._port_lights[16384].toolTip())

print("\n[6] 探测中再点灯 → 不叠加, 记为 pending 并在本轮后补跑", flush=True)
win._probe_busy = True                 # 假装有一轮在跑
win._probe_pending = None
CALLS.clear()
win._start_probe(only=16448)
check("没有新起线程(busy 仍为真)", win._probe_busy is True and CALLS == [],
      f"calls={CALLS}")
check("pending 记下 16448", win._probe_pending == 16448, str(win._probe_pending))
check("被点的那盏立刻显示「正在重连…」",
      win._port_lights[16448].text().startswith("◌"),
      win._port_lights[16448].text())
win._probe_q.put({"ok": True, "states": {p: False for p in want},
                  "source": "fake-adb", "front": "", "err": ""})
win._drain_probe()                     # 本轮结果回来 → busy 释放 → 补跑 pending
check("pending 已被消费", win._probe_pending is None)
check("补跑的是 16448, 且没再叠加", CALLS == [[16448]], str(CALLS))

# 收尾: 让后台那轮跑完并落队, 不随它去
win._probe_busy = False

print("\n" + ("ALL PASS" if not fails else f"FAILED: {fails}"), flush=True)
sys.stdout.flush()
os._exit(1 if fails else 0)
