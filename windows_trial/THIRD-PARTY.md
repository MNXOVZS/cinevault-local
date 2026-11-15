# Windows 便携试用组件与样片

## Python

程序包携带 Python Software Foundation 的 Python 3.14.10 Windows x64 嵌入版。除 `sqlite3.dll` 外，运行时文件来自 [PSF 官方归档](https://www.python.org/downloads/release/python-31410/)。

- 安装包：[`python-3.14.10-embed-amd64.zip`](https://www.python.org/ftp/python/3.14.10/python-3.14.10-embed-amd64.zip)
- 归档 SHA-256：`90905e64a840557d8c1bc4f5b74db3d7ee85e3ffb217611cac1c7813acea4159`
- 原许可及第三方声明：`runtime/windows-x64/LICENSE.txt`

`sqlite3.dll` 使用 SQLite 官方 3.54.1 Windows x64 版本。该 DLL 含有 [WAL 重置修复](https://sqlite.org/wal.html#the_wal_reset_bug)。SQLite 以[公有领域声明](https://www.sqlite.org/copyright.html)发布。

- 官方归档：[`sqlite-dll-win-x64-3540100.zip`](https://www.sqlite.org/2026/sqlite-dll-win-x64-3540100.zip)
- 归档 SHA-256：`f436f53ad8f965af43f3817b42c99c0689e6787f6b7ace208fe38dab7ffc4971`

## FFmpeg 和 ffprobe

它们是独立的可选媒体工具。试用包提供准备入口。用户确认后，入口向 Gyan 请求固定归档，校验后提取两个可执行文件和许可文件。Windows 现有系统路径已提供两项工具时，直接使用现有工具。

- 上游：[Gyan FFmpeg 9.0.2 Essentials](https://github.com/GyanD/codexffmpeg/releases/tag/9.0.2)
- 归档：[`ffmpeg-9.0.2-essentials_build.zip`](https://www.gyan.dev/ffmpeg/builds/packages/ffmpeg-9.0.2-essentials_build.zip)
- 归档 SHA-256：`60f467265b1e312373dbcd92200c2618a74850f98d3d078e94296bb3fa2047ba`
- 厂商公布的 [FFmpeg 源码提交](https://github.com/FFmpeg/FFmpeg/commit/946fcce07b)
- 该构建的 GPL 许可随上游归档提供，程序目录中为 `bin/FFmpeg-LICENSE.txt`。

运行时的模型下载可选，模型和组件的许可文件位于 `ai/vision/`。

## 合成样片

自带的六段测试短片由本项目生成，用于公开演示。内容包括山谷列车、灯塔日落、山间观星台、雨巷书店、漫剧文字分镜和手机竖屏文字样片。
