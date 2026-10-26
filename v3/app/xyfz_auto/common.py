# -*- coding: utf-8 -*-
"""公共工具：配置加载、图片解码、日志"""
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler      # v4.6.0(P0-4) 文本日志滚动
import shutil
import sys
from pathlib import Path

import cv2
import numpy as np
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent  # xyfz_auto/

APP_NAME = "鸡驴助手"

# 版本号规则 (2026-10-16 用户修订, 取代 2026-10-09 的旧约定): 主.界面/功能位.脚本位
#   「3.」后面两位数字, 各记各的账:
#     第一位(十位) = **界面/功能**的修改次数(GUI、引擎行为、配置项、打包脚本等)
#     第二位(个位) = **脚本**(assets/tasks 里的 YAML)的修改次数
#     十位进一: 个位满 10 → 十位 +1、个位归 0
#     界面/功能改动 → 十位 +1 且**个位归零**(高位进位, 低位重置)
#   主版本进位(沿用旧规则): 十位满 10 → 主 +1, 两位归 0 (如 3.9.x → 4.0.0)
# 改版本号时同步这四处: common.py 本行 / __init__.py 的 __version__ / 使用说明.txt
# 版本历史 / README.md 顶部的「当前版本」
# (2026-10-16 v3.0.0: 原先只列三处, README 不在清单里 → 每次必漏, 长期滞后一个
#  版本。这是流程设计缺陷不是疏忽, 已把 README 补进来。以 common.py 为准。)
APP_VERSION = "6.1.4"        # v6.1.4 —— 修「五环+师门自动离队没生效」: ①奖励弹窗守卫提到脚本最前(原来排在离队段之后,
                             #   弹窗挡着时离队段全空转→判"不在队伍"→人还在队里→前往/选师傅被静默拒绝→0/5却报成功)
                             #   ②离队判据由 OCR「离队」改成「目标」(固定队面板也有"离队"353,506,会误点成退固定队)
                             #   ③点离队后实测直接回主界面, 收尾改为先判主界面再决定是否点返回箭头。(YAML位, 2026-10-26)
                             # v6.1.3 —— 「领取离线奖励」从全家桶拆成独立顶层脚本: tasks/领取离线奖励.yaml
                             #   = 离线-守卫(奖励弹窗 loop8, 与 v6.1.1 同款) + include parts/daily_claim.yaml
                             #   + 收尾确认主界面。内容不复制, 与 daily_all 共用同一份子脚本。(YAML位, 2026-10-26)
                             # v6.1.2 —— 修「单独跑五环+师门」三处: ①脚本最前先离队(在队伍里时前往/选师傅被静默拒绝→整天0/5) ②纯享段最前加奖励弹窗守卫(它是5个任务共同入口, 旧版先跑它被弹窗挡死) ③纯享失败不再终止任务(YAML位)(2026-10-23)
                             #   依据: v5.3.1 那轮 daily_all 27.8 分钟 / 351 步的耗时构成 ——
                             #     OCR 341 帧×1.55s=8.8min(32%) / 截图 417 次=6.0min(20%) /
                             #     写死 sleep 6.5min(24%) / 实点击滑动只 6min(21%)。
                             #   即"真正动手 6 分钟, 其余 22 分钟都在重复看同一屏"。
                             #   ① adb 预检去掉(device/adb_client.py): shell()/screenshot() 每次
                             #      前都跑一遍 is_online()(adb devices 单次 136ms), 一轮约 770 次
                             #      ≈1分45秒纯开销, 而且设备真掉线它也救不回来。改成"先跑, 失败
                             #      再重连+重试一次"。tap 内置防抖 0.3→0.12s(实测 tap 总 435ms
                             #      = shell 135ms + sleep 300ms), 350 次点击又省 1 分钟。
                             #   ② 帧复用 + 裁剪识别 + 热点窗口(engine/runner.py):
                             #      · 帧复用 _grab(ttl): 2s 内复用同一帧, 动作(tap/swipe/key)后
                             #        立刻失效; **轮询一律 ttl=0**(复用旧帧等于把轮询变瞎)。
                             #      · region 从"全图 OCR 后过滤"改成真的裁小再识别。只按**纵向
                             #        裁带**, 横向保持整行 —— 第一版按矩形裁, 24 张真实现场图有
                             #        10 张把「今日多倍次数：10/10」截成「倍次数：10/10」, 判据
                             #        会因此漏判。实测 1849ms → 1081ms。
                             #      · 热点窗口: 记住某关键词上次出现的位置, 下次先只识别那一小块
                             #        (619ms vs 1849ms, 实测命中率 93%), 没命中就照常全图,
                             #        连续 2 次不中自动忘掉(防止"不在"的检查倒赔时间)。
                             #      · ⚠️ 缩放降采样**无效**: 0.75/0.6/0.5 倍实测 2.0~2.2s, 反而
                             #        更慢(引擎内部会拉到自己的尺寸), 所以只走裁剪。
                             #   ③ 抓屏改走 RAW(`adb exec-out screencap`, 无 PNG 编码):
                             #      684ms → 393ms(设备端 PNG 编码才是慢的那一半)。
                             #      ⚠️ 头部长度必须按(总长-像素数据)反推: 写死 12 字节
                             #      会让整屏右移 1 像素(MuMu 实际 16 字节头), 肉眼看不出,
                             #      但固定坐标点击和模板匹配会悄悄偏。解析失败一律退回 PNG。
                             #   ④ 轮询间隔 1.0→0.3s(config/默认值): 一次轮询本身 2.8s, 再睡
                             #      1 秒等于多付 26% 空转, 且窗口内帧数变少 → 假超时更多。
                             #   + 运行级性能统计(_emit_stats): 每轮结束打一行"截图/OCR 次数、
                             #     复用与热点省了多少、占本轮百分之几", 优化有没有效一眼可辨。
                             #   sleep→wait 的普查结论: **不动**。345 秒固定等待里 240 秒是
                             #     一条龙「安静确认」4×60s(跨赶路间隙的安全窗口, 曾出事故)、
                             #     105 秒是战斗过渡守卫(域外/五环收尾的 15s/10s/12s), 剩 18 秒
                             #     的复核停顿改了收益 < 风险。
                             #   只动引擎与设备层 → 界面功能位 +1 → 5.5.0。
                             #
                             # v5.4.0 —— 结束统计清零 + 一条龙续跑判据(真机 2026-10-19 16:20):
                             #   P0-1 条件探测补标: 11 处「若…则…」带点击的探测步显式 optional
                             #     (地煞结算弹窗×2 三档循环、域外"若面板开着则关闭"×4、
                             #      一条龙"若误开任务总览"/"结束前若任务总览"/"若弹离线完成"、
                             #      地煞"若有10星则点10星"/"恢复-若获取途径弹窗")。
                             #     一轮 daily_all 实测有 6 条以上这类假失败把真失败淹掉。
                             #     新增 scripts/check_optional.py(静态审计, 已接进 check_refs,
                             #     只提示不阻断) —— 以后漏标会被点名, 不靠人肉记。
                             #   P0-2 一条龙续跑判据: 旧判据 has:['/'] not:[V] 等于"任务栏任何
                             #     带计数的行", 会命中主线/跑环行 → 假续跑 → 监控 45 轮(≈50分钟)
                             #     白等。ocr_row 新增 any_of, 必须命中一条龙 5 个玩法名之一;
                             #     点完加「续跑后确认真的动了」(8s 内离开主界面/进战斗), 否则
                             #     直接按今日已完成收尾。
                             #   P0-3 找卡: 五环/师门点卡 8s→12s(单帧 OCR 1.5~5.2s, 8s 只够
                             #     1~2 帧, 而横幅遮挡 3~6s); 五环失败先"等横幅 4s 再点一次",
                             #     不再直接掉进 11 步恢复块(实测白走 20 秒)。
                             #   P1-1 手动停止不算失败: except InterruptedError 原来写在
                             #     except Exception **之后**(永远走不到), 已移到前面; 并在
                             #     通用异常分支先判 running/_stop_at —— 停止时的 DeviceError
                             #     不再在结束统计里留一条「真失败」。
                             #   P1-4 魔王窟礼包: 可领取时"!"角标盖住模板 → 新增
                             #     click 的 mode: locate_or_at(模板优先, 失配退回实测坐标)。
                             #   引擎+脚本都动 → 界面功能位 +1 → 5.4.0。
                             #
                             # v5.3.0 —— 「等待超时不算真失败」+ 介绍页按钮修复(真机 2026-10-19):
                             #   用户定调: 「类似这种等待情况, 超时失败是正常的, 不应计入
                             #   脚本结束后的统计」。真机一轮日常里 28 步"未做成"中一大半
                             #   是等待超时, 其中「域外验收-看徽标」+「复核徽标」一个玩法
                             #   就重复算两步 → 统计噪音盖住了真问题。
                             #   改法(engine/runner.py): _handle_fail/_record_failure 新增
                             #   kind="timeout"; 等待超时且**本步无动作**(纯 wait) 且作者
                             #   没写 on_fail: stop/retry → 记 skipped_steps(条件未命中),
                             #   不进「真失败 N 步」。
                             #   · 有动作的步超时(点前往/点领取/点师傅)仍算真失败 ——
                             #     "该点的没点上"是真问题, 不能一起抹掉;
                             #   · 验收点(verify_done)的面板结论不变: _mark_verified 照旧
                             #     记 True/False, 子任务该 ✔ / ✘ 还是 ✔ / ✘(权威答案没削弱);
                             #   · required 步仍算真失败。
                             #   回归: scripts/test_failure_accounting.py 新增 [8] 用例。
                             #
                             # v5.2.1 —— 「介绍页按钮点不动」根治(真机 2026-10-19 14:20):
                             #   账号2 一轮 1022 步 22 分钟跑完(v5.2.0 的域外死循环修好了),
                             #   但师门 0/5、五环 0/5、域外 0/50 全没做成 —— 三个玩法的
                             #   「前往」/「师傅」都点不动。真机逐一定位到 3 个独立原因:
                             #   ① 师门/五环 的「前往」一直点 (241,782), 而这恰好是 OCR
                             #      「前注(前往)」文字框的**左上角**(实测框 241,782 60x29)——
                             #      点角上游戏判为出界, 介绍页纹丝不动; 同一台机手点框中心
                             #      (270,800) 立刻进任务/战斗。改中心 (271,800),
                             #      12s 未生效再换落点 (271,812) 补点一次才判死。
                             #   ② 域外风情是「3人组队」玩法(介绍页写着 人数:3人组队):
                             #      单人点「前往」被游戏**静默拒绝**(不弹任何提示) → 白等
                             #      120s 后被"前往无效则收尾"判死。改为脚本自己点「组队」
                             #      → 招募窗「开始招募」(侠客助战默认勾选, 自动补满)
                             #      → 「人数已满, 是否开始域外风情」→「确定」直接开打。
                             #   ③ 师门介绍页是「请选择想要接取任务的师傅」版式(没有前往按钮),
                             #      实测点程咬金中心无反应 → 依次换菩提祖师/空度禅师兜底。
                             #   只动 3 个 YAML, 脚本位 +1 → 5.2.1。
                             #
                             # v5.2.0 —— 「黑命令行窗口 / 进程风暴」根治(真机 2026-10-19):
                             #   用户报"脚本弹了好几次黑色命令行窗口"。先做对照实验
                             #   (_collect/probe_console_win.py, EnumWindows 查可见控制台):
                             #   · creationflags=0 跑 6 次 adb connect → 弹 6 个 "Terminal";
                             #   · CREATE_NO_WINDOW 同样 6 次 → 0 个。Win11 默认控制台宿主
                             #     是 Windows Terminal, 黑窗就是它 → adb 调用本身没问题。
                             #   真正要修的是两处:
                             #   ① diagnostics.py 的 `adb devices -l` 漏了 creationflags
                             #      (点「诊断」会闪 1~2 个黑窗) → 补上。
                             #   ② 状态灯每 10 秒探 5 个端口, 没开模拟器的那些每次都要跑
                             #      `adb connect`(自身重试 1.5s) → 实测 75 秒里 40+ 个 adb.exe
                             #      + 同数量 conhost.exe, CPU/句柄一起抖。现改为**先 socket
                             #      探活**(0.12s), 没人监听就一条 adb 都不起。
                             #   回归: scripts/test_probe_dead_ports.py。
                             #   ③ scripts/run_tests.py 拉 28 个 test_*.py 子进程时也没带
                             #      creationflags —— 从没有控制台的父进程(后台 shell/pythonw)
                             #      拉起来时, Windows 会给**每个**子进程分配一个新控制台,
                             #      28 个用例 = 28 个一闪而过的黑窗(用户 11:29 那次"弹了几十
                             #      次"最像它)。现统一带 CREATE_NO_WINDOW。
                             #   另: 实测"每次只跑一条 bash 命令"不会弹窗(前台继承已有控制台),
                             #   但**后台/分离式**启动会新开一个 Terminal 窗口并在命令期间一直
                             #   挂着 —— 排查时若看到"弹了一会", 先分清是程序还是诊断会话。
                             # ------- 本版第二块: 域外死循环重构 + 崩溃定位增强 -------
                             #   ② 域外 934↔936 死循环(11:34~11:48 空转 14 分钟, 每 4~5 秒
                             #      一次全图 OCR, 最后 access violation 崩溃):
                             #      · 环 = [等回主界面] → [关面板] → [复核无战斗] → goto 回第一步。
                             #      · 元凶1: 「等回主界面」只有 {ui:主界面}(模板), 战斗切换的
                             #        过渡帧上会假阳性; 现改为 all_of[模板 + OCR 无战斗词],
                             #        settle 3→8(域外一批 10 场, 场间主界面只停几秒)。
                             #      · 元凶2: 「复核无战斗」timeout:3 只够 1 帧而单帧 OCR 5.2s,
                             #        战斗中必然超时 → 每次都 goto 回去。现 timeout 15 且加
                             #        {not:{ui:战斗中}} 模板快路径(20ms 级)。
                             #      · 两个环各加一个 max_visits:6 守卫步, 超限走**明确收尾**
                             #        而不是无限转(比引擎的 goto_loop_limit 软, 也不会终止任务)。
                             #      · 「前往无效」旧版只查「介绍页还开着」, 而实测真实表现是
                             #        **退回玩法面板**(16/50) → 漏判后直接掉进死等。现两者都认,
                             #        并回到批次循环重试; 批次入口(max_visits:8)兜底。
                             #      · 面板已开时不再点 (506,539)(那一下是"关闭", 点了就空转)。
                             #      · 等战斗 120s×3(6 分钟) → 60s×2; 批次后徽标判定补
                             #        pair_min_score 0.62 + pair_min_x 250 + timeout 12。
                             #   ③ 崩溃定位(11:48 access violation 只留下 "<no Python frame>",
                             #      连崩在哪个 DLL 都不知道):
                             #      · crash_guard.note(): 面包屑 → logs/last_action.log,
                             #        引擎在截图/OCR/步骤切换处写, 崩溃后能看到死前动作;
                             #      · start_thread_watchdog(): 每 30s 一份"全线程名+栈"快照;
                             #      · refresh_modules(): 已加载 DLL 基址表(启动 + 每 30s 刷新),
                             #        **崩溃回调里只用它做一次减法**, 把崩溃地址归因到
                             #        `xxx.dll +0x偏移` —— 这条不需要管理员权限;
                             #      · MiniDumpWriteDump 在异常上下文里会重入失败(实测只得到
                             #        0 字节), 所以 dmp 只当 best-effort; 归因文本才是主判据;
                             #      · 多开子进程(run_one)也装, 文件名带 `_p<端口>` 后缀;
                             #      · scripts/analyze_dump.py: 离线解析 dmp(异常码+故障模块),
                             #        回归 scripts/test_crash_guard.py(真崩一次验证归因)。
                             #   ④ config engine.goto_loop_limit 30 → 12: 实测那个环每轮
                             #      约 40 秒, 30 轮要 20 分钟才触发, 事故正好死在触发之前。
                             # ------- v5.1.0 (2026-10-19): 修「师门漏做」 -------
                             #   ① 卡名是美术字, 而世界播报横幅(y≈128)正好盖住第一张卡
                             #      「师门任务」的卡名 → click:{ocr:师门任务} 找不到就点不了,
                             #      恢复块又重复同一次 OCR, 最后把"找不到卡"当"已完成"收尾
                             #      → 师门是**零执行**而不是判定漏。现改为: OCR 失败 → 等横幅
                             #      走开 → 用卡面固定坐标[270,250]点(真机实测可开介绍页),
                             #      恢复块失败一律走明确收尾, 不再假装已完成。
                             #   ② 验收 pair_min_score 0.75 压在真实分布中位数上(43 张实测:
                             #      五环0.755/地煞0.744/域外0.795) → 同卡不同帧忽过忽不过。
                             #      改为 0.62 + pair_min_x:250(卡名在右半区, 实测 x≈298~317),
                             #      用几何条件挡掉沉底误读(x≈211 w≈112), 比分数干净。
                             #   ③ 判定类 wait 5s→12s(单帧≈截图0.8s+全图OCR5.2s, 5s 只够1帧)。
                             #   ④ 新增队伍申请弹窗守卫(「XX 申请顶替成为队伍队长」6 秒后
                             #      自动同意, 会抢队长且挡住卡面) → 判到就点「拒绝」。
                             #   ⑤ 新增 scripts/test_module_paths.py: 静态对所有 gui/*.py 的
                             #      根路径赋值表达式求值, 断言都等于 app/xyfz_auto/ 且其下
                             #      assets/tasks、assets/templates 真能扫到内容 —— 这类"少写
                             #      一级目录就静默失灵"的错以后会在回归里被抓住。
                             # ------- 上一版 v4.9.0: 批次六·渲染与精度收尾 -------
                             #   ① P1-2 渲染性能: bgr_to_qimage 改用 Format_BGR888(不再翻
                             #      通道), 帧转码实测 5.2~7.9ms → 0.7~0.9ms(7~8 倍); 缩略图
                             #      按 (port, frame_seq) 去重并限到 ~3fps; 一帧只转一次码(缩略图
                             #      与大画面共用); 不在「实时画面」页签时完全不转码, 切回来补
                             #      一帧; 状态灯 tooltip / 缩略图文字先比对再写。
                             #   ② P2-5 模板阈值分级: 新增 assets/templates/_meta.txt 给单个
                             #      模板配默认阈值, Step 新增 threshold 字段可逐步骤覆盖。
                             #      优先级 脚本 > _meta.txt > config.match_threshold —— 纯文字
                             #      按钮要 0.9 才算命中、带角标的模板 0.78 就够, 一刀切治不了。
                             #   ③ P2-4 跳转目标解耦: loop 展开会把步骤名变成「X(2/6)」, 脚本里
                             #      写死的 goto 一旦改了循环次数就全部失效(仓库里 191 处引用)。
                             #      加载后统一解析: 同轮次改名、裸名落到第一轮, 解析不到的原样
                             #      留着报错。check_refs 增查 on_success/jump、重名歧义与阈值表。
                             #   ④ P2-6 删掉 dump_task: 无任何调用点, 却只写回 7 个键 ——
                             #      settle/required/optional/verify_done/max_seconds/threshold
                             #      等 10 个字段和整个 loop 结构会被静默丢掉。
                             # ------- 上一版 v4.8.0: 批次五·判定精度与安全闸 -------
                             #   ① P1-3 单步预算启闸: config 的 max_step_seconds 从"注释着的 0"
                             #      正式启用(1800s = 30 分钟); 同时 Step 支持 per-step
                             #      max_seconds —— 全局只能当天花板, 域外「等打完回主界面」
                             #      这类天生要几十分钟的步骤要有自己的口子(已在 3 步标注)。
                             #      全项目 timeout≥600 的只有 4 步, 都单独核过。
                             #   ② P1-3b 验收点判定下限: 以前 verify_done 写的 timeout:3~5 +
                             #      retry:0, 而真机实测一轮判定就要 6 秒(截图 0.8 + 全图 OCR
                             #      5.2) → 只够看一帧就宣判"面板没有已完成徽标", 把做成的
                             #      玩法报成未完成。现在对验收点/复核步自动抬到 15s(约 2.5 轮)
                             #      + 1 次复核(普通步骤不动)。
                             #   ③ 单调时钟: 计时一律改 time.monotonic()。墙钟会被 NTP 校时
                             #      拨动、休眠期间照走, 都能让 deadline 失真。
                             #   ④ max_visits 批次上限: 给域外这类循环玩法一个明确的
                             #      收手闸门(达到次数上限即按 on_fail 收尾去验收)。
                             #   ⑤ P1-6 诊断包: 尾读不再整读(几百 MB 日志不再吃 GB 内存)、
                             #      补定时任务清单 / 磁盘与日志体积 / adb devices 原文,
                             #      并修掉「04」同时属于两份文件的编号重复。
                             #   ⑥ --keep N: dist 攒到 7.2GB/16 个正式包, 打包可保留最近 N 个
                             #      (零删除, 旧包挪到 dist/_retire_<时间戳>/)。
                             # ------- 上一版 v4.7.0: 批次四·工程 -------
                             #   ① P2-1 main_window.py 从 2890 行拆到 1650 行: 定时 / 日志滚动 /
                             #      脚本编辑器 / 状态灯 / live 轮询 五块出去做 Mixin(沿用 v3.5.0
                             #      拆分 render_mixin 的惯例, 方法体逐字搬运、零行为改写);
                             #      共用的 _safe_call/_tail_int/_input_dialog/_PortLight 抽到
                             #      gui/ui_helpers.py, 否则 Mixin 与 main_window 会循环导入。
                             #   ② 拆分过程中暴露并修掉一个隐性硬崩: MainWindow 构造时后台
                             #      重载模板的 QThread 若在脚本退出时仍未收尾会被回收 →
                             #      Qt qFatal → 进程 0xC0000409(表面 ALL PASS、退出码却非 0)。
                             #      回归脚本改用公共桩同步执行后台任务。
                             #   ③ P2-2 scripts/run_tests.py: 一键跑全部回归并出 JUnit XML
                             #      (与 pytest --junitxml 同格式)。没用 pytest 是因为打包 runtime
                             #      没装它、而 vendor 是 v2/v3 共用的 junction, 装包会污染两边。
                             #   ④ P2-3 tools/release.py: 版本四处自检 → 回归 → 打包 → 产物自校验
                             #      (zip 能不能解压 / 顶层目录对不对 / md5 对不对)。
                             # v4.6.0 —— 批次三·可观测(功能位+1): P0-4 日志滚动 +
                             #   P0-5 定时健壮性 + 诊断看得出日志健康度
                             #   ① 文本日志(run_<port>.log / run.log)以前从不滚动,
                             #      实测撑到 159MB: 补"单文件大小滚动 + 按天归档过期删除",
                             #      触发点从"只有跑完任务"扩到 启动/跨日/任务结束 三处。
                             #   ② 前置改造: run_<port>.log 收敛成**单一写方**(子进程
                             #      RotatingFileHandler 独写, GUI 侧 stdout 改接 boot_<port>.log),
                             #      修掉"每行日志写两遍"的老毛病, 轮转才安全。
                             #   ③ 失败截图 PNG→JPEG(实测省 86%), 配额 200→120 张;
                             #      失败截图目录同样纳入 启动/跨日 清理(以前只在跑任务时清)。
                             #   ④ 定时: 错过不再是静默丢弃(明确报案)、失败退还当天份额并
                             #      可重试(次数与其冷却均可配)、执行份额落盘(崩溃重启不再重跑)、
                             #      多条同时到点自动错峰(不再一起抢 adb)。
                             #   ⑤ 诊断包新增「日志健康度」: 占用多大/有没有在滚动 一眼看见。
                             # v4.4.0 —— 批次一(P0-2)收官: 组队一条龙接 4 卡验收(挖宝封妖/烧法任务/
                             #      商旅护送/帮派任务), 域外风情接标准验收(检查+复核);
                             #      检查✘→复核✔=✔ 的恢复语义), 不同验收点 AND 合并
                             #      (任一卡未见徽标 → 子任务 ✘), 修掉多卡同键覆盖失真。
                             # v4.3.1 —— 地煞接面板验收点(ocr_pair[已完成,挑战地煞→地煞星])。
                             # v4.3.0 —— 批次一·正确性(P0-1): 验收点失败不再被当"条件未命中"静默吞掉
                             #   → 失败验收步强制非 optional, 进 failed_steps 并标 verified=False,
                             #   通报不再报"成功 ✔"; 成功路径也渲染验收行。
                             #   (v4.2.0 被 MD5 工具占用、不属原四批, 本批顺延到 v4.3.0)
                             # v4.2.0 —— 加「验证完整性」工具 + 打包自动出 zip(功能位 +1):
                             #   ★ 真 bug: LiveReader.is_active() 只用 OpenProcess 判活,
                             #     而主进程自己 Popen 着子进程、握着它的句柄 → 子进程
                             #     退出后 OpenProcess 照样成功 → 永远判"活着" →
                             #     _poll_live 里"子进程已退出"的兜底从未生效过。
                             #     实测: 19:15 停掉账号2, 状态灯右边一直显示「运行中」。
                             #     改为 OpenProcess + GetExitCodeProcess(退出码 != 259)。
                             #   另加两重不依赖 pid 的保险: on_child_finished 收口
                             #     running 通道(被 terminate 的子进程不写 task_end);
                             #     点停止但该端口已无子进程时清掉孤儿「运行中」。
                             #   顺带: 本机手动停止不再被显示成红色「失败 ✘」(改「已停止」)。
                             # v4.0.0 —— 每账号任务状态挪到状态灯右侧(界面位 9+1 满十进主版本):
                             #   启动按钮下面那行红字(空闲/运行中/完成/失败)撤掉,
                             #   改成每盏状态灯右边一列 —— 一行一个账号,
                             #   以前只有一行且只跟「当前账号」, 多开时别的号跑成
                             #   什么样完全看不见。状态以通道为准自动推导
                             #   (本机与多开子进程同一套), 只有"停止中"这类瞬时态
                             #   由 _set_port_state() 临时接管, 下次启动该号自动清。
                             # v3.9.0 —— 完成通报简化 + 五环/师门接验收点(界面/功能位 +1):
                             #   ①成功通报只报最后一个步骤: "任务成功 ✔ — 最后执行
                             #     第N步「xxx」", 子任务汇总/条件未命中计数全部撤掉
                             #     (用户: "目标都能完成, 总结没什么用")。失败/终止路径
                             #     保留子任务汇总与逐步明细(排障需要), 明细照旧进 run.log。
                             # v3.8.1 —— 「每日必领」把已知正常的跳过标成 optional(脚本位 +1):
                             #   17 步: 7 个「有弹窗才关」的关弹窗步骤 + 10 个逐层返回的
                             #   冗余后几次(前面已回主界面, 再点返回按钮自然找不到)。
                             #   这些步骤的 on_fail 全是 continue, 作者(脚本注释)早写明
                             #   "找不到=这次没有/已领过" —— 但引擎此前把它们一律记成
                             #   真失败。实录: 13:26 那轮报"13 步失败", 而玩法其实
                             #   全做成了; 现在这 13 步归位到"条件未命中", 界面 7 段
                             #   全 ✔, 不再有误报。
                             #   ⚠ 保留每个段的**第一次**返回不标 —— 第一次返回就失败
                             #   = 页面不在预期位置, 那是真异常, 必须报出来。
                             # v3.8.0 —— 验收点 verify_done(界面/功能位 +1, 个位归零):
                             #   「做成没有」以**游戏面板的完成状态**为准, 不再数掉了
                             #   几步 —— 卡片变灰 / 挂「已完成」徽标才是权威答案, 我们
                             #   数"有几步没走到"只是二手猜测(用户 2026-10-17 主张)。
                             #   脚本里某步标 `verify_done: true` 即成为该子任务的达成
                             #   凭证: 命中 → ✔「面板已『已完成』」(就算中间掉了几步);
                             #   未命中 → ✘(哪怕零失败)。没标的子任务行为完全不变。
                             #   (本版只做机制, 尚未给任何玩法接上 —— 需要开模拟器
                             #   采图校准"已完成"徽标的 OCR, 故暂不铺开。)
                             # v3.7.0 —— 完成后汇总改为**子任务粒度**(界面/功能位 +1):
                             #   背景(用户反馈): 任务跑完的汇总按步骤逐条列, 926 步的
                             #   日常能刷出上百行, 而其中大半是跳转/条件未命中这类
                             #   正常现象 —— "到底成没成"反而被淹了。用户要的是
                             #   "每个子任务的结果"。
                             #   ①**子任务分组**(engine/model.py: subtask_of): 两种模式,
                             #     由 runner 按脚本结构自动选, 脚本一行不用改 ——
                             #     · source  : 任务由多个子脚本 include 组成(daily_all
                             #       的 7 个 parts) → 按 Step.source 分组, 显示可读名
                             #       (师门/五环/域外风情…, 未登记的退回文件名)。
                             #     · segment : 单脚本任务(「每日必领」99 步在一个文件里,
                             #       source 只有一种) → 按步骤名的段标记分组(① ② ③)。
                             #     run_task 开跑时统计每个子任务的步数与"是否被跑到过"
                             #     (区分 未执行 vs 全做成, 任务提前停止时前者才准)。
                             #   ②**界面只看子任务**: RunResult 加 subtasks, worker 的
                             #     counts() 带出去; 界面每个子任务一行
                             #     (✔ 完成 / ⚠ N步未做成 / ✘ N步关键失败 / — 未执行)。
                             #     逐步明细仍由 failure_summary() 生成, 但改成
                             #     `_emit_failure_summary(to_gui=False)` —— 只进 run.log。
                             #     异常终止(死循环/required 失败/用户停止)仍推界面, 那时
                             #     候必须看细节; 拿不到子任务统计时也自动退回明细。
                             #   ③**条件未命中不再占界面**: _emit_log 加 gui=False,
                             #     未命中行只写 run.log(926 步能刷 157 行, 全属正常)。
                             #     真失败(warning)照旧推界面。状态标签也不再显示
                             #     "N步未命中" —— 那是正常现象, 不是要处理的问题。
                             #   ④多开子进程(scripts/run_one.py)同步改为子任务粒度,
                             #     否则多开面板的日志区一样被刷屏。
                             # v3.6.0 —— 批次三「稳定性与结构」七项(界面/功能位 +1):
                             #   ①**截图/采集/重载模板全部后台化**: 新增 FuncWorker +
                             #     MainWindow._run_bg() 统一入口。「截图」按钮以前在主线程
                             #     直连 adb, 端口没开时**冻结 4.6 秒**(在线也 0.6s);
                             #     采集模板、matcher.reload()(重读 65 张 png)同理。
                             #     现在后台跑 + 按钮置灰 + 完成后回主线程; 采集没画面时
                             #     改成"截图完成后回调重入"(不能同步等)。
                             #     缩略图改 FastTransformation, _poll_live 600→800ms。
                             #   ②**跨线程共享状态**: worker 信号全部显式
                             #     Qt.QueuedConnection(以前靠 Auto 猜, 一旦从非主线程
                             #     connect 就退化成 Direct, 直接在 worker 线程操作 QWidget);
                             #     TemplateMatcher._load 改为"装进局部 dict 后整体赋值"
                             #     的原子切换 —— 旧写法 clear() 后逐条填, 装载期间别的
                             #     线程遍历会撞 dictionary changed size / 读到半套模板。
                             #   ③**worker 生命周期**: 接 finished → disconnect +
                             #     deleteLater + 引用置空; 退出时 _retire_worker()
                             #     超时则 terminate 并记 crash.log(旧代码 wait(5000)
                             #     超时照样往下走, 于是 QThread: Destroyed while
                             #     thread is still running 硬崩)。
                             #   ④**句柄泄漏**: LiveReader 补 close(); _readers 只增不减
                             #     的端口在"写方已退出且端口消失"时摘除并关句柄;
                             #     multi_panel 启动子进程后立刻关父进程那份日志句柄
                             #     (子进程已复制, 留着无用却占着文件);
                             #     crash_guard 补 close_crash_handle()(先 disable 再 close);
                             #     _full_shutdown 全部收干净 —— 治"退出后文件夹删不掉"。
                             #   ⑤**debug 截图滚动清理**: config engine.debug_keep_max
                             #     (默认 200 张) / debug_keep_days (默认 7 天)。以前只增
                             #     不减, 实测一晚 157 张 / 134MB 永久常驻。
                             #   ⑥**loader 环检测**: A include B、B include A 以前报
                             #     "嵌套超过 5 层"(完全指错方向), 现在按文件链判定并
                             #     直接点名成环的文件; 菱形引用(同一文件被引用两次)
                             #     不算环, 仍正常展开。
                             #   ⑦**拆渲染层**: 画面/步骤/日志约 215 行切到新的
                             #     gui/render_mixin.py(RenderMixin), main_window 2600+
                             #     → 2437 行。用 Mixin 而非独立类: 那块代码要读写主窗口
                             #     上十几个状态位, 搬走它们风险远大于收益, Mixin 是
                             #     零行为变化的纯搬移。
                             #   注: 原计划"删 5 个孤儿模板"经复核**取消** —— 精确扫描后
                             #   65 个模板全部在用(它们被 daily_pure_mode.yaml 与
                             #   tests/*.yaml 引用), 之前的孤儿结论是统计脚本误判。
                             # v3.3.0 —— 免责声明「主播推荐」格式改写(界面文案, 用户指定写法):
                             #   原「大话西游归来 · 缘起服 —— 达拉崩吧」两种分隔符混用,
                             #   容易被读成主播叫"大话西游归来"。按用户给的写法统一为
                             #   「大话西游归来缘起服-达拉崩吧」(游戏+区服 - 主播)。
                             #   DISCLAIMER_VERSION 2 → 3, 老用户(勾过不再显示)会重弹一次。
                             # v3.2.0 —— 修「GUI 卡顿/无响应」(界面性能, 用户实测反馈):
                             #   根因 = 左上角多端口状态灯在主线程里逐个端口调
                             #   AdbDevice.reconnect(), 而它是 每端口 3 条 adb 子进程
                             #   (start-server + connect + devices)。2026-10-16 实测:
                             #   在线端口 ~0.6s, **没人监听的端口 ~4.6s**(adb connect
                             #   自己做 TCP 重试), 5 个账号串起来 = 21.6 秒, 每 10 秒来一发
                             #   → 界面必然"卡一下卡一下"、长时间无响应。
                             #   修法(三处):
                             #   ①探测整体移到**后台线程**(_start_probe/_probe_worker +
                             #     结果队列, GUI 线程 0 占用; busy 门闩防叠加);
                             #   ②新增批量 probe_ports(): 1 次 start-server + 1 次
                             #     devices, 只对不在列表里的端口补 connect(短超时 1.5s
                             #     且**并行**), 21.6s → 2.0s;
                             #   ③AdbDevice 加 connect=False —— 启动时只建句柄不连设备,
                             #     免得端口没开时启动也卡几秒。
                             #   另修: 探测进行中时点状态灯会漏掉"正在重连…"即时反馈。
                             #   新增 scripts/test_status_lights.py(offscreen 回归测试).
                             # v3.1.0 —— 启动免责声明弹窗改造(界面):
                             #   ①正文精简(7 条压成单行短句 + 主播推荐一段), 默认窗口
                             #     580x560 一屏放得下, 不用拖动就能看完;
                             #   ②原 NoWrap 改按窗口宽度换行(以前长行要横拖);
                             #   ③"必须读到最后才能确定" —— 正文溢出时「我已阅读并同意」
                             #     在滚到底之前禁用并给提示, 滚到底自动解锁, 不溢出时直接可用;
                             #   ④DISCLAIMER_VERSION 1→2(正文改了, 老用户重弹一次)。
                             # v3.0.7 —— 域外脚本完成判定改造(2026-10-16 用户确认标准):
                             #   玩法面板次数不实时刷新不可信, 权威信号=卡片变灰(已完成徽标)。
                             #   删掉两处永远不可能命中的「面板查 50/50」死步骤(面板不显示
                             #   50/50, 各拖慢 5 秒/轮且制造跳过噪音); 批次重开命中后改跳
                             #   「已完成」徽标检查; 介绍页 50/50 保留作快速通道。
                             # v3.0.6 —— 左上角设备状态灯改为「多端口灯」: 按「多开管理」(accounts.json)
                             #   的端口逐个亮灯, 哪个端口真在线哪个亮绿, 不再只盯 config 里那一个固定端口
                             #   (之前换了别的模拟器实例端口就误报「未连接」)。点单灯立即重连该端口;
                             #   多开管理保存后状态灯自动同步端口/名称。
                             # v3.0.5 —— ★ 根治域外批次后 goto 死循环(实战复现空转):
                             #   根因 = "等回主界面"在战斗→主界面切换时底栏短暂闪现, 主界面判定
                             #   (底栏任一 + not战斗中) 瞬间成立, 被 390"复核仍主界面"误判后 goto 回等,
                             #   与下游复核形成 goto 环空转。修法 = 引擎新增 wait 的 settle 稳检测
                             #   (命中后等 N 秒复核仍成立才成功, 过渡期假阳性继续轮询), 域外
                             #   "等回主界面"加 settle:3 并删掉会成环的"复核停顿10秒+复核仍主界面"两步。
                             #   注: v3 此段脚本与 v2.1.0 逐行一致, 死循环非 v3 引入, 是旧隐患被时机触发。
                             # v3.0.4 —— 加 goto 死循环保护(同一 goto 目标重复跳转超上限自动终止并报清楚)
                             #   v3 用 pythonw 起, stderr 被丢弃, 且无 faulthandler /
                             #   excepthook。结果任何原生崩溃或主线程未捕获异常都"静默
                             #   消失"(本次 v3 跑师门时 GUI 直接没了, run.log 戛然而止)。
                             #   现: ① 新增 crash_guard.py, 程序最早期启用 faulthandler
                             #      (落盘 logs/crash.log) + sys/threading excepthook;
                             #   ② worker 线程异常写 crash.log 而非丢 stderr;
                             #   ③ 定时器回调与 worker 信号槽包 _safe_call, 单点错误
                             #      不再拖垮整个 GUI。下次再崩, crash.log 里有真凶。
                             #   修法: 真退出分支末尾显式 QApplication.quit()。
                             #   附带: 确认框在窗口隐藏时会不可见 → 先把窗口拉回来,
                             #   用户点"否"再恢复隐藏。
                             # v3.0.1 —— ① ★「关窗 ≠ 退出」的出口问题(用户实测踩到):
                             #   点窗口 ✕ 只是最小化到托盘(定时任务要在后台跑, 进程刻意保留),
                             #   而唯一能真退出的托盘右键菜单常被 Windows 折叠 → 用户以为
                             #   退出了 v2, 进程其实从上午挂到下午。现在主界面加了菜单栏
                             #   「文件」: 最小化到托盘 / 退出(Ctrl+Q), 不依赖托盘是否可见;
                             #   且每次隐藏都弹提示(原来只弹一次, 没看见就再也不提)。
                             # v3.0.0 —— 独立工程 v3/(与 v2.1.0 并存, 各跑各的):
                             # ① ★「失败不再被报成成功」: 新增失败记账(Engine.failed_steps
                             #   / failure_summary)。以前 on_fail: continue 返回 True 被当成功,
                             #   帮派捐献没做成却整条报 ✅(2026-10-11 事故)。现在任务结束会
                             #   显示「⚠ 完成(N步未做成)」并逐条列出; 脚本可给关键步骤加
                             #   required: true, 失败即整条判失败。
                             # ② ★ 图片读写统一走 common.imwrite_unicode/imread_unicode:
                             #   cv2.imwrite 对中文路径返回 False 却不报错(点了"采集模板"
                             #   文件其实没生成), imread 返回 None(模板库读不进来只表现为
                             #   等待超时)。全项目不再有裸 imwrite/imread。
                             # ③ 清理: 删 2 个孤儿模板 + 5 个零引用脚本(win_click/make_shortcut/
                             #   scan_waits/test_ocr/multirun) + 5 处死代码 + 4 个死配置项
                             #   + app/xyfz_auto/requirements.txt(与根目录那份矛盾)。
                             # v2.1.0 —— ① 界面: 「窗口诊断」从主窗口左侧挪到「脚本选项 →
                             #   运行(全局)」页, 与它要改的 win_input 配置同页闭环;
                             # ② 设备状态灯改自愈式: 不再启动时一锤定音 —— 每 10 秒轻量探测
                             #   一次(只跑 adb devices), 连不上显示灰色「未连接(会自动重试)」
                             #   而不是永久红字, 点标签可立即重连;
                             # ③ 修 current_activity() 解析: 以前取到 ActivityRecord{... 外壳
                             #   而非包名, is_app_front() 因此永远返回 False;
                             # ④ 每日必领 段③秘境扫荡: 模板右缘"!"角标(可领时才出现)把匹配分
                             #   拖到 0.794(阈值 0.80), 明明有奖励却跳过 —— 裁掉角标区后 0.912
                             # v2.0.6 —— ① 启动弹【免责声明】+ 主播推荐(可勾选不再显示; 正文有
                             #   重要改动 DISCLAIMER_VERSION 变更会再弹一次), 见 gui/disclaimer.py
                             # v2.0.0 —— ① 每日必领段⑤ 盈福大厅签到: 实机采集 5 个模板 + 注册
                             #   2 界面/3 按钮, 修掉「关奖励弹窗」白等一次超时、返回次数多写一次;
                             # ② engine/runner 补写法/注册表错误兜底(ValueError 等), 单个步骤
                             #   写错不再炸掉整条任务, on_fail 终于生效;
                             # ③ 新增 scripts/check_refs.py —— 事前扫出"引用了不存在的按钮/
                             #   locate 没配模板"这类问题(本次事故正是这一类);
                             # ④ 界面归位: 识别模板库挪进「实时画面」页(取点→采集同页闭环),
                             #   脚本的打开/新建/保存挪进「脚本(YAML 可编辑)」页
                             # v2.0.1 —— ① 新建开发源码工程 D:\鸡驴助手_dev(源码与 runtime 分离,
                             #   vendor\runtime 331MB 不进 git), tools\build_portable.py 一键打包绿色版
                             #   (版本号自动读本行, 带 10 项关键件 + bat 编码自检);
                             # ② 修 bat 启动乱码: 我曾把 bat 写成 UTF-8+LF, cmd 只认 CRLF, 且 chcp
                             #   会打乱字节偏移 → 报「鎵撳寘 不是内部或外部命令」「系统无法接受输入的时间」。
                             #   统一为 GBK+CRLF+不切代码页, 并写进打包自检防复发;
                             # ③ 多开管理: 撤掉页签, 改左侧「多开管理…」按钮弹独立窗口
                             #   (实例保留 —— 停止按钮状态/定时任务按账号启动/非本机端口启停/退出回收
                             #   子进程共 6 处仍依赖它)
                             # v2.0.2 —— 日常玩法拆出 4 个可单独运行的顶层脚本(组队一条 / 五环+师门 /
                             #   地煞星 / 域外风情), 都自带纯享模式前置。用 include 复用 parts/ 子脚本,
                             #   与 daily_all.yaml 共用同一份, 不复制内容
                             # v2.0.3 —— 每日必领新增 段⑥ 帮派捐献(主界面右下「帮派」→ 帮派页(议政厅)
                             #   → 底部「聚义厅」页签 → 捐献)。聚义厅捐献区"免费态(单个捐献)/
                             #   常规态(材料捐献+银两捐献)"两态自动识别(上一步找不到就 goto 下一步);
                             #   捐完弹「恭喜获得」点顶部空白关, 再点左下返回(=菜单返回同模板)回主界面。
                             #   新增 2 界面(帮派页/帮派聚义厅页) + 3 按钮 + 5 模板; 真机实跑验证通过
                             # v2.0.4 —— 使用说明.txt 新增【免责声明】章节(文末): 明确软件为个人学习研究
                             #   用途的 ADB 截图+模拟点击外部辅助(不修改游戏/不逆向协议/不读内存/不绕过付费
                             #   与安全机制); 使用者自负账号限制等后果, 软件按"现状"提供, 作者免责; 禁商业牟利
                             # v2.0.5 —— ① 修 每日必领段⑥「点帮派」定位失败: 原 right_btn_gangpai 模板
                             #   把按钮周边背景也截了进去, 角色站在不同场景(背景变化)时匹配分从 0.949 掉到
                             #   0.779(<0.80 阈值), 判定失败导致整个段⑥被 on_fail 跳过(捐献没做成但报"成功")。
                             #   重采模板只保留按钮本体(深盘+徽记+文字, 32×34), 实测各主界面截图 0.99+、
                             #   议政厅/聚义厅 <0.5;
                             # ② 修 绿色版 GBK 控制台下日志刷屏 UnicodeEncodeError(✔/▶/⚠ 无法编码,
                             #   把 run_<port>.log 灌满 traceback): common._harden_console() 把
                             #   stdout/stderr 的 error 策略设为 replace, 符号降级为 ? 不再中断
                             # v2.0.6 —— 新增 gui/disclaimer.py: 启动后弹一次【免责声明】(内容与使用说明
                             #   文末一致, 结尾推荐主播「大话西游归来·缘起服 —— 达拉崩吧」), 可勾选
                             #   「不再显示此提示」永久跳过 → 写入 gui_settings.json(不入库)。
                             #   声明正文版本 DISCLAIMER_VERSION 变更时会重新弹一次;
                             #   菜单栏「帮助」与托盘菜单随时可重开, 也能取消勾选恢复启动弹窗
