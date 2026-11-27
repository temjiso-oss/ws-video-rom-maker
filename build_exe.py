"""配布用の exe を作る。

    python build_player.py   (player.asm を変えたとき)
    python build_exe.py          普通の版 (CPU で計算)
    python build_exe.py --gpu    GPU 版 (CuPy と NVIDIA の部品を同梱。NVIDIA の GPU のある PC で速い。大きい)

出力: dist/WSVideoRomMaker/WSVideoRomMaker.exe (ffmpeg.exe と player.bin、画面 web/ を同梱)
      files/WSVideoRomMaker-<版>.zip (GPU 版は -GPU.zip)
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
    gpu = '--gpu' in sys.argv
    work = ROOT / 'build'
    sep = ';'
    cmd = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--windowed', '--onedir',
           '--name', 'WSVideoRomMaker', '--distpath', str(ROOT / 'dist'), '--workpath', str(work),
           '--specpath', str(work), '--paths', str(SRC),
           '--add-data', f'{SRC / "wsvideo" / "player.bin"}{sep}.',
           '--add-data', f'{SRC / "wsvideo" / "player_nile.bin"}{sep}.',
           '--add-data', f'{SRC / "wsvideo" / "web"}{sep}wsvideo/web',
           '--collect-all', 'tkinterdnd2',
           '--collect-all', 'webview',
           '--add-binary', f'{ff}{sep}.',
           '--exclude-module', 'imageio_ffmpeg', '--exclude-module', 'matplotlib']
    if gpu:
        for m in ('cupy', 'cupy_backends', 'fastrlock', 'cuda_pathfinder'):
            cmd += ['--collect-all', m]
        # CuPy の中 (Cython) から読まれる標準の部品は、PyInstaller が見つけられないので明示する
        for m in ('graphlib', 'cupy._core._scalar'):
            cmd += ['--hidden-import', m]
        for m in ('nvidia', 'nvidia.cuda_runtime', 'nvidia.cuda_nvrtc', 'nvidia.cublas', 'nvidia.curand',
                  'nvidia.cufft', 'nvidia.cusolver', 'nvidia.cusparse', 'nvidia.nvjitlink'):
            cmd += ['--collect-binaries', m, '--collect-data', m]
    else:
        cmd += ['--exclude-module', 'cupy', '--exclude-module', 'cupy_backends']
    cmd.append(str(SRC / 'app.py'))
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
    out = ROOT / 'files' / (f'WSVideoRomMaker-{__version__}' + ('-GPU' if gpu else ''))
    shutil.make_archive(str(out), 'zip', dist.parent, dist.name)
    print('OK:', dist / 'WSVideoRomMaker.exe')
    print('ZIP:', str(out) + '.zip')


if __name__ == '__main__':
    main()
