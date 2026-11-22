"""1コマあたりのタイル数の上限を変えながら ROM を作り、エミュレーターで
間に合わなかったコマ数(LATE)を数える。
使い方: python tests/measure_budget.py 動画 作業フォルダ"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'tests'))
from wsvideo import encoder as E  # noqa: E402
from emu import Emu, RAM  # noqa: E402


def trial(video, work, k, maxt, audio_div=1):
    E.MAX_TILES[k] = maxt
    out = Path(work) / f'b_k{k}_{maxt}.wsc'
    s = E.Settings(k=k, audio_div=audio_div, quality='best', max_rom_mb=16)
    r = E.convert([E.Item(video, 0, 8)], s, str(out))
    e = Emu(str(out))
    e.run(int(r.frames * k) - 20)
    fi, late = e.w(RAM['FRAMEI']), e.w(RAM['LATE'])
    e.close()
    return r.avg_tiles, fi, late


def main():
    video, work = sys.argv[1], sys.argv[2]
    ks = [int(x) for x in sys.argv[3].split(',')] if len(sys.argv) > 3 else [1, 2, 3, 4]
    for k in ks:
        for maxt in (100, 150, 200, 260, 330, 400, 504):
            avg, fi, late = trial(video, work, k, maxt)
            print(f'K={k} maxt={maxt:3d} avg={avg:5.1f} frames={fi} late={late}', flush=True)
            if late > fi // 10:
                break


if __name__ == '__main__':
    main()
