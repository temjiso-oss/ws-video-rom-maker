"""エミュレーターの画面が、ROM を Python で読み戻した画面と一致するかを調べる。
使い方: python tests/verify.py rom.wsc [エミュレーターのフレーム数]"""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from emu import Emu, RAM  # noqa: E402
import sim_rom  # noqa: E402


def main():
    rom = sys.argv[1]
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 1500
    frames = {}
    for it, f, img in sim_rom.decode(Path(rom).read_bytes()):
        frames[(it, f)] = img.astype(np.int16)
    e = Emu(rom)
    checked = exact = 0
    worst = 0
    for i in range(n):
        e.run(1)
        if i % 37 != 5:
            continue
        scr = np.asarray(e.image.convert('RGB'), np.int16)
        item = e.ram[RAM['ITEM']]
        fi = e.w(RAM['FRAMEI'])
        cands = [frames[k] for k in ((item, fi - 1), (item, fi), (item, fi - 2)) if k in frames]
        if not cands:
            continue
        d = min(int(np.abs(scr - c).max()) for c in cands)
        checked += 1
        exact += d <= 2
        worst = max(worst, d)
    print(f'checked {checked} screens, exact {exact}, worst diff {worst}, late {e.w(RAM["LATE"])}')
    e.close()


if __name__ == '__main__':
    main()
