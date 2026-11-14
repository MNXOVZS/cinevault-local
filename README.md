# 映库 CineVault Local

[English](README.en.md) · [版本说明](docs/RELEASE-NOTES.md) · [MIT 许可证](LICENSE)

把硬盘上的视频和照片，整理成可以搜索、浏览和回看的影音资料库。

映库在本机启动，通过浏览器使用。可以关联现有文件夹，保留原片的位置和目录结构。片目、封面、观看进度和扫描记录保存在本机。

![映库资料库。合成样片的封面、标签与预览状态](docs/screenshots/library.jpg)

[查看播放器](docs/screenshots/player.jpg) · [查看变动记录](docs/screenshots/activity.jpg)

## 主要功能

- **关联目录**：登记本机或已挂载硬盘中的视频和照片。扫描后显示新文件、缺失文件和离线来源。
- **浏览和检索**：按标题、手动标签、模型标签及概述搜索。封面标签可以直接筛选，绿点表示静音预览就绪。
- **播放和定位**：支持可见卡片的静音预览、日间和夜间主题、时间轴抽样图及手动标记。浏览器无法直接解码部分格式时，可生成兼容副本。
- **核对和备份**：在「设置 → 变动记录」核对登记数、扫描结果和设置变更。备份可保留片目、封面及已登记预览。原片需单独备份。
- **本机视觉概述**：可选 Qwen3-VL 2B 根据抽样画面给出中文概述和场景标签。模型结果和手动笔记分开保存。

## 从源码运行

先安装带 SQLite 的 Python 3.11+。再安装 FFmpeg，并确认 `ffmpeg`、`ffprobe` 都在 `PATH`。静音及兼容视频编码需要 FFmpeg 提供 `libx264`。[Python 下载](https://www.python.org/downloads/) · [FFmpeg 下载](https://ffmpeg.org/download.html)

解压仓库源码，在项目目录启动。Windows 可直接双击 `Start-Windows.cmd`，也可运行：

```powershell
python -X utf8 launch.py
```

macOS 使用终端：

```bash
bash Start-Mac.command
```

打开 [本机映库](http://127.0.0.1:8765)，选择「关联目录」。发布前在 Windows 以 Python 3.14.6 和 FFmpeg 9.0.2 验证源码启动、登记和播放。目前提供 macOS 启动脚本，实机验证待完成。

## 可选本机模型

基础播放、目录索引和手动标签可直接使用。需要本机概述时，在项目目录运行：

```bash
python ai/setup_managed_vision.py
```

macOS 可把 `python` 换成 `python3`。安装脚本下载约 1.45 GiB 权重及本机推理组件。准备完成后，到「分析中心」选择 Qwen3-VL。在画面抽样的范围内，模型可以给出概述和搜索标签。[固定版本的 Qwen 权重](https://huggingface.co/Qwen/Qwen3-VL-2B-Instruct-GGUF/tree/52d6c8ffea26cc873ac5ad116f8631268d7eb503) · [llama.cpp 对应版本](https://github.com/ggml-org/llama.cpp/releases/tag/b12284)

预览版支持场景切换、音量变化和手动标记。发布源码未配备 ONNX 分类及语音活动检测组件。

## 资料和许可

首次启动会在项目目录生成本机资料。关联磁盘后，原片仍在原位置。换电脑时，先停止映库，再迁移片目和预览，并在新设备重新关联硬盘。

外部搜图由你确认当前截图和目标网站后发起。项目演示使用 AI 生成的原创插画和合成样片。截图内的标签用于解释界面。

项目原创源码采用 [MIT 许可证](LICENSE)。第三方组件、模型权重和演示素材有各自的适用范围。第三方下载项沿用上游许可，演示素材独立于源码授权。