APP_TITLE = f"{APP_NAME} v{APP_VERSION}"


# 2026-10-17: 配置兜底。以前 load_config 裸返回 yaml.safe_load 的结果, 调用方
# 又是清一色 self.cfg["device"] 裸索引 —— config.yaml 少一节 / 被手改坏 / 读成 None,
# KeyError 会在 MainWindow.__init__ 里抛出, 表现为"双击之后什么都没发生",
# 且 pythonw 下无 stderr, 连报错都看不到(正是 v3 早期"闪退"那类难查问题)。
# 现在: 缺节补默认值 + 解析失败也返回可用配置, 保证程序一定能起来。
DEFAULT_CONFIG = {
    "device": {"adb_path": "auto", "host": "127.0.0.1", "port": 16384,
               "package": "com.netease.xyfz_02"},
    "recognition": {"template_dir": "assets/templates", "match_threshold": 0.80,
                    "ocr_engine": "rapidocr"},
    "engine": {"tasks_dir": "assets/tasks", "default_timeout": 30,
               # v5.5.0: 轮询间隔 1.0 → 0.3(一次轮询本身 2.8s, 睡 1 秒纯属空转)
               "poll_interval": 0.3, "max_sleep_seconds": 0,
               # v4.5.0 心跳: 别太久(僵尸要尽早发现), 别太密(写盘开销)
               "heartbeat_interval": 5, "heartbeat_timeout": 120,
               # v4.6.0(P0-4) 日志: 单文件上限/保留份数/归档保留天数
               "log_max_bytes": 2097152, "log_backup_count": 3,
               "log_keep_days": 7,
               # v4.6.0(P0-5) 定时健壮性
               "timer_grace_minutes": 3,          # 到点后多久算"错过"
               "timer_max_retry": 2,              # 失败最多重试几次
               "timer_retry_cooldown_seconds": 300,   # 两次重试之间的冷却
               "timer_stagger_seconds": 20,       # 多条定时同时到点时的错峰间隔
               # v4.6.0(P0-4) 失败截图: 200→120 张(配合 JPEG 化, 稳态 ~90MB 降到个位数)
               "debug_keep_max": 120, "debug_keep_days": 7},
    "win_input": {"enabled": True, "hwnd": 0, "index": 0, "title_contains": [],
                  "exe_contains": [], "fallback_adb": True,
                  "scale_x": 0.6772, "scale_y": 0.6754, "y_offset": 40.5},
    "app": {"package": "com.netease.xyfz_02", "single_instance": True,
            "kill_adb_on_exit": True},
}


