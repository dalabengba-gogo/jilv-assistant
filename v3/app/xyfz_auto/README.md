# 鸡驴助手 (xyfz_auto)

一个 ALAS 式的 MuMu 模拟器游戏自动化框架：**截图识别 → 点击 → 再识别** 的可编辑脚本循环。

> **当前版本：v6.1.4**（2026-10-26）
> 改版本号须同步四处：`common.py` 的 `APP_VERSION`（唯一权威）、`__init__.py` 的
> `__version__`、本文档本行、`使用说明.txt`【版本历史】首条。
> 2026-10-18 起打包自检**真的会校验这四处**（以前文档这么写、脚本却只校验两处，
> 于是本文档长期停在 v3.9.0 没人发现 —— 现已补进校验清单）。

## 界面可调选项（脚本选项页）

GUI 右侧「脚本选项」页：**左侧先选脚本，右侧只显示该脚本自己的选项**，
改动即时写入 `config.yaml`，下轮任务生效。左侧列出当前任务里的所有脚本
（按 include 顺序；有可调选项的标 ●），外加一个「运行(全局)」组。

当前已声明的选项：

| 脚本 | 选项 | 配置键 | 取值 |
|---|---|---|---|
| 运行(全局) | 单步时间上限 | `engine.max_step_seconds` | 0 = 不限；>0 = 整步共享预算(含重试/复核) |
| 运行(全局) | 固定停顿上限 | `engine.max_sleep_seconds` | 0 = 不限；>0 = 压缩休眠 |
| 运行(全局) | 失败截图保留(张) | `engine.debug_keep_max` | 0 = 不限；超了删最旧的(另按 7 天过期) |
| 运行(全局) | Windows 鼠标通道 | `win_input.enabled` | true / false |
| 地煞星×3 | 挑战星级 | `dixia.star_row` | `auto` / `row1`~`row4` |

### 给自己的脚本加选项（新增脚本时照抄）

在脚本（或子脚本）yaml 里加一段 `options:`，界面左侧就会自动多出这个脚本
（并标 ●），**不用改 GUI 代码**：

```yaml
options:
  script: parts/my_task.yaml   # 归属脚本(可省, 省略=本文件); 选项块也可以
                               # 集中写在别的文件里, 用它指明服务谁
  title: 我的玩法              # 左侧列表与表单标题
  desc: 一句话说明（可选）
  items:
    - key: my_mode             # 写进 config.yaml 的键
      section: my_task         # 写进哪个节
      label: 模式              # 表单行标题
      type: choice             # choice | bool | int
      default: "fast"          # 缺省值（config 缺键时用它兜底，脚本照常能跑）
      choices:
        - {text: "快速", value: "fast"}
        - {text: "稳妥", value: "safe"}
      help: 灰色小字说明（可选）

steps:
  - name: 我的玩法-分支
    jump: "我的玩法-{{cfg:my_task.my_mode}}"   # 加载时替换成 我的玩法-fast
```

类型：`choice`（下拉，取值写 `choices`）、`bool`（勾选框，占位符渲染成 `true`/`false`）、
`int`（数字框，可写 `min`/`max`/`step`/`unit`）。选项声明由 `engine/options.py` 解析，
按主脚本 `include` 子脚本的顺序排列；`{{cfg:节.键}}` 的替换在 `engine/loader.py`
（include/loop 展开后执行，config 缺键时回落到脚本里声明的 `default`）。

注意：**改脚本的 `options:` 段后，在界面点「重新扫描脚本」**（或切一下任务下拉）即可刷新；
`config.yaml` 的值由 GUI 启动时读取，改完选项若要立刻生效，重启 GUI 最稳。

## 按钮注册表(用中文写脚本)

`assets/ui_registry.yaml` 定义了界面与按钮，脚本里可以直接用中文名引用按钮：

