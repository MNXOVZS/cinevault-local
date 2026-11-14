# CineVault Local

[简体中文](README.md) · [Release notes](docs/RELEASE-NOTES.md) · [MIT license](LICENSE)

CineVault Local turns media folders into a searchable library. Browse your videos and photos, preview clips, resume playback, and track changes from a browser served by your own computer.

![CineVault Local library with illustrated demo covers, labels, and preview status](docs/screenshots/library.jpg)

[Player screenshot](docs/screenshots/player.jpg) · [Activity screenshot](docs/screenshots/activity.jpg)

- Link existing folders and mounted drives in place.
- Search titles, notes, and labels. Click a cover label to filter the library.
- Preview visible video cards, view sampled frames on the timeline, and add manual markers.
- Review source scans and catalogue changes, then back up the catalogue and its previews.
- Optionally generate Chinese summaries and scene labels from sampled frames with Qwen3-VL 2B running locally.

## Quick start

Install Python 3.11+ with SQLite. Put `ffmpeg` and `ffprobe` on your `PATH`. Choose an FFmpeg build with `libx264` for muted previews and compatible playback copies. Download and extract the repository source, then run a command from its root. [Python downloads](https://www.python.org/downloads/) · [FFmpeg downloads](https://ffmpeg.org/download.html)

On Windows, run `Start-Windows.cmd` or enter:

```powershell
python -X utf8 launch.py
```

On macOS, run:

```bash
bash Start-Mac.command
```

Open [the local app](http://127.0.0.1:8765) and choose **关联目录** to link a folder. The interface is in Simplified Chinese. Startup, folder indexing, and playback were verified on Windows with Python 3.14.6 and FFmpeg 9.0.2. A macOS launcher is included. Device testing on macOS is pending.

## Optional visual summaries

Run `python ai/setup_managed_vision.py` on Windows, or `python3 ai/setup_managed_vision.py` on macOS. The setup downloads about 1.45 GiB of weights plus the native runtime. Then choose Qwen3-VL in the **分析中心** tab. Model output is based on sampled frames. [Qwen model release](https://huggingface.co/Qwen/Qwen3-VL-2B-Instruct-GGUF/tree/52d6c8ffea26cc873ac5ad116f8631268d7eb503) · [llama.cpp runtime release](https://github.com/ggml-org/llama.cpp/releases/tag/b12284)

Scene cuts, audio changes, and manual markers are available in this preview. ONNX classification and voice activity detection require components outside this source distribution.

Project source is available under the [MIT license](LICENSE). Downloaded third-party dependencies retain their upstream terms. Public demo illustrations and screenshots are separate from the project source license.
