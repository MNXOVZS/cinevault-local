# windows_trial/

> L2 | 上级 [AGENTS.md](../AGENTS.md)

- `build.py` 从公开源码和已校验输入生成便携 ZIP。
- `setup_ffmpeg.py` 从固定上游取得本机媒体工具。
- `Start-Trial.cmd` 使用便携运行时启动试用资料库。
- `Stop-Trial.cmd` 停止同一个试用实例。
- `Setup-FFmpeg.cmd` 打开媒体工具的安装步骤。
- `QUICK-START.md` 说明首次运行、权限和数据位置。
- `THIRD-PARTY.md` 记录组件来源和许可。

构建过程只读取公开源码和固定的合成样片。
从 `site/assets/` 取得样片。输入归档见 [THIRD-PARTY.md](THIRD-PARTY.md)。

```powershell
python windows_trial/build.py --python-zip <PSF归档> --sqlite-zip <SQLite归档> --output <源码目录之外的ZIP>
```

[PROTOCOL]: Update this header on change, then check AGENTS.md