```yaml
steps:
  - name: 等进入主界面
    wait: {ui: 主界面}            # 界面判定(任一特征模板命中)
  - name: 打开菜单
    click: {button: 四方格}       # 按名字点按钮(别名也行)
  - name: 关闭菜单
    click: {button: 菜单返回}
    verify: {ui: 主界面}
  - name: 等按钮出现
    wait: {visible: 玩法}         # 等某按钮可见(用其模板)
  - name: 模板定位点击
    click: {button: 角色, mode: locate}   # 不用固定坐标,模板找到哪点哪
  - name: 自动导航到玩法面板
    goto: {ui: 玩法面板}          # 引擎按 opens 字段自动推导点击路径
```

- 新增按钮：GUI 实时画面点击取坐标采集模板，或在 registry 里加条目
- 按钮字段：desc(功能说明)/screen(归属界面)/at(默认坐标)/template/alias(别名)/dynamic(动态内容标记)
- **opens 字段(多级导航)**：入口按钮填 `opens: 目标界面`，引擎即可 `goto:{ui:目标}` 自动推导从主界面出发的点击路径；按钮库树形列表按 `主界面 › 角色面板 › 技能页` 层级显示
- 已入库：18 个界面 / 121 个按钮，覆盖 主界面→二级面板→三级页(技能/作坊/七十二变/商城/福利/邮件/好友/仓库)→四级页(打造页) 完整导航链（test_l3_l4.yaml 13 步真机验证通过）
- 当前等级锁定项（已标注，等级到了再补识别）：座驾/飞行器页签、召唤兽图鉴/仓库
- 二级/三级菜单按同样方式扩充(screens 加界面、buttons 加按钮、入口按钮填 opens)

## 项目结构

```
xyfz_auto/
├── config.yaml              # 全局配置(adb路径/端口/阈值/超时)
├── live.py                  # 跨进程实时通道(子进程→GUI 回传画面/步骤/日志)
├── accounts.py              # 多开账号清单(accounts.json 读写)
├── tools/adb/               # 内置 adb.exe(换机零配置)
├── device/adb_client.py     # ADB 控制: 截图/点击/滑动/按键/启停App
├── recognition/template.py  # OpenCV 模板匹配(核心识别)
├── engine/                  # YAML 步骤引擎
│   ├── model.py             #   Step/Task 数据模型
│   ├── loader.py            #   YAML 加载/保存(含 {{cfg:节.键}} 配置占位符替换)
│   ├── options.py           #   脚本选项 schema 发现(各脚本 options: 段)
│   └── runner.py            #   执行循环(wait→action→verify→on_fail)
├── assets/
│   ├── templates/           # 模板图库(每张png=一个画面特征)
│   ├── ui_registry.yaml     # 按钮注册表(中文按钮名→坐标/模板/说明)
│   └── tasks/               # 任务脚本(YAML)
├── scripts/
│   ├── run_one.py           # 单账号任务子进程入口(多开调用)
│   ├── capture.py           # 模板采集工具(截图/裁剪)
│   ├── self_test.py         # 链路自检 + 任务执行入口
│   ├── test_match.py        # 识别快速验证
│   └── test_wait_battle.py  # 等待战斗结束实测
├── gui/                     # PySide6 界面
│   ├── main_window.py       #   主窗口(控制面板 + 实时监控)
│   ├── live_store.py        #   按账号的实时数据缓存(通道模型)
│   ├── live_panel.py        #   实时画面页(账号下拉 + 缩略图墙)
│   ├── multi_panel.py       #   多开管理(账号 × 端口 × 脚本)
│   ├── settings_panel.py    #   脚本选项页(按脚本分组, 写 config.yaml)
│   └── worker.py            #   任务工作线程
└── logs/                    # 运行日志/失败现场截图
    └── live/port_<端口>/    #   多开账号的实时回传(meta / events / frame)
```

## 多账号: 实时画面与步骤按账号分开

多开时每个账号跑在独立的 `scripts/run_one.py` 子进程里，与主界面是两个进程。
Qt 信号不能跨进程，所以早期版本界面上**只能看到第一个账号**的画面，
步骤也分不清属于谁。现在通过文件式实时通道把这个缺口补上了：

