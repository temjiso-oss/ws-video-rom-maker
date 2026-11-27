"""公開用のソース一式を release/ にそろえる (GitHub にそのまま置ける形)。

    python build_player.py
    python build_exe.py        # 配布用 zip (files/WSVideoRomMaker-<版>.zip)
    python make_release.py     # release/ (ソース・ライセンス・README・画像)

個人的な作業メモ (docs/HANDOFF.md など)、テスト用のエミュレーター (tools/)、変換したデータは入れない。
"""
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REL = ROOT / 'release'

KEEP_IN_RELEASE = {'LICENSE', 'THIRD_PARTY_NOTICES.md', 'licenses', 'screenshots'}


def copy(src, dst):
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def main():
    # 前回作った分 (ライセンス・画像以外) を消す
    for p in REL.iterdir() if REL.exists() else []:
        if p.name in KEEP_IN_RELEASE:
            continue
        shutil.rmtree(p) if p.is_dir() else p.unlink()
    copy(ROOT / 'README.md', REL / 'README.md')
    for name in ('build_player.py', 'build_exe.py', 'make_release.py'):
        copy(ROOT / name, REL / name)
    copy(ROOT / 'src' / 'app.py', REL / 'src' / 'app.py')
    for p in (ROOT / 'src' / 'wsvideo').glob('*'):
        if p.suffix in ('.py', '.bin'):
            copy(p, REL / 'src' / 'wsvideo' / p.name)
    # 1.5.0〜 の画面 (HTML・JS・CSS)。2026-11-27 まで入れ忘れていた
    for p in (ROOT / 'src' / 'wsvideo' / 'web').glob('*'):
        if p.suffix in ('.html', '.js', '.css', '.svg', '.png', '.ico'):
            copy(p, REL / 'src' / 'wsvideo' / 'web' / p.name)
    for p in (ROOT / 'src' / 'player').glob('*'):
        if p.suffix in ('.asm', '.inc'):
            copy(p, REL / 'src' / 'player' / p.name)
    for p in (ROOT / 'tests').glob('*.py'):
        copy(p, REL / 'tests' / p.name)
    # 技術資料 (作業メモの言い回しを公開向けに)
    spec = (ROOT / 'docs' / 'SPEC.md').read_text(encoding='utf-8')
    spec = spec.replace('（2026-11-07 ユーザー確認、nileswan + IPS化WSC。イヤホンは問題なし）', '（実機で確認。イヤホンは問題なし）')
    spec = re.sub(r'ユーザー向けの説明は `\.\./README\.md`', 'ユーザー向けの説明は `../README.md`', spec)
    (REL / 'docs').mkdir(exist_ok=True)
    (REL / 'docs' / 'TECHNICAL.md').write_text(spec, encoding='utf-8')
    (REL / 'requirements.txt').write_text('numpy\npillow\nimageio-ffmpeg\ntkinterdnd2\npywebview\npyinstaller\n', encoding='utf-8')
    (REL / '.gitignore').write_text('__pycache__/\n*.pyc\nbuild/\ndist/\nfiles/\ntools/\n*.lst\nsrc/player/font.bin\n'
                                    'src/player/osdfont.bin\nsrc/player/guidefont.bin\n*.wsc\n*.WSV\n*.mp4\n!screenshots/*.mp4\n', encoding='utf-8')
    (REL / 'tests' / 'README.md').write_text(
        '# テスト用の道具\n\n'
        '- `sim_rom.py` / `verify.py` / `seek_test.py` / `run_rom.py`: libretro の Beetle Cygne (mednafen_wswan) コアを\n'
        '  `tools/emu/mednafen_wswan_libretro.dll` に置いて使う（同梱していません）。\n'
        '- `fat32img.py` / `run_nile_emu.py`: nileswan 公式のエミュレーター（nileswan-medem と nileswan-fw-emulator）を\n'
        '  `tools/nileswan-emu/` に置いて使う（同梱していません）。\n', encoding='utf-8')
    # 個人情報が紛れ込んでいないか (このパソコンのユーザー名やホームフォルダのパス)
    import os
    words = {os.environ.get('USERNAME', ''), Path.home().name}
    words = [re.escape(w) for w in words if len(w) >= 3]
    bad = re.compile('|'.join(words + [r'\\Users\\', r'/Users/']), re.IGNORECASE)
    for p in REL.rglob('*'):
        if p.is_file() and p.suffix in ('.py', '.md', '.asm', '.inc', '.txt') and p.name != 'make_release.py':
            if bad.search(p.read_text(encoding='utf-8', errors='ignore')):
                raise SystemExit(f'個人情報らしき文字列があります: {p}')
    n = sum(1 for p in REL.rglob('*') if p.is_file())
    print(f'release/ に {n} ファイル')


if __name__ == '__main__':
    main()