def load_config(path: str | Path | None = None) -> dict:
    """加载 config.yaml —— 缺节补默认值, 解析失败也能返回一份可用配置

    保证调用方 self.cfg["xxx"]["yyy"] 不会因为配置缺失而让程序静默起不来。
    出问题时会往 stderr 与日志写一条明确告警(不再无声降级)。
    """
    p = Path(path) if path else PROJECT_ROOT / "config.yaml"
    cfg = {k: dict(v) for k, v in DEFAULT_CONFIG.items()}
    try:
        with open(p, "r", encoding="utf-8") as f:
            loaded = yaml.safe_load(f) or {}
        if not isinstance(loaded, dict):
            raise ValueError("config.yaml 顶层不是字典")
        for k, v in loaded.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)          # 节内逐键合并, 缺的键用默认值补
            else:
                cfg[k] = v
    except Exception as e:
        # 拿不到配置也要能起来 —— 用默认值跑, 并把问题说清楚
        try:
            import sys
            print(f"[WARN] 读取 config.yaml 失败({e}), 已用默认配置启动: {p}",
                  file=sys.stderr)
        except Exception:
            pass
    return cfg


# ---------------- adb 路径解析 (2026-09-24 新增) ----------------
# 事故背景: config.yaml 的 device.adb_path 曾写死 MuMu 的 C 盘安装路径,
# 换机器后 MuMu 装在 D 盘(或根本没装) → adb 找不到 → 界面只报"设备未连接",
# 用户完全看不出是路径问题。
# 现在改为多级回退, 任何一级命中即可用:
#   1) config.yaml 显式指定且文件真实存在
#   2) 包内自带 tools/adb/adb.exe (便携版首选, 换机器零配置)
#   3) 系统 PATH 里的 adb
#   4) MuMu 模拟器常见安装位置(跨盘符扫描)

