# 映库 · CineVault Local v1.8.1-preview

本次提供产品介绍页、免安装在线演示和 Windows x64 便携试用包。桌面版可用 Windows 文件夹选择器关联原有目录，确认范围后按原位方式扫描。

- [项目介绍](https://mnxovzs.github.io/cinevault-local/) · [免安装在线体验](https://mnxovzs.github.io/cinevault-local/demo/)
- [Windows x64 便携试用包](https://github.com/MNXOVZS/cinevault-local/releases/download/v1.8.1-preview/CineVault-Local-Windows-x64.zip)

## 这个版本能做什么

- 按指定目录和排除项递归扫描；默认只读索引，原片保留在原处。
- 浏览日间或夜间界面；可见视频卡片自动播放就绪的静音预览。
- 管理收藏、手动标签和 18+ 分区；通过时间轴抽样图和标记找片段。
- 核对来源扫描及登记数量，查看文件缺失和来源离线，手动备份片目、封面和已登记预览。
- 可选安装 Qwen3-VL 2B，通过抽样画面在本机生成中文概述和通用语义标签。

## 公开范围

- **平台**：Windows x64 试用包包含 Python 和非成人样片。解压后双击 `Start-Trial.cmd`，点「体验样片」可离线播放。macOS 提供源码版启动脚本，实机验证待完成。[运行步骤](../README.md)
- **播放**：来源格式是否能直放取决于浏览器和编码；需转换时，使用本机 FFmpeg 准备兼容副本。
- **媒体工具**：试用包提供 `Setup-FFmpeg.cmd`。首次处理自己的视频时，可使用现有 FFmpeg，或确认下载校验后放入试用包目录。随包样片可直接播放。
- **本机模型**：视觉概述运行组件与权重由可选脚本下载。公开版未附 NudeNet、ONNX 分类或语音活动检测组件。18+ 分区可手动设置，通用语义标签用于检索普通场景和动作。
- **素材与许可**：公开截图与示例标签只用于演示。原创源码采用 [MIT 许可证](../LICENSE)，第三方下载项沿用各自的上游许可。

遇到问题时，可在 [GitHub Issues](https://github.com/MNXOVZS/cinevault-local/issues/new/choose) 报告，并附系统、FFmpeg 版本、复现步骤与脱敏日志。
