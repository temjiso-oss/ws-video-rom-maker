"""player_nile.bin を作り直したあと、変換し直さずに nileswan 版 ROM だけ作り直す (テスト用)。
    python tests/remake_nile_rom.py 出力.wsc データ名.WSV"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from wsvideo import encoder, rom  # noqa: E402

Path(sys.argv[1]).write_bytes(rom.build_nile_rom(sys.argv[2], encoder.Settings()))
print('ok')