```
子进程(run_one.py)                         主界面(MainWindow)
  engine 事件 ──► live.LiveWriter ──► logs/live/port_<端口>/
                                        ├─ meta.json      账号名/端口/任务/pid
                                        ├─ ev_<rid>.jsonl start/log/step_*/task_end
                                        └─ fr_<rid>.jpg   最新一帧(原子替换)
                                              │
                            QTimer 600ms ──► live.LiveReader(增量读)
                                              │
                                        ChannelStore(按端口聚合的通道)
                                              │
                            ┌─────────────────┴─────────────────┐
                    实时画面页(账号下拉+缩略图墙)      实时步骤页(账号筛选+前缀)
```

设计要点：

- **本机任务也是一条通道**（`source="local"`），UI 不区分"本机"和"子进程"，
  只多一个来源标记 —— 所以不会再出现"某个号没入口"的情况。
- **步骤清单一次性回传**（`start` 事件带全部步骤描述），界面立刻铺满列表，
  不用等步骤一步步冒出来；脚本开局若有 `sleep 60s` 也不会空白。
- **帧原子替换 + 限流 0.4s**：读方永远拿到完整 JPEG，密集动作也不会写爆磁盘。
- **子进程被强杀也能看出来**：`LiveReader.is_active()` 查 meta 里的 pid，
  进程没了但状态还停在"运行中"时，界面标为「子进程已退出(未见结束事件)」。
- 每个端口目录只保留**最近一次**运行，GUI 重启后仍能把在跑的通道认回来。

日常用法：

| 想看什么 | 操作 |
|---|---|
| 所有账号当前画面 | 「实时画面」页下方缩略图墙 |
| 切换看某个账号大图 | 「实时画面」页顶部下拉，或点缩略图 |
| 只看某个账号的步骤 | 「实时步骤」页顶部下拉选账号 |
| 某个账号在跑什么 | 步骤行前的 `[账号名]` 前缀 + 分组标题 |
| 从多开页跳过去 | 「多开管理」选中行 → 📺 看选中账号画面 |


## 快速上手

```bash
PY="C:/Users/Desktop/.workbuddy/binaries/python/envs/default/Scripts/python.exe"

# 1. 截取当前屏幕
$PY scripts/capture.py shot --out shots/live.png

# 2. 裁剪模板(看图后框选区域) —— --alpha 四周挖空掩膜
$PY scripts/capture.py template shots/live.png 模板名 x1 y1 x2 y2 --alpha

# 3. 验证识别
$PY scripts/test_match.py

# 4. 执行任务
$PY scripts/self_test.py --task assets/tasks/demo_login.yaml
```

## 任务脚本格式 (YAML)

```yaml
name: 任务名
settings: { default_timeout: 30, default_retry: 3 }
steps:
  - name: 步骤描述
    wait:  { template: 模板名 }        # 或 {ocr: "关键词"} / {any:[...]} / {not:{...}}
    timeout: 30
    action: { type: click_center }     # click(固定坐标)/click_center/swipe/key/sleep/start_app
    verify: { template: 另一模板 }     # 可选,点击后确认
    retry: 3
    on_fail: stop                      # retry/continue/stop/{goto: 步骤名}
```

### 重复块 (loop) —— 2026-09-09 新增

loader 支持把一段步骤原地重复 N 次(展开式,适合"打3轮/循环点结算弹窗"这类定数重复)：

```yaml
steps:
  - name: 打一轮10星地煞
    loop: 3                      # 重复次数
    steps:
      - name: 确认在主界面
        wait: {ui: 主界面}
        timeout: 120
      - name: 打开玩法面板
        click: [506, 539]
      ...
```

展开后每份子步骤名自动带 `(i/N)` 后缀,结构与普通步骤一致。

### 子任务引用 (include) / 无条件跳转 (jump) / OCR定位点击 —— 2026-09-09 新增

