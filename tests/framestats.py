"""1本の動画を nileswan 版で変換しながら、コマごとの (展開の重み, 読み込み込みの重み, バイト数, 全体書き直しか) を記録する。
python framestats.py 入力.mp4 出力フォルダ [開始秒 長さ秒] [--sd 250] [--k 5]"""
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from wsvideo import encoder  # noqa: E402


def main():
    args = [a for a in sys.argv[1:]]
    sd = 250
    k = 5
    if '--sd' in args:
        i = args.index('--sd')
        sd = int(args[i + 1])
        del args[i:i + 2]
    if '--k' in args:
        i = args.index('--k')
        k = int(args[i + 1])
        del args[i:i + 2]
    src, out = args[0], Path(args[1])
    start = float(args[2]) if len(args) > 2 else 0.0
    length = float(args[3]) if len(args) > 3 else None
    out.mkdir(parents=True, exist_ok=True)
    log = []
    pend = []
    orig = encoder.choose_options
    orig_runs = encoder.runs_of

    def wrap(err, byt, unt, lam, bbud, maxunits, force=False):
        r = orig(err, byt, unt, lam, bbud, maxunits, force)
        if r is not None:
            ch = r[0]
            idx = np.arange(len(ch))
            b = float(byt[idx, ch].sum())
            u = float(unt[idx, ch].sum())
            pend.append((u - encoder.NILE_READ_UNIT * b, u, b, 0))
        return r

    def runs(sel):
        if pend:
            log.append(pend.pop())
        else:                       # 全体の書き直し (パレット切り替え・ジャンプ先)
            n = len(sel)
            b = 34.0 * n + 32 * 16
            log.append((float(n), n + encoder.NILE_READ_UNIT * b, b, 1))
        return orig_runs(sel)

    encoder.choose_options = wrap
    encoder.runs_of = runs
    s = encoder.Settings(target='nile', k=k, hz60=True, sd_kbps=sd)
    print('budget', encoder.unit_budget(s), 'fps', s.fps, flush=True)
    t0 = time.time()
    encoder.convert([encoder.Item(src, start, length)], s, str(out / 'pc.wsc'), progress=None, log=print)
    np.save(out / 'stats.npy', np.array(log, np.float64))
    print('done', time.time() - t0, len(log), flush=True)


if __name__ == '__main__':
    main()
