"""Stage only public frontend files for Tauri's compile-time asset embedding."""
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]


def prepare():
    target = ROOT / '.build-cache/frontend'
    if target.exists():
        shutil.rmtree(target)
    (target / 'static').mkdir(parents=True)
    source = ROOT / 'tts_lab/static'
    for name in ('index.html', 'app.js', 'desktop.js', 'style.css'):
        item = source / name
        if item.is_symlink() or not item.is_file():
            raise ValueError(f'Missing or unsafe frontend file: {name}')
        shutil.copy2(item, target / 'index.html' if name == 'index.html' else target / 'static' / name)
    return target


if __name__ == '__main__':
    print(prepare())
