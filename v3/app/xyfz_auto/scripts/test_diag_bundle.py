# -*- coding: utf-8 -*-
"""P1-6 诊断包收尾 的确定性测试(不依赖 Qt / 模拟器)

验证:
  1. _tail_lines 流式尾读: 内容正确、有省略提示、**不整读**(大文件也能过)
  2. 定时任务清单: 有/无数据都不炸, 关键列齐全
  3. 磁盘与日志体积: 能给出剩余空间与 logs 明细
  4. 整个包能导出成功, 且**编号不重复**(历史上 04 同时属于两份文件)
  5. 新增的排障材料都在包里(定时任务 / 磁盘体积 / adb 原文)
"""
import os
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")

import diagnostics as dg

FAILS = []


def check(cond, msg):
    print(f"  {'✔' if cond else '✘'} {msg}")
    if not cond:
        FAILS.append(msg)


print("[1] _tail_lines: 流式尾读")
tmp = tempfile.mkdtemp(prefix="diagtest_")
big = os.path.join(tmp, "big.log")
N = 20000
with open(big, "w", encoding="utf-8") as f:
    for i in range(N):
        f.write(f"第{i}行\n")
txt = dg._tail_lines(dg.Path(big), 5)
lines = txt.splitlines()
check(lines[-1] == f"第{N - 1}行", "最后一行内容正确")
check(f"前面 {N - 5} 行已省略" in lines[0], "有省略提示且总数算对")
check(len(lines) == 6, f"返回 5 行 + 1 行省略提示(实得 {len(lines)})")

# 内存口径: 用 tracemalloc 证明没有 readlines() 那种整读(重点不是精确字节数,
# 而是"峰值不随文件大小线性增长")
import tracemalloc
tracemalloc.start()
dg._tail_lines(dg.Path(big), 50)
peak = tracemalloc.get_traced_memory()[1]
tracemalloc.stop()
check(peak < 4 * 1024 * 1024, f"尾读 2 万行文件的内存峰值 {peak / 1024:.0f} KB(远小于整读)")

print("\n[2] 定时任务清单(08)")
two = dg.build_timer_text({"timers": [
    {"enabled": True, "time": "09:00", "task": "五环+师门.yaml",
     "account": "本机", "today": "今天已跑"},
    {"enabled": False, "time": "21:30", "task": "域外风情.yaml",
     "account": "账号2(端口16416)", "today": "今天未跑"},
], "timer_grace_minutes": 3})
check("09:00" in two and "五环+师门" in two, "列出时间与脚本")
check("今天已跑" in two and "今天未跑" in two, "带今天状态")
check("3 分钟" in two, "带错过判定宽限")
check("(没有读到定时任务" in dg.build_timer_text({}), "没配定时也不炸")

print("\n[3] 磁盘与日志体积(09)")
disk = dg.build_disk_text({})
check("剩余" in disk and "GB" in disk, "给出所在盘剩余空间")
check("logs" in disk or "合计" in disk, "给出 logs 体积明细")

print("\n[4] 整包导出: 编号唯一 + 新增材料齐全")
dest = dg.export_diagnostics({}, {
    "adb_path": "", "ports": [], "timers": [
        {"enabled": True, "time": "09:00", "task": "五环+师门.yaml",
         "account": "本机", "today": "今天已跑"}],
}, dg.Path(tmp))
with zipfile.ZipFile(dest) as z:
    names = z.namelist()
    txt02 = z.read("02_设备与端口.txt").decode("utf-8", "replace")
    guide = z.read("00_排错指引.txt").decode("utf-8", "replace")
tops = {}
for nm in names:
    head = nm.split("/")[0]
    if head[:2].isdigit():
        tops.setdefault(head[:2], []).append(head)
dups = [k for k, v in tops.items() if len(set(v)) > 1]
check(not dups, f"顶层编号无重复 {sorted(tops)}" if not dups else f"编号重复: {dups}")
need = ("00_排错指引", "01_环境信息", "02_设备与端口", "03_配置摘要", "04_账号表",
        "05_日志健康度", "06_脚本清单", "07_资产统计", "08_定时任务",
        "09_磁盘与日志体积")
for n in need:
    check(any(h.startswith(n) for h in tops.keys()) or any(x.startswith(n) for x in names),
          f"含 {n}")
check("adb devices" in txt02 and ("跳过" in txt02 or "执行失败" in txt02 or "[stderr]" in txt02
      or "无输出" in txt02 or "退出码" in txt02),
      "02 里有 adb devices 原文位(本次没给 adb 路径, 写明原因)")
missing = [n for n in need if n not in guide]
check(not missing, f"排错指引的文件清单已同步(缺: {missing})" if missing
      else "排错指引的文件清单已同步到新编号")

print("\n[5] 清理临时文件")
for nm in os.listdir(tmp):
    os.remove(os.path.join(tmp, nm))
os.rmdir(tmp)
check(True, "已清理")

print("\n" + ("ALL PASS" if not FAILS else f"FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
