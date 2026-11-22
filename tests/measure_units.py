"""タイルの種類ごとの展開の重さ (普通のタイル = 1) をエミュレーターで測る。
1コマに決まった種類のタイルだけを N 枚ずつ送る ROM を作り、間に合わなかったコマ (LATE) が出始める N を比べる。
使い方: python tests/measure_units.py 動画 作業フォルダ [K]"""
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'tests'))
from wsvideo import encoder as E  # noqa: E402
from emu import Emu, RAM  # noqa: E402

ORIG = E.choose_options


def only(opt):
    def f(err, byt, unt, lam, bbud, maxunits, force=False):
        err = err.copy()
        for o in range(1, err.shape[1]):
            if o != opt:
                err[:, o] = np.inf
        err[:, 0] = np.where(np.isfinite(err[:, opt]), np.inf, err[:, 0])
        unt = unt.copy()
        unt[:, opt] = 1.0
        return ORIG(err, byt, unt, 0.0, np.inf, maxunits, force=True)
    return f


def trial(video, work, k, opt, n):
    E.choose_options = only(opt)
    E.MAX_UNITS[k] = n
    out = Path(work) / f'u{opt}_{n}.wsc'
    s = E.Settings(k=k, quality='best', max_rom_mb=16)
    r = E.convert([E.Item(video, 0, 6)], s, str(out))
    e = Emu(str(out))
    e.run(int(r.frames * k) - 20)
    fi, late = e.w(RAM['FRAMEI']), e.w(RAM['LATE'])
    e.close()
    return r.avg_tiles, fi, late


def main():
    video, work = sys.argv[1], sys.argv[2]
    k = int(sys.argv[3]) if len(sys.argv) > 3 else 2
    for opt, name in ((1, 'full'), (4, 'c4'), (3, 'c2')):
        for n in (40, 60, 80, 100, 120, 140, 160, 190, 220, 260, 330, 400, 504):
            avg, fi, late = trial(video, work, k, opt, n)
            print(f'{name} n={n:3d} avg={avg:5.1f} frames={fi} late={late}', flush=True)
            if late > fi // 20:
                break


if __name__ == '__main__':
    main()
