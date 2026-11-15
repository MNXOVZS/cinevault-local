# 映库 CineVault Local

本机影音片目与播放器。

Python 3.11+ · SQLite · FFmpeg · TypeScript 7.0.2 · esbuild 0.28.2

| 路径 | 边界 |
| --- | --- |
| [cinevault_services/](cinevault_services/AGENTS.md) | 分析、播放、缓存与片目变动 |
| [web/](web/AGENTS.md) | 浏览器界面与前端构建产物 |
| [frontend/](frontend/AGENTS.md) | 严格 TypeScript 源码与构建 |
| [ai/](ai/AGENTS.md) | 可选本机视觉组件的准备入口 |
| [docs/](docs/AGENTS.md) | 发布说明、产品文案与演示截图 |
| [site/](site/AGENTS.md) | 产品介绍与合成样片在线演示 |
| [windows_trial/](windows_trial/AGENTS.md) | Windows 便携包构建、入口与媒体工具准备 |
| [.github/](.github/AGENTS.md) | GitHub 反馈表单与网站部署 |
| `server.py` | HTTP 接口、SQLite 事务与来源调度 |
| `launch.py` | 指定回环端口的启动、停止与浏览器入口 |
| `package_library.py` | 按模式打包并校验文件清单 |
| `restore_backup.py` | 备份校验与事务式恢复 |
| `verify_package.py` | 读取包的校验清单并核对内容 |
| `diagnose.py` | 检查本机依赖与服务状态 |
| `Start-Windows.cmd` / `Start-Mac.command` | 使用平台 Python 启动 |
| `Stop-Windows.cmd` / `Stop-Mac.command` | 请求正常停止服务 |

使用合成资料测试。提交公开源码前排除本机 `data/`、权重和运行组件。

[PROTOCOL]: Update this header on change, then check AGENTS.md
