# 鸡驴助手 (xyfz_auto) —— 开发源码工程

这里是**开发项目**，`app/xyfz_auto/` 是真正的源码。对外分发的绿色便携版由 `build.bat` 从本目录打包生成到 `dist/`。
开发目录和绿色版目录职责分离：改代码在这里改，改完打包一次，把 `dist` 里的产物拷给别人。

- 版本：**v2.0.6**
- 运行时：Python 3.13.14 + 17 个依赖（onnxruntime / opencv / PySide6 / numpy / RapidOCR…）
- 目标设备：MuMu 12 模拟器，ADB `127.0.0.1:16384`，屏幕基线 540×960 / density 240
需要加入固定队，且创建队伍时所有成员都能加入队伍。
需要完成主线任务，坐骑任务等任务。
需要队伍可以自动模式通过地煞10星。
推荐一位主播  大话西游归来缘起服-达拉崩吧   
git版本为存档使用，不一定为最新版。

## 目录结构

```
鸡驴助手_dev/
├─ app/xyfz_auto/        源码本体（唯一要改代码的地方）
│  ├─ gui/               PySide6 界面（主窗口 / 实时画面 / 脚本编辑 / 按钮库 …）
│  ├─ engine/            loader(YAML加载) model(Step/Task) runner(执行循环) options ui_registry
│  ├─ device/            adb_client(截图/点击) win_input(Windows鼠标注入)
│  ├─ recognition/       template(OpenCV匹配) ocr(RapidOCR)
│  ├─ live.py            多开子进程 → GUI 的文件式实时回传
│  ├─ assets/tasks/      任务脚本 YAML（顶层可单独跑）
│  ├─ assets/tasks/parts/ 子脚本（被总脚本 include）
│  ├─ assets/templates/  识别模板 PNG（62 个）
│  ├─ assets/ui_registry.yaml  按钮/界面注册表（中文名 → 模板/坐标）
│  ├─ scripts/           check_refs.py 等工具脚本
│  └─ tools/adb/         内置 adb（不依赖系统环境）
├─ vendor/runtime/       Python 3.13 + 依赖，337MB，不入库（见 .gitignore）
├─ assets/launcher/      3 个 bat 源文件（打包时放到绿色版根目录）
├─ assets/docs/          使用说明.txt / 跨电脑排查手册.txt（同上）
├─ tools/build_portable.py  打包脚本
├─ dist/                 打包产物，不入库
├─ build.bat             双击打包
├─ 运行开发版.bat         从源码直接跑
└─ requirements.txt      依赖清单（仅供复现环境，运行时实际用 vendor/runtime）
```

## 下载与使用方式

本仓库只存**源码**（337MB 运行时和打包产物已用 `.gitignore` 排除，不进 git 历史）。按你的角色选一种：

### 普通用户（想开箱即用）
1. 去仓库 **Releases** 页下载 `鸡驴助手_v2.0.6.zip`（已含 Python 运行时，解压即跑）
2. 解压到任意目录，双击 **`启动助手.bat`** 启动
- 前提环境：本机已装 **MuMu 12** 模拟器，游戏包名 `com.netease.xyfz_02`，ADB 端口 `16384`

### 开发者（想改代码 / 自己跑源码）
1. `git clone` 本仓库，或 GitHub 页面 **Code → Download ZIP**
2. 恢复运行时（二选一）：
   - 把绿色版里的 `runtime\` 整个拷到 `vendor\runtime\`
   - 或 `pip install -r requirements.txt`（需 Python 3.13，onnxruntime/PySide6 等约 300MB）
3. 双击 **`运行开发版.bat`** 从源码运行

### 自己重新打包绿色版
双击 **`build.bat`**（或命令行 `build.bat --zip` 顺带压包），产物在 `dist\鸡驴助手_v2.0.6\`。

> 注意：绿色版（含 runtime）约 340MB，**只通过 Releases 分发，不要提交进 git 仓库**（否则每个 clone 都要下载、且历史永久膨胀）。

## 日常怎么用

| 想做什么 | 怎么做 |
|---|---|
| 从源码运行 | 双击 `运行开发版.bat` |
| 看报错（GUI 起不来时） | 双击 `运行开发版(调试).bat` |
| 打包绿色版 | 双击 `build.bat` → `dist\鸡驴助手_v2.0.6\` |
| 打包并压 zip | 命令行 `build.bat --zip` |
| 脚本/注册表自检 | `vendor\runtime\python.exe app\xyfz_auto\scripts\check_refs.py` |

## 打包产物的结构（与历史绿色版完全一致）

```
鸡驴助手_v2.0.6/
├─ app/xyfz_auto/
├─ runtime/
├─ 启动助手.bat / 启动助手(调试).bat / 清理残留进程.bat
└─ 使用说明.txt / 跨电脑排查手册.txt
```

**零绝对路径**：三个 bat 全部用 `%~dp0` 定位自身目录，整个文件夹可以拷到任意盘符、任意电脑直接跑。
打包脚本会自动校验这点（发现硬编码盘符会报错）。排除项：`__pycache__` `logs` `_collect` `shots` `*.pyc` `*.log`。

## 版本号规则

**进位规则：第二位到 9 就进一位，同时第一位 +1**（如 1.10.3 → 2.0.0）。

改版本要同步**三处**（历史上这三处长期不一致，已统一，别再拆开）：
1. `app/xyfz_auto/common.py` → `APP_VERSION`
2. `app/xyfz_auto/__init__.py` → `__version__`
3. `assets/docs/使用说明.txt` → 【版本历史】补一条

打包目录名自动读 `APP_VERSION`，不用手改。

## 换电脑 / 恢复 vendor

`vendor/runtime` 因体积不入库。新环境拿到本仓库后：
- 有绿色版 → 把绿色版里的 `runtime\` 整个拷到 `vendor\runtime\`
- 没有 → `pip install -r requirements.txt`（需 Python 3.13，onnxruntime/PySide6 约 300MB）

## 开发约定（都是事故换来的）

- **脚本一份 = `name` + 可选 `settings` + `steps[]`**，引擎逐步做「轮询识别 → 动作 → 复核 → 超时 on_fail」
- **每步只写一个动作键**：`click` + `sleep` 混写会静默丢掉后者，想"点击后等待"就拆两步
- **`mode: locate` 的按钮必须在注册表里有 `template:`**，否则运行时抛错（已由 `check_refs.py` 提前拦截）
- **每步都要 `on_fail` 兜底**，子脚本里裸奔失败会终止整条日常
- **步骤名加玩法前缀**（如 `地煞-`）：include 展开后 `goto/jump` 按名字找首个同名步骤，重名会串门
- **分支铁律**：条件成立的路径必须 `jump` 跳走，否则顺序下滑会重复点击
- **弹窗等待优先用模板不用 OCR**（美术字易读错，如「一键扫荡」被读成「键扫荡」）
- **收尾用线性逐层关**：`(点返回 → 等主界面) × 3~4`
- **注册表不能重名**：历史上两个「商城」被 `yaml.safe_load` 静默覆盖，现由 `StrictLoader` 显式报错
- **`menu_panel_back` 在盈福大厅页也会命中 0.991**（共用返回箭头），别拿"主菜单面板"当"已退出子页"的依据

改完脚本或注册表后跑一次 `check_refs.py`，它能扫出「引用了不存在的按钮 / locate 没配模板 / goto 目标缺失」。
