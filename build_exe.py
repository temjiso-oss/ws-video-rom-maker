"""配布用の exe を作る。

    python build_player.py   (player.asm を変えたとき)
    python build_exe.py

出力: dist/WSVideoRomMaker/WSVideoRomMaker.exe (ffmpeg.exe と player.bin を同梱)
      files/WSVideoRomMaker-<版>.zip
"""
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / 'src'


def main():
    import imageio_ffmpeg
    ff = imageio_ffmpeg.get_ffmpeg_exe()
    work = ROOT / 'build'
    sep = ';'
    cmd = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--windowed', '--onedir',
           '--name', 'WSVideoRomMaker', '--distpath', str(ROOT / 'dist'), '--workpath', str(work),
           '--specpath', str(work), '--paths', str(SRC),
           '--add-data', f'{SRC / "wsvideo" / "player.bin"}{sep}.',
           '--add-data', f'{SRC / "wsvideo" / "player_nile.bin"}{sep}.',
           '--collect-all', 'tkinterdnd2',
           '--add-binary', f'{ff}{sep}.',
           '--exclude-module', 'imageio_ffmpeg', '--exclude-module', 'matplotlib',
           str(SRC / 'app.py')]
    subprocess.run(cmd, check=True)
    dist = ROOT / 'dist' / 'WSVideoRomMaker'
    internal = dist / '_internal'
    base = internal if internal.exists() else dist
    src_ff = base / Path(ff).name
    if src_ff.exists():
        src_ff.rename(base / 'ffmpeg.exe')
    shutil.copy(ROOT / 'README.md', dist / 'README.md')
    # ライセンス一式 (release/ に置いたもの)
    rel = ROOT / 'release'
    for name in ('LICENSE', 'THIRD_PARTY_NOTICES.md'):
        shutil.copy(rel / name, dist / name)
    if (dist / 'licenses').exists():
        shutil.rmtree(dist / 'licenses')
    shutil.copytree(rel / 'licenses', dist / 'licenses')
    if (dist / 'screenshots').exists():
        shutil.rmtree(dist / 'screenshots')
    if (rel / 'screenshots').exists():
        shutil.copytree(rel / 'screenshots', dist / 'screenshots', ignore=shutil.ignore_patterns('*.mp4'))
    sys.path.insert(0, str(SRC))
    from wsvideo import __version__
    out = ROOT / 'files' / f'WSVideoRomMaker-{__version__}'
    shutil.make_archive(str(out), 'zip', dist.parent, dist.name)
    print('OK:', dist / 'WSVideoRomMaker.exe')
    print('ZIP:', str(out) + '.zip')


if __name__ == '__main__':
    main()
