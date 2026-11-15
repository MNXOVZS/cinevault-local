# 映库 · CineVault Local

[**Try online**](https://mnxovzs.github.io/cinevault-local/demo/?lang=en) · [**Download for Windows**](https://github.com/MNXOVZS/cinevault-local/releases/download/v1.8.1-preview/CineVault-Local-Windows-x64.zip) · [Product site](https://mnxovzs.github.io/cinevault-local/?lang=en) · [简体中文](README.md)

**Find the clip you remember on an external drive, without moving the original.**

CineVault Local indexes folders you choose. Videos preview as you scroll, and sampled timeline frames help you jump to a moment. Keep phone exports, anime, comic adaptations, films, photos and **18+ videos** in one collection with separate groups when you want them.

The online demo has public, non-adult sample videos. Search the samples, try a filter, play a clip, or scrub the timeline. The desktop version is where you link your own folders.

![CineVault Local library with synthetic demos of phone videos, animation, short clips, and an 18+ section](docs/screenshots/library.jpg)

## Browse, find, and watch

- **Previews play as you browse.** With card previews enabled, ready videos play muted while visible and pause when scrolled away. A green dot marks ready previews. Opening an item loads the player and starts playback when browser policy permits.
- **Find a frame without guessing.** Hover over or scrub the timeline for sampled thumbnails. Jump using scene changes, noticeable shifts in audio level, or your own markers. Play supported sources directly; FFmpeg can prepare a compatible playback copy when needed.
- **Keep private collections organized.** A dedicated 18+ section and searchable manual tags let you organize private videos and animation separately. Assign a section per item or choose a default for new items from a linked folder. Use favorites to return to the items you want to watch again.
- **Optionally let local AI suggest tags.** **Qwen3-VL 2B** can generate Chinese summaries and semantic tags from sampled frames, including scenes and clearly visible everyday actions such as walking, running, and waving. Results depend on the sampled frames. You can enable automatic analysis for new imports after installing the model. Model tags are searchable and stored separately from your manual classifications. The public preview does not integrate dedicated 18+ auto-classification or sexual-act recognition.
- **See what changed.** Preview folder scope and exclude subfolders before scanning. Linked folders are indexed read-only by default. Check for new or missing files and offline sources in the activity log; back up the catalogue, covers, and registered previews.

## Screenshots

The public gallery uses synthetic demo material. The black-and-white 18+ slate shows manual grouping. Qwen3-VL produced the train motion and bridge tags from two sampled frames of the train clip.

| Player and thumbnail scrubbing | Favorites |
| --- | --- |
| ![Playback and sampled timeline frames](docs/screenshots/player.jpg) | ![Favorites view](docs/screenshots/favorites.jpg) |
| **Analysis Center** | **Settings** |
| ![Local model selection and analysis tasks](docs/screenshots/models.jpg) | ![Source, playback, and file protection settings](docs/screenshots/settings.jpg) |

**Light and dark themes**

![Side-by-side views of the light and dark themes](docs/screenshots/themes.jpg)

[See the activity log and item counts](docs/screenshots/activity.jpg)

## Try the portable Windows edition

1. [Download the Windows x64 ZIP](https://github.com/MNXOVZS/cinevault-local/releases/download/v1.8.1-preview/CineVault-Local-Windows-x64.zip) and extract it into a writable folder.
2. Double-click `Start-Trial.cmd`. In the local browser UI, choose **体验样片** to play the bundled non-adult sample clips offline.
3. Select **关联目录** and open the Windows folder picker to link an existing folder. Confirm the scan scope before indexing. The desktop interface is in Simplified Chinese.

The trial includes Python. Covers, previews and conversion for your own videos require FFmpeg. If needed, run the bundled `Setup-FFmpeg.cmd` and confirm the upstream download into the trial folder. See the [release notes](https://github.com/MNXOVZS/cinevault-local/releases/tag/v1.8.1-preview).

## macOS and source setup

Source setup requires **Python 3.11+** with SQLite, plus **FFmpeg** with `libx264` and `ffprobe`. Extract the source and open `Start-Windows.cmd` on Windows. On macOS, run `bash Start-Mac.command` from the project folder. Testing on macOS hardware is pending. [Release and source code](https://github.com/MNXOVZS/cinevault-local/releases/tag/v1.8.1-preview) · [Python](https://www.python.org/downloads/) · [FFmpeg](https://ffmpeg.org/download.html)

MOV, MKV, and AVI are among the containers the library can index. Playback and conversion depend on the codecs and FFmpeg build.

## External drives, migration, and offline use

**You choose what gets scanned.** The library can scan a linked folder recursively. When folder monitoring is enabled, the local service checks for additions periodically. A background queue prepares muted previews at up to 480 × 270 and 24 fps. Browsing reuses those small cached clips. The player picks direct playback, remuxing, or transcoding based on the source and browser support.

**Reconnect after a drive letter or mount location changes.** Stop the app, move the catalogue, cache, and backup as needed, mount the drive on the other computer, then choose and verify the new folder under source settings. Both computers need to be able to read the drive. The app does not silently change source paths or search other drives. Keep a separate backup of your originals.

To carry the catalogue with you, stop the app and run `python package_library.py --profile catalog --target universal` (use `python3` on macOS). The unencrypted ZIP contains the app, catalogue, covers, registered previews, and any model files installed in the app folder. Move or back up originals from external drives separately. Keep the archive private.

**AI can work offline too.** Run `python ai/setup_managed_vision.py` from the project directory, or use `python3` on macOS. Setup downloads about 1.45 GiB of model weights plus the native runtime. Choose Qwen3-VL in **分析中心** (Analysis Center). The installer offers Windows x64, Apple silicon, and Intel Mac runtimes. To search by image externally, review the frame and destination, then initiate the search in your browser.

## Feedback and credits

Search [GitHub Issues](https://github.com/MNXOVZS/cinevault-local/issues) or [open an issue](https://github.com/MNXOVZS/cinevault-local/issues/new/choose). Include your operating system, browser, FFmpeg version, file container and codecs, steps, and expected and actual behavior. Share redacted logs or synthetic samples instead of private media, covers, or file paths.

Thank you to the maintainers and contributors of [CPython](https://github.com/python/cpython), [SQLite](https://www.sqlite.org/), [FFmpeg](https://ffmpeg.org/), [TypeScript](https://github.com/microsoft/TypeScript), [esbuild](https://esbuild.github.io/), [Qwen3-VL](https://github.com/QwenLM/Qwen3-VL), and [llama.cpp](https://github.com/ggml-org/llama.cpp). [Pinned model weights](https://huggingface.co/Qwen/Qwen3-VL-2B-Instruct-GGUF/tree/52d6c8ffea26cc873ac5ad116f8631268d7eb503) · [Matching runtime release](https://github.com/ggml-org/llama.cpp/releases/tag/b12284)

Original code is provided under the [MIT license](LICENSE). Third-party components and models retain their upstream terms. Demo media is separate from the source license. [Public preview scope](docs/RELEASE-NOTES.md)
