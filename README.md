# 映库 · CineVault Local

[English](README.en.md) · [下载公开预览](https://github.com/MNXOVZS/cinevault-local/releases) · [报告问题 / 提议功能](https://github.com/MNXOVZS/cinevault-local/issues) · [MIT 许可证](LICENSE)

**从手机相册到外接硬盘，给所有本地视频一个好找、好看的地方。**

映库是一套为个人收藏打造的离线影音资料库。把从 iPhone 或 Android 导出的手机视频、短片、动画、漫剧、老电影、照片，以及有权保存的 18+ 私人视频，整理进同一套资料库。打开就能浏览封面、自动播放静音卡片预览，按标签找到想看的片段。文件仍留在你指定的目录。

![映库首页：手机视频、动画、短片及 18+ 分类的非露骨合成演示素材](docs/screenshots/library.jpg)

## 看得快，找得准

- **打开就预览。** 开启卡片预览后，已就绪的可见卡片会自动静音播放，离屏后暂停。绿点表示预览就绪。打开影片即加载播放器，浏览器允许时自动开播。
- **拖着时间轴找画面。** 悬停或拖动时间轴可查看抽样缩略图。用画面切换、明显音量变化和手动标记跳到目标位置。片源可直接解码时直接播放，遇到须转换的编码再由 FFmpeg 生成兼容副本。
- **私人收藏由你分。** 用专门的 18+ 分区和可检索的手动标签整理 18+ 视频与动画。可在详情中调整分区，也可按目录指定新入库资料的分区。收藏、标题和标签都是回找入口。
- **可选离线 AI 自动打标签。** 安装 **Qwen3-VL 2B** 后，可从抽样画面生成中文概述，以及语义标签，包括场景和画面中清晰可见的普通动作，例如行走、跑步和挥手。结果取决于抽样画面。可开启新入库资料自动分析。模型标签与手动分类分别保存，可以筛选。公开预览版未集成专门的 18+ 自动分类或性动作识别。
- **扫描范围由你决定。** 预览指定目录的扫描范围，可排除子目录。默认只读索引，不搬移、改名或删除原片。设置里的变动记录可核对新增、缺失及来源离线情况，也可备份片目、封面和已登记的预览。

## 界面一览

公开截图均使用合成演示资料。黑白 18+ 提示卡演示手动分区。列车短片的「火车行驶」「古桥」是 Qwen3-VL 从两张抽样图生成的标签。

| 播放器和时间轴 | 我的收藏 |
| --- | --- |
| ![播放器：清晰度与时间轴的抽样缩略图](docs/screenshots/player.jpg) | ![我的收藏：单独浏览收藏的资料](docs/screenshots/favorites.jpg) |
| **分析中心** | **设置** |
| ![分析中心：本机视觉模型、任务与状态](docs/screenshots/models.jpg) | ![设置：来源目录、播放和本机保护选项](docs/screenshots/settings.jpg) |

**日间 / 夜间主题对比**

![同一套映库界面的日间和夜间主题对比](docs/screenshots/themes.jpg)

[查看变动记录与数量核对](docs/screenshots/activity.jpg)

## 在 Windows 或 Mac 上运行

1. 安装带 SQLite 的 **Python 3.11+** 和带 `libx264` 的 **FFmpeg**。确认 `ffmpeg` 和 `ffprobe` 在系统的 `PATH` 上。[Python 下载](https://www.python.org/downloads/) · [FFmpeg 下载](https://ffmpeg.org/download.html)
2. 下载并解压源码。在 Windows 双击 `Start-Windows.cmd`（也可运行 `python -X utf8 launch.py`）；在 macOS 终端进入项目目录，运行 `bash Start-Mac.command`。
3. 打开 [本机映库](http://127.0.0.1:8765)，点「关联目录」，选择本机文件夹或已挂载的外接盘目录。打开的网页由你自己的电脑提供。基础功能不需要账号或网络。

公开版提供两个平台的启动脚本；Windows 已验证启动、索引和播放，macOS 实机验证待完成。原片编解码器与 FFmpeg 构建决定实际格式兼容性；MOV、MKV、AVI 等是可扫描的格式，无法承诺每一种编码都能播放。

## 外接硬盘、迁移与离线使用

**扫描只发生在你关联的范围内。** 映库可递归扫描该目录及子目录；开启目录监测后，本机服务运行期间会定期检查新增。预览由后台队列生成，使用最高 480 × 270、24 fps 的静音短片。浏览时复用预览缓存，避免为每张卡片反复解码大文件。播放器会按原片与浏览器支持情况选择直放、换封装或转码。

**硬盘换盘符或从 Windows 拔到 Mac 时，重新关联即可。** 先停映库并迁移片目、缓存和备份，再挂载硬盘，在来源设置选新位置并核对。硬盘须能被两台电脑读到。来源原路径不会被静默改写，换位置不代表自动搜遍所有磁盘。原片需要单独备份。

要把片目一起带走，停服务后运行 `python package_library.py --profile catalog --target universal`（macOS 使用 `python3`）。压缩包包含程序、片目、封面和已登记预览，也会带上已安装在程序目录里的模型文件。外接盘原片需另外迁移。压缩包未加密，请按私人资料保管。

**AI 也可留在本机。** 在项目目录运行 `python ai/setup_managed_vision.py`；macOS 可用 `python3`。脚本下载约 1.45 GiB 的模型权重及本机推理组件。下载后在「分析中心」选择 Qwen3-VL，即可离线分析。模型安装器提供 Windows x64、Apple 芯片与 Intel Mac 的运行组件。外部搜图由你核对截图和目标后，交给浏览器手动发起。

## 反馈与开源致谢

在 [GitHub Issues](https://github.com/MNXOVZS/cinevault-local/issues) 查找已有问题，或[新建反馈](https://github.com/MNXOVZS/cinevault-local/issues/new/choose)。报告播放、预览或硬盘重连问题时，请写上系统、浏览器、FFmpeg 版本、文件封装及编解码器、复现步骤、预期结果和实际现象。请用脱敏日志或合成样片，避免上传私人视频、封面和磁盘路径。

感谢 [CPython](https://github.com/python/cpython)、[SQLite](https://www.sqlite.org/)、[FFmpeg](https://ffmpeg.org/)、[TypeScript](https://github.com/microsoft/TypeScript)、[esbuild](https://esbuild.github.io/)、[Qwen3-VL](https://github.com/QwenLM/Qwen3-VL) 和 [llama.cpp](https://github.com/ggml-org/llama.cpp) 的维护者与贡献者。[固定模型权重](https://huggingface.co/Qwen/Qwen3-VL-2B-Instruct-GGUF/tree/52d6c8ffea26cc873ac5ad116f8631268d7eb503) · [对应推理组件](https://github.com/ggml-org/llama.cpp/releases/tag/b12284)

原创源码采用 [MIT 许可证](LICENSE)。第三方组件和模型沿用上游许可；演示素材独立于源码授权。[公开预览范围](docs/RELEASE-NOTES.md)