```yaml
steps:
  - name: "⓪领取离线奖励"
    include: parts/daily_claim.yaml  # 把子文件的 steps 内联展开(递归,最深5层,相对自身目录,支持子目录)
  - name: "①组队一条"
    include: parts/daily_team_oneloop.yaml
```

`jump: 步骤名` 无条件跳转到展开后同名步骤(区别于 on_fail 的 goto,用于分支汇合):

```yaml
  - name: 判断是否需选师傅
    wait: {ocr: 请选择}
    timeout: 8
    on_fail: {goto: 无师傅直接点前往}   # 条件不满足走另一分支
  - name: 点第一个师傅
    click: [122, 781]
  - name: 跳到等战斗
    jump: 师门等战斗                    # 选完师傅跳过前往分支
  - name: 无师傅直接点前往
    click: [241, 782]
```

OCR 定位点击(适合玩法面板卡片这类位置随账号/完成状态变动的入口):

```yaml
  - name: 点师门任务卡片
    click: {ocr: 师门任务}     # 找到含关键词的文字框,点其中心
    timeout: 15
    retry: 5
```

> ⚠️ 语法注意:**一个 step 只写一个动作键**(click / sleep / swipe / key 不能同步骤混写),
> 需要"点击后等待"就拆成 `click:` 一步 + `sleep:` 一步(loader 语法糖只取第一个动作键)。

### 内置日常任务脚本

> 2026-09-11 目录整理: 日常只需**一个** `daily_all.yaml`。原先可单独运行的子脚本已移入
> `parts/`(仍由 `daily_all` include,只是不再出现在 GUI 任务下拉框),回归用例移入 `tests/`。

| 文件 | 内容 |
|---|---|
| `daily_all.yaml` | **日常全家桶(唯一日常脚本)**:include 依次串起 ⓪领取离线奖励→①组队一条→②地煞星×3→③师门→④五环→⑤域外风情额外50次(展开 551 步) |
| `parts/daily_claim.yaml` | 领取离线奖励:玩法面板循环点最上方宝箱(305,160)→确认领取(311,618),领完自动收尾(领奖段 77 步) |
| `parts/daily_team_oneloop.yaml` | 组固定队 → 点任务栏「组队一条」→ 自动跑域外风情/挖宝封妖/烧法/商旅/帮派,监控循环点掉「奖励总览」+ 4 轮安静确认判结束 |
| `parts/daily_dixia_3x.yaml` | 地煞星 10星地藏星 ×3:玩法→地煞星→10星前往→挑战→等12秒→强制进入→自动→胜利→点结算(3/3 守卫跳过) |
| `parts/daily_shimen.yaml` | 师门任务(单人):玩法面板 OCR 点师门卡→新号选师傅/老号点前往→全自动5环→点结算 |
| `parts/daily_wuhuan.yaml` | 五环任务(单人):玩法面板 OCR 点五环卡→前往→全自动寻路/对话/托管战斗→点结算 |
| `parts/daily_yuwai.yaml` | 域外风情额外50次:批次循环(点前往→打完→查50/50→未满再进),OCR 到 50/50 收尾 |
| `tests/login_chunxiang.yaml` | 登录 + 开启纯享模式 |
| `tests/demo_login.yaml` / `tests/test_*.yaml` | 自检/回归用例(goto 导航、二级面板、三级四级页、ocr_pair、on_success 等) |

两脚本关键坐标(540x960): 玩法(506,539)、地煞星卡(270,170)、10星前往(413,366)、挑战(349,601)、强制进入(267,560)、自动(490,915)、结算深色区(450,700)、固定队(506,683)、创建队伍(168,823)、邀请入队(370,632)、组队一条(420,289)。地煞星流程真机实测 3/3 通过;组队一条实测全 5 项完成(结算弹窗需逐次点掉)。

## 当前模板库