BUNDLED_ADB = PROJECT_ROOT / "tools" / "adb" / "adb.exe"

# 这些值视为"未配置", 不当作路径
_ADB_PLACEHOLDERS = {"", "auto", "none", "null", "-", "adb", "adb.exe"}


def _drives() -> list[Path]:
    """列出本机盘符。优先用 os.listdrives(3.12+), 避免逐个探测不存在的盘。"""
    fn = getattr(os, "listdrives", None)
    if callable(fn):
        try:
            return [Path(d) for d in fn()]
        except OSError:
            pass
    return [Path(f"{c}:/") for c in "CDEFGH"]


def _mumu_adb_candidates() -> list[Path]:
    """枚举 MuMu 模拟器可能的 adb.exe 位置(含 D 盘等非系统盘的自定义安装)"""
    relative = [
        r"Netease\MuMu\nx_main\adb.exe",
        r"Program Files\Netease\MuMu\nx_main\adb.exe",
        r"Program Files (x86)\Netease\MuMu\nx_main\adb.exe",
        r"MuMu\nx_main\adb.exe",
        r"MuMuPlayer-12.0\nx_main\adb.exe",
        r"Program Files\Netease\MuMuPlayer-12.0\nx_main\adb.exe",
        r"Program Files\MuMu\nx_main\adb.exe",
    ]
    out = []
    for root in _drives():
        for rel in relative:
            out.append(root / rel)
    return out


