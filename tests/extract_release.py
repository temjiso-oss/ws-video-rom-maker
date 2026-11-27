"""公開した版の配布 zip (PyInstaller の exe) から、wsvideo の .pyc とプレイヤーを取り出す。
比べる動画 (make_demo.py) を、ソースの残っていない公開版で作るため。Python 3.12 と PyInstaller が要る。

    python -X utf8 tests/extract_release.py 1.3.3      → files/old-1.3.3/wsvideo/
"""
import importlib.util
import marshal
import sys
import tempfile
import zipfile
from pathlib import Path

from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader

ROOT = Path(__file__).resolve().parents[1]
ver = sys.argv[1]
OUT = ROOT / 'files' / f'old-{ver}' / 'wsvideo'
OUT.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory() as td, zipfile.ZipFile(ROOT / 'files' / f'WSVideoRomMaker-{ver}.zip') as z:
    exe = Path(td) / 'app.exe'
    exe.write_bytes(z.read('WSVideoRomMaker/WSVideoRomMaker.exe'))
    for n in z.namelist():
        if n.endswith('.bin') and Path(n).name.startswith('player'):
            (OUT / Path(n).name).write_bytes(z.read(n))
    car = CArchiveReader(str(exe))
    pyz_path = Path(td) / 'PYZ.pyz'
    pyz_path.write_bytes(car.extract([n for n in car.toc if n.endswith('.pyz')][0]))
    pyz = ZlibArchiveReader(str(pyz_path))
    for name in pyz.toc:
        if name == 'wsvideo' or name.startswith('wsvideo.'):
            p = OUT / ('__init__.pyc' if name == 'wsvideo' else name.split('.', 1)[1] + '.pyc')
            p.write_bytes(importlib.util.MAGIC_NUMBER + b'\0' * 12 + marshal.dumps(pyz.extract(name)))
print('ok', OUT, sorted(p.name for p in OUT.iterdir()))