| 模板名 | 含义 | 画面状态 |
|---|---|---|
| login_btn | 登录按钮 | 登录界面 |
| main_ui_menu_role | 底部"角色"菜单图标 | 游戏主界面 |
| main_btn_wanfa | 右侧"玩法"按钮 | 游戏主界面 |
| main_btn_menu | 右下角四方格菜单按钮 | 游戏主界面 |
| menu_btn_chun | 菜单底部"纯"字按钮 | 主菜单展开 |
| chun_btn_open | "开启纯享"文字 | 纯享模式面板 |
| chun_btn_close_x | 面板右上角✗ | 纯享模式面板 |
| battle_flag_auto | 右下角"自动中"标志 | 战斗中 |
| battle_btn_flee | 左侧"逃跑"按钮 | 战斗中 |

## 多开并行(多账号) —— 2026-09-09 新增

最多 **5 个账号**并行, 每个账号绑定一个 MuMu 模拟器实例的 ADB 端口。
GUI 右侧 **「多开管理」** tab 维护账号清单(accounts.json):

| 启用 | 名称 | ADB端口 | 任务脚本 | 延迟(分) | 状态 |
|---|---|---|---|---|---|
| ☑ | 账号1 | 16384 | daily_all.yaml | 0 | 运行 3 分 |
| ☑ | 账号2 | 16416 | daily_all.yaml | 20 | 延迟20分 |

- **MuMu 多开默认 adb 端口**: 实例1 = 16384, 之后每实例 +32 (16384/16416/16448/16480/16512);
  端口**手动改成实例真实端口**(以 MuMu 多开器或 `adb devices` 为准)
- 双击单元格改 名称/端口/延迟(错峰分钟, 建议 15~30); 行首勾选决定是否参与「启动全部」
- 「▶ 启动全部」为每个启用账号拉起**独立子进程** `scripts/run_one.py`,
  日志各自写 `logs/run_<port>.log`, 一个号失败不影响其它号
- 「■ 停止全部」终止所有账号子进程; 关闭 GUI 窗口也会自动回收(不残留孤儿进程)
- 命令行调度: `python scripts/multirun.py [--dry-run] [--wait]`
- 单账号调试/连通性探测: `python scripts/run_one.py --port 16416 --task daily_all.yaml`
  或 `--probe`(只连 adb+截图, 不动屏幕)
- 定时任务(GUI 左侧列表)与多开可组合: 勾选后到点自动执行所选脚本; 若想让全家桶
  在多个号上错峰跑, 勾选多个账号+各自延迟即可(账号1 延迟0, 账号2 延迟25, …)

前提: 每个模拟器实例里先把对应账号**手动登录好**(记住密码/纯享已开), 脚本只管日常。
注意: 「启动全部」不会启动 GUI 正在跑的同一个端口(会提示跳过), 避免双开抢同一模拟器。

## 已验证

- ✅ adb 连接/截图/点击(端口 127.0.0.1:16384)
- ✅ 模板匹配自洽 score=1.0,跨界面区分正确
- ✅ 端到端登录任务: 识别→点击→验证进入主界面 全链路成功
- ✅ 登录+纯享完整任务(9步)实测成功,含"已登录自动跳过登录"容错
- ✅ OCR(RapidOCR) 中文识别可用(wait:{ocr:"关键词"})
- ✅ PySide6 GUI 构建通过(gui_main.py 启动)
- ✅ 组队一条日常(2026-09-09 新号3转154): 固定队5人→一条龙5项全完成;地煞星10星×3 全胜
- ✅ loader 支持 loop 重复块展开;daily_dixia_3x(70步)/daily_team_oneloop(34步) 解析校验通过

## Roadmap

- [x] Phase 1: 设备控制 + 模板匹配 + YAML 引擎 + 闭环验证
- [x] Phase 2: OCR(RapidOCR) 接入,支持 wait:{ocr:"文字"}
- [x] Phase 3: PySide6 GUI(控制面板+实时截图+步骤日志+点画面取坐标)
- [x] Phase 4: 登录+纯享任务实测通过(login_chunxiang.yaml)
- [x] Phase 5: 组队一条 + 地煞星×3 日常脚本(真机验证,含结算弹窗处理)
- [ ] Phase 6: 师门/五环等单人日常、防检测(随机延迟/点击抖动)、断线自愈