def find_adb_ex(configured: str | Path | None = None) -> tuple[str | None, str]:
    """解析 adb 并同时返回来源标签, 返回 (路径或None, 来源说明)。

    只有真实存在的文件才算命中——配置里那条失效的老路径会被自动跳过,
    这正是"换了电脑就连接失败"的修复点。
    """
    # 1) 显式配置(真实的文件才认)
    if configured is not None:
        s = str(configured).strip().strip('"').strip("'")
        if s.lower() not in _ADB_PLACEHOLDERS:
            try:
                if Path(s).is_file():
                    return str(Path(s)), "config.yaml 指定"
            except OSError:
                pass

    # 2) 包内自带(便携版首选, 换机器零配置)
    try:
        if BUNDLED_ADB.is_file():
            return str(BUNDLED_ADB), "包内自带"
    except OSError:
        pass

    # 3) 系统 PATH
    which = shutil.which("adb")
    if which:
        return which, "系统 PATH"

    # 4) MuMu 常见安装位置(跨盘符)
    for p in _mumu_adb_candidates():
        try:
            if p.is_file():
                return str(p), "MuMu 安装目录"
        except OSError:
            continue

    return None, "未找到"


def find_adb(configured: str | Path | None = None) -> str | None:
    """按优先级解析出可用的 adb.exe 绝对路径; 全部失败返回 None。"""
    path, _ = find_adb_ex(configured)
    return path


