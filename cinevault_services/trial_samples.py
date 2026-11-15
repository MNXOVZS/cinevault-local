"""[INPUT]: 便携包自带的非成人合成短片。
[OUTPUT]: 可直接播放的固定样片列表与本机文件。
[POS]: 独立于 SQLite 和媒体来源的离线试玩入口。
[PROTOCOL]: Update this header on change, then check AGENTS.md
"""
from pathlib import Path

FILMS = {
    'storyboard': '漫剧 · 分镜试片',
    'phone-diary': '手机日记 · 竖屏',
    'train': '山谷列车',
    'bookshop': '雨巷书店',
    'lighthouse': '灯塔日落',
    'observatory': '山间观星台',
}


def path_for_film(app_root, key, plain_descendant):
    title = FILMS.get(key)
    if title is None:
        return None
    root = Path(app_root) / 'samples' / 'media'
    path = root / (title + '.mp4')
    return path if path.is_file() and plain_descendant(path, root) else None


def present(app_root, plain_descendant):
    return [
        {'id': key, 'title': title, 'url': f'/trial-media/{key}.mp4'}
        for key, title in FILMS.items()
        if path_for_film(app_root, key, plain_descendant)
    ]
