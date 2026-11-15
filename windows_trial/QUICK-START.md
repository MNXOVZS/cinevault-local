# 映库 · CineVault Local Windows 便携试用

1. 在 Windows 10 或 11 x64 上解压整个 ZIP。
2. 双击 `Start-Trial.cmd`。映库在默认浏览器中打开。
3. 点击「体验样片」。自带六段合成短片离线可播。
4. 如需扫描自己的视频，先双击 `Stop-Trial.cmd`，再双击 `Setup-FFmpeg.cmd`。准备工具会明确提示下载约 110 MiB 的媒体组件，也支持系统路径上已有的 FFmpeg 和 ffprobe。
5. 再次双击 `Start-Trial.cmd`，点「关联目录」并选择文件夹。确认扫描范围后开始只读登记。

如浏览器未打开，请访问 <http://127.0.0.1:8766>。

需生成预览、提取视频资料或转码时，请先准备 FFmpeg 和 ffprobe。下载后，媒体组件保存在便携文件夹的 `bin/`。首次启动不会下载模型或工具。程序缓存及片目保存在当前文件夹下的 `data/`；变动不会写到已关联的原片目录。

双击 `Stop-Trial.cmd` 后，可以迁移文件夹或重新准备媒体组件。Windows 如提示文件来自互联网，请核对 GitHub 发布页和所下载的 ZIP 哈希值。

[项目与问题反馈](https://github.com/MNXOVZS/cinevault-local) · [第三方组件](THIRD-PARTY.md)
