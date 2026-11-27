"""nileswan 版のプレイヤーを、追加の定義 (-D) を付けてビルドし、ROM を作る (テスト用。src/wsvideo の
player_nile.bin は書き換えない)。

    python -X utf8 tests/make_variant_rom.py 出力.wsc データ名.WSV BENCH NOSDMA ...

例: 速さの測定 (BENCH) を、音の DMA あり / なし (NOSDMA) で比べる。
"""
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from wsvideo import encoder, rom  # noqa: E402

NASM = ROOT / 'tools' / 'nasm' / 'nasm.exe'
PSRC = ROOT / 'src' / 'player'


def main():
    out, wsv, defines = Path(sys.argv[1]), sys.argv[2], ['NILE'] + sys.argv[3:]
    with tempfile.TemporaryDirectory() as td:
        binp = Path(td) / 'player_nile.bin'
        subprocess.run([str(NASM), '-f', 'bin', '-I', str(PSRC) + '/'] + [f'-D{d}' for d in defines] +
                       [str(PSRC / 'player.asm'), '-o', str(binp)], check=True)
        data = binp.read_bytes()
        orig = rom.player_bin
        rom.player_bin = lambda name: data
        try:
            out.write_bytes(rom.build_nile_rom(wsv, encoder.Settings()))
        finally:
            rom.player_bin = orig
    print('ok', out, ' '.join(defines))


if __name__ == '__main__':
    main()
