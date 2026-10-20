# 鸡驴助手

大话西游手游日常任务自动化工具：ADB 截图 + 图像模板匹配驱动（ALAS 风格），
支持多开、定时、任务状态灯与运行历史。

**当前版本：v6.0.0**（2026-10-20）

## 下载绿色版（普通用户，推荐）

去 [Releases](https://github.com/rczhaoliang-gif/jilv-assistant/releases) 页下载
**`JilvAssistant_v6.0.0.zip`** —— 112.5MB，已内置 Python 运行时，**解压即跑，无需安装任何东西**。

- 直链：https://github.com/rczhaoliang-gif/jilv-assistant/releases/download/v6.0.0/JilvAssistant_v6.0.0.zip
- 官方校验值：同页的 `JilvAssistant_v6.0.0.md5.txt`；解压后包内还自带「验证完整性.bat」，双击即可核对
- GitHub 的 Release 附件名不支持中文，故文件名用英文；解压后目录名仍是「鸡驴助手_v6.0.0」

## 目录

| 路径 | 说明 |
|---|---|
| `v3/` | **源码工程本体**（唯一在用） |
| `vendor/` | Python 3.13 运行时 + 依赖，337MB，**不入库**（换机器时从绿色版拷贝） |
| `v3/dist/` | 打包产物，**不入库**（单包 290MB + zip 112MB，只走 Releases 分发） |

源码结构、怎么跑、怎么打包 → [`v3/README.md`](v3/README.md)
使用说明与免责声明 → [`v3/assets/docs/使用说明.txt`](v3/assets/docs/使用说明.txt)

## 从源码运行 / 打包

```
开发运行:   v3\运行开发版.bat
打包绿色版: cd v3 && python tools\build_portable.py      (默认产出同名 zip)
发布到 Release: python tools\_publish_release.py         (自动传当前版本 zip，不进仓库)
```

## 声明

本项目仅供学习研究使用，使用后果自负。
完整条款见 `v3/assets/docs/使用说明.txt` 的【免责声明】章节。
