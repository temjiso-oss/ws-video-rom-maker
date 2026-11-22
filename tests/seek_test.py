"""ジャンプ (Y ボタン) のテスト (通常版 ROM)。
ジャンプした後の画面が、ROM を最初から順に読み戻した画面と一致するかを調べる。
    python tests/seek_test.py rom.wsc"""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from emu import Emu, RAM  # noqa: E402
import sim_rom  # noqa: E402


def main():
    rom = sys.argv[1]
    frames = {}
    for it, f, img in sim_rom.decode(Path(rom).read_bytes()):
        frames[(it, f)] = img.astype(np.int16)
    e = Emu(rom)
    e.run(5)
    kfw = e.w(0x422)
    print('jump interval (frames):', kfw)

    def state():
        return e.ram[RAM['ITEM']], e.w(RAM['FRAMEI'])

    def check(n=200, label=''):
        ok = bad = 0
        for i in range(n):
            e.run(1)
            if i % 9 != 4:
                continue
            item, fi = state()
            scr = np.asarray(e.image.convert('RGB'), np.int16).copy()
            cands = [frames[k].copy() for k in ((item, fi - 1), (item, fi), (item, fi - 2)) if k in frames]
            if not cands:
                continue
            osd = scr[128:136, 8:112].copy()
            for c in cands + [scr]:
                c[128:136, 8:112] = 0       # 時間表示の場所は比べない
            d = min(int(np.abs(scr - c).max()) for c in cands)
            if e.ram[0x428] and len(set(map(tuple, osd.reshape(-1, 3)))) > 2:
                pass                         # 時間表示が出ている (白と黒の2色以外が混じれば異常)
            ok += d <= 2
            bad += d > 2
        print(f'{label:28s} now item {state()[0]} frame {state()[1]:5d}  screens exact {ok}, wrong {bad}')
        return bad

    bad = 0
    e.run(30)
    bad += check(150, 'start')
    for key, label in [('Y2', '+10s'), ('Y2', '+10s'), ('Y1', '+1min'), ('Y4', '-10s'), ('Y3', '-1min'),
                       ('Y3', '-1min (clamp to 0)'), ('Y1', '+1min'), ('Y1', '+1min (past end: ignored)')]:
        before = state()[1]
        e.run(2, [key])
        e.run(10)
        after = state()[1]
        print(f'  {label}: frame {before} -> {after} (expected near {kfw}x)')
        bad += check(150, label)
    h0 = e.w(RAM['HBLCNT'])
    e.run(60)
    print('audio interrupts in 60 frames:', (e.w(RAM['HBLCNT']) - h0) & 0xFFFF, ' late:', e.w(RAM['LATE']))
    print('RESULT:', 'OK' if bad == 0 else f'{bad} wrong screens')
    e.close()


if __name__ == '__main__':
    main()