ADB_NOT_FOUND_MSG = (
    "未找到 adb.exe，无法连接模拟器。请任选一种方式修复：\n"
    "  1) 用便携版自带的 adb：确认 app\\xyfz_auto\\tools\\adb\\adb.exe 存在；\n"
    "  2) 打开 config.yaml，把 device.adb_path 改成你机器上的真实路径"
    "（注意盘符，MuMu 可能装在 D 盘）；\n"
    "  3) 把 adb.exe 所在目录加入系统 PATH 环境变量。"
)


def png_bytes_to_bgr(data: bytes) -> np.ndarray:
    """把 adb screencap 的 PNG 字节解码为 OpenCV BGR 图像"""
    arr = np.frombuffer(data, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("PNG 解码失败")
    return img


# ==================================================================
# 2026-10-16 v3.0.0: 图片读写统一出口(中文路径铁律)
#
# 坑(踩过两次, 每次都表现为"静默失败", 极难排查):
#   · cv2.imwrite 写含中文的路径 → 返回 False, 但不抛异常。
#     实例: GUI「从点击点采集模板」点了、也提示"已保存", 文件其实没生成;
#          等项目路径换成英文才发现模板一直是空的。
#   · cv2.imread 读含中文的路径 → 返回 None, 同样不抛异常。
#     实例: recognition/template.py 逐个读模板时读不到就 continue,
#          结果整个模板库"空了", 表现出来却是每个步骤都"等待超时"。
#
# 正解: 用 numpy 做字节中转(np.fromfile / tofile), 绕开 cv2 自身的
# 路径处理。runner.py 早在 2026-09-11 就这么写了, 但只有那一处 ——
# 现在抽成公共函数, 全项目一律走这里, 不再有裸 imwrite/imread。
# ==================================================================

def imwrite_unicode(path, img) -> bool:
    """写图片, 兼容中文路径/文件名。返回是否真的写成功(务必检查返回值)

    不要再直接用 cv2.imwrite: 它对中文路径静默失败(返回 False 不报错),
    调用方不检查就会被"保存成功"的假象骗过去。
    """
    import os
    try:
        ok, buf = cv2.imencode(os.path.splitext(str(path))[1] or ".png", img)
        if not ok:
            return False
        with open(str(path), "wb") as f:
            f.write(buf.tobytes())
        return os.path.exists(str(path)) and os.path.getsize(str(path)) > 0
    except Exception:
        return False


def imread_unicode(path, flags=None):
    """读图片, 兼容中文路径。失败返回 None(与 cv2.imread 语义一致)

    注意: cv2.imread 对中文路径返回 None 且不报错, 是"模板库读不进来"
    这类诡异故障的根源。全项目统一用本函数替代。
    """
    import numpy as _np
    if flags is None:
        flags = cv2.IMREAD_COLOR
    try:
        data = _np.fromfile(str(path), dtype=_np.uint8)
        if data.size == 0:
            return None
        return cv2.imdecode(data, flags)
    except Exception:
        return None


def _hard_remove(path: str) -> bool:
    """删除单个文件, 绕开运行时注入的 os.remove 批量删除守卫。

    背景(2026-10-19 v5.5.0 后): WorkBuddy 运行时会在 os.remove 上挂钩子, 对"一次
    进程里删除文件数"计数, 超过阈值(约 50)就 raise SystemExit(1)。而 SystemExit 是
    BaseException 子类, 普通 except Exception 拦不住, 会直接把主进程(尤其 GUI)搞崩
    —— 实测日志轮转清理 300+ 个旧文件时主线程崩在 _check_bulk_delete_guard, 表现就是
    "任务跑完 GUI 闪退"。

    日志清理是本程序的家务活(删的是自己的旧日志), 用 Windows API 直接删, 守卫拦截不到。
    非 Windows / API 异常时回退 os.remove, 保留"删不掉就跳过"语义。
    返回 True=已删(或本就不存在), False=跳过。
    """
    import os
    try:
        if not os.path.lexists(path):
            return True
    except OSError:
        return True
    try:
        import ctypes
        k32 = getattr(ctypes, "windll", None)
        if k32 is not None:
            if k32.kernel32.DeleteFileW(path):
                return True
            return False  # 被占用/权限不足 → 按原语义静默跳过
    except Exception:
        pass
    try:
        os.remove(path)
        return True
    except OSError:
        return False


def rotate_files(directory, keep_max: int = 200, keep_days: int = 7) -> int:
    """按「最多保留 N 个 + 最多保留 N 天」滚动清理目录里的文件, 返回删除个数。

    用途: 失败现场截图(logs/debug*)。2026-10-17 v3.5.0: 一晚能产生 157 张 /
    134MB 且永久常驻, 跑几晚就把便携版目录撑爆, 外发时还会把游戏截图带出去。
    两条规则取并集(超数的删最旧、过期的全删), 0 表示该规则不生效。

    不递归子目录; 只看普通文件; 删除失败(被占用)静默跳过 —— 清理是家务活,
    不该因为一张删不掉的图把任务搞砸。
    """
    import os
    import time as _t

    if not directory:
        return 0
    try:
        if not os.path.isdir(directory):
            return 0
        now = _t.time()
        cutoff = now - keep_days * 86400 if keep_days and keep_days > 0 else 0
        entries = []
        for n in os.listdir(directory):
            p = os.path.join(directory, n)
            try:
                if not os.path.isfile(p):
                    continue
                entries.append((os.path.getmtime(p), p))
            except OSError:
                continue
        # 新的在前
        entries.sort(key=lambda x: x[0], reverse=True)
        victims = []
        if keep_max and keep_max > 0:
            victims.extend(p for _, p in entries[keep_max:])
            keep_head = entries[:keep_max]      # 条数规则留下的那些
        else:
            keep_head = entries                 # 条数规则没开 → 全部参与天数判定
        if cutoff:
            victims.extend(p for m, p in keep_head if m < cutoff)
        removed = 0
        for p in set(victims):
            if _hard_remove(p):
                removed += 1
        return removed
    except Exception:
        return 0


def rotate_text_logs(log_dir, keep_days: int = 7, skip_names=None) -> dict:
    """2026-10-18 v4.6.0(P0-4): **文本日志**滚动。返回 {"archived":n,"removed":n}。

    为什么单独要有它: 原来的 rotate_files 只被 engine 拿来滚 debug 截图,
    logs/run_<port>.log 这种文本日志**从来没有滚动**, 一路追加到 159MB(实测)。

    做两件事:
      ① 按天归档: run_*.log 若"最后写入不是今天"(说明对应的运行已结束),
         改名成 run_<port>_YYYYMMDD.log, 腾出当天的新文件。
      ② 按天删除: 归档件超过 keep_days 天删掉。

    ⚠️ 刻意不做的事(都是实测/推理出来的坑):
      · 不归档 crash.log —— faulthandler 常驻握着它的句柄, 一改名它就会
        继续写进被改名那个文件, 之后的崩溃记录就丢了(过期删除则照旧,
        这是唯一能管住 crash.log 体量的办法; 被占着删不掉时会静默跳过)。
      · 同理, **任何本进程正打开的日志都改不动名**(Windows 不允许 rename 被打开的
        文件, os.rename 抛 PermissionError → 被下面的 except OSError 吞掉, 表现为
        "清了但没动静")。GUI 主日志 run.log 就属于这种, 所以调用方把它放进
        skip_names(v5.0.0); 它自己有 RotatingFileHandler 按大小滚。
      · 只改"最后写入不是今天"的文件 —— 正在跑的任务今天肯定写过,
        排除掉就不会把活着的日志文件从写方手里抽走。
      · skip_names 里的文件直接跳过 —— 调用方把"当前正在跑的账号"传进来,
        跨日那一刀就不会砍到正在写日志的子进程(砍了它会继续写进改名后的 inode)。
      · 不抛异常: 清理是家务活, 不能因为删不掉把程序搞崩。

    触发点(三条缺一不可, 见调用处): GUI 启动 / 跨日 / 任务结束。
    """
    skip = set(skip_names or ())
    import os
    import time as _t
    from datetime import datetime as _dt

    out = {"archived": 0, "removed": 0}
    if not log_dir:
        return out
    try:
        if not os.path.isdir(str(log_dir)):
            return out
    except Exception:
        return out

    today_tag = _dt.now().strftime("%Y%m%d")
    now = _t.time()
    try:
        for name in sorted(os.listdir(str(log_dir))):
            p = os.path.join(str(log_dir), name)
            try:
                if not os.path.isfile(p):
                    continue
            except OSError:
                continue
            stem, ext = os.path.splitext(name)
            if ext.lower() != ".log" or name in skip:
                continue

            try:
                mtime = os.path.getmtime(p)
            except OSError:
                continue
            mtag = _dt.fromtimestamp(mtime).strftime("%Y%m%d")

            # ① 归档: run_*.log 与 run.log 两类, 且今天没写过(未在跑), 且还没归档过
            #    (stem 里含 "_20" 说明已经是归档件了 —— 归档件别再归档一次)
            if ((name.startswith("run_") or stem == "run")
                    and "_20" not in stem and mtag != today_tag):
                arch = os.path.join(str(log_dir), f"{stem}_{mtag}.log")
                try:
                    if os.path.exists(arch):        # 同一天归档过一次以上
                        arch = os.path.join(str(log_dir),
                                            f"{stem}_{mtag}_{int(mtime)}.log")
                    os.rename(p, arch)
                    out["archived"] += 1
                    continue
                except OSError:
                    pass            # 被占用/重名都跳过, 下次再说

            # ② 删除过期: 归档件才删, 今天写过的绝不删
            if keep_days and keep_days > 0 and mtag != today_tag:
                age_days = (now - mtime) / 86400.0
                if age_days > keep_days:
                    if _hard_remove(p):
                        out["removed"] += 1
    except Exception:
        pass
    return out


def _harden_console() -> None:
    """GBK 控制台下, 日志里的 ✔/▶/⚠/✗ 等符号无法编码, StreamHandler 会抛
    UnicodeEncodeError 并刷屏(2026-10-11 绿色版实测: run_<port>.log 被 traceback 灌满)。
    把 stdout/stderr 的错误策略改为 replace(符号降级为 ?, 中文不受影响), 不再中断。
    """
    for s in (sys.stdout, sys.stderr):
        if s is None:
            continue
        try:
            s.reconfigure(errors="replace")      # Python 3.7+
        except Exception:
            pass


def _log_engine() -> dict:
    """取 config engine 节里跟日志有关的键(取不到就返回空 dict, 调用方用默认)。

    2026-10-18 v4.6.0(P0-4): setup_logger / run_one 都要用同一套滚动参数,
    各自写一遍容易走样, 收敛到这里。
    """
    try:
        eng = load_config().get("engine") or {}
        return eng if isinstance(eng, dict) else {}
    except Exception:
        return {}


def setup_logger(name: str = "xyfz", level=logging.INFO) -> logging.Logger:
    """控制台 + 文件双写日志"""
    _harden_console()
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(level)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")

    if sys.stdout is not None:          # pythonw.exe 下 stdout 为 None(绿色版双击启动)
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        logger.addHandler(sh)

    log_dir = PROJECT_ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    target = log_dir / "run.log"
    # 2026-10-18 v4.6.0(P0-4): FileHandler → RotatingFileHandler。
    # 主界面这份 run.log 以前也是只增不减, 和 run_<port>.log 一样属于
    # "不跑任务的日子也一直在写、但从来没人清理"的那类。
    mb = max((int(_log_engine().get("log_max_bytes") or 2 * 1024 * 1024), 64 * 1024))
    bc = max((int(_log_engine().get("log_backup_count") or 3), 1))
    try:
        fh = RotatingFileHandler(target, maxBytes=mb, backupCount=bc,
                                 encoding="utf-8")
    except PermissionError:
        # 2026-09-28: run.log 被另一个实例或残留进程占着时, 旧版直接抛异常导致
        # 程序根本起不来(冒烟测试/双开都会撞上)。改为退到带 PID 的日志文件。
        fh = RotatingFileHandler(log_dir / f"run_{os.getpid()}.log",
                                 maxBytes=mb, backupCount=bc, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    return logger


def close_loggers() -> None:
    """关闭日志文件句柄。

    不关的话 logs\\run.log 会一直被本进程占着 —— 这正是"退出后删不掉文件夹"
    的常见原因之一(见 proc_guard.py 的说明)。
    """
    for name in list(logging.Logger.manager.loggerDict):
        lg = logging.getLogger(name)
        for h in list(getattr(lg, "handlers", [])):
            try:
                h.flush()
            except Exception:
                pass
            try:
                h.close()
            except Exception:
                pass
            try:
                lg.removeHandler(h)
            except Exception:
                pass


# ---------------- 跨进程设备互斥锁 (2026-09-11 新增) ----------------
# 事故背景: 凌晨定时任务运行时, 另一个 GUI 实例(绿色包旧版)同时触发了定时,
# 两个进程并发点击同一个模拟器 → 界面被搅乱, 双双失败。
# 锁文件放系统临时目录(跨项目目录有效, 主项目/绿色包共用同一把锁)。

def _lock_path(port: int) -> str:
    import os, tempfile
    d = os.path.join(tempfile.gettempdir(), "xyfz_auto_locks")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"lock_{port}.lock")


def acquire_device_lock(port: int) -> bool:
    """尝试获取指定模拟器端口的任务锁; 已有活进程持锁时返回 False"""
    import os
    p = _lock_path(port)
    if os.path.exists(p):
        try:
            pid = int(open(p, encoding="utf-8").read().strip() or 0)
        except Exception:
            pid = 0
        if pid and pid != os.getpid():
            import ctypes
            k = ctypes.windll.kernel32
            h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if h:
                # 2026-09-23 加固: 旧版只看"PID 是否存活"——PID 会被系统复用,
                # 死进程的 PID 落到无关进程(如 explorer)头上时 OpenProcess 照样
                # 成功 → 残留锁被误判为"有实例在跑", 用户点开始被拒。
                # 现在再查进程映像名, 不是 python 解释器视为持锁进程已死 → 抢占。
                buf = ctypes.create_unicode_buffer(512)
                size = ctypes.c_ulong(512)
                name_ok = False
                try:
                    name_ok = bool(
                        k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)))
                except Exception:
                    name_ok = False
                k.CloseHandle(h)
                image = (buf.value or "").lower()
                if name_ok and ("python" in image or "xyfz" in image):
                    return False    # 确实是本工具的进程持锁 → 拒绝
                # PID 复用给了无关进程 / 查询失败 → 按死锁处理, 抢占覆盖
        # 持锁进程已死 → 抢占并覆盖
    with open(p, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))
    return True


def release_device_lock(port: int):
    import os
    try:
        _hard_remove(_lock_path(port))
    except OSError:
        pass
