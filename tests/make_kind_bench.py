"""タイルの種類ごとの展開の重さを実機で測るための .WSV と ROM を作る (2026-11-25)。

1 本の .WSV に、1 種類のタイルだけでできた動画を 8 本入れる。速さの測定用プレイヤー (-DBENCH) は、
動画を 1 本ずつ測り、最後に「番号 コマ数 VBlank数 SD」の表を出す。
1 コマの VBlank 数 x 199 x 256 / 504 = タイル 1 枚のサイクル (60Hz、1 コマ 504 枚)。

    python -X utf8 tests/make_kind_bench.py 出力フォルダ
    → KINDS.WSV と KINDBNCH.wsc (microSD のルートに .WSV、.wsc は好きな所)
"""
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from wsvideo import encoder as E, rom  # noqa: E402

NT = 504
FRAMES = 400
KINDS = ['bulk', 'full', 'c4', 'c2', 'solid', 'copy0', 'copyv', 'copyh']
rng = np.random.default_rng(5)


def mw(t, b, flag=0, pal=0):
    return struct.pack('<H', (t | (pal << 9) | (b << 13) | flag) & 0xFFFF)


def frame(kind, b):
    """1 コマ (コマは偶数の番地から始まる前提。まとめ書きの詰め物はコマの中の位置で決める)"""
    out = bytearray([0])
    if kind == 'bulk':
        t = 0
        while t < NT:
            n = min(64, NT - t)
            out += bytes([0, 0x80 | n])
            for i in range(n):
                out += mw(t + i, b, pal=(t + i) % 16)
            if len(out) & 1:
                out += b'\0'
            out += rng.integers(0, 256, 32 * n, dtype=np.uint8).tobytes()
            t += n
        return bytes(out)
    t = 0
    while t < NT:
        n = min(126, NT - t)
        out += bytes([0, n])
        for i in range(n):
            ti = t + i
            tx, ty = (ti % 28) * 8, (ti // 28) * 8
            if kind == 'full':
                out += mw(ti, b, pal=ti % 16) + rng.integers(0, 256, 32, dtype=np.uint8).tobytes()
            elif kind == 'c4':
                out += mw(ti, b, 0xC000, ti % 16) + bytes([2]) + rng.integers(0, 256, 18, dtype=np.uint8).tobytes()
            elif kind == 'c2':
                out += mw(ti, b, 0xC000, ti % 16) + bytes([1]) + rng.integers(0, 256, 9, dtype=np.uint8).tobytes()
            elif kind == 'solid':
                out += mw(ti, b, 0x8000, ti % 16) + bytes([int(rng.integers(0, 256))])
            else:
                if kind == 'copy0':
                    sx, sy = tx, ty
                elif kind == 'copyv':
                    sx, sy = tx, min(136, ty + 3)
                else:
                    sx, sy = min(216, tx + 2), ty
                out += mw(ti, b, 0x4000, ti % 16) + bytes([sx, sy])
        t += n
    return bytes(out)


def main():
    outdir = Path(sys.argv[1])
    outdir.mkdir(parents=True, exist_ok=True)
    s = E.Settings(k=3, target='nile', hz60=True, sd_kbps=350, audio_div=1)
    sw = E.StreamWriter(True)
    starts, fstarts, audios, nframes = [], [], [], []
    for kind in KINDS:
        sw.align()
        starts.append(sw.pos)
        fstarts.append(len(sw.frame_pos))
        for f in range(FRAMES):
            sw.place(frame(kind, f & 1))
        nframes.append(FRAMES)
        audios.append(np.full(E.audio_len(FRAMES, s), 0x80, np.uint8))
    wsv = outdir / 'KINDS.WSV'
    E.write_wsv(str(wsv), sw, starts, fstarts, audios, nframes, s, 0)
    # 速さの測定用プレイヤー (-DBENCH) で ROM を作る
    with tempfile.TemporaryDirectory() as td:
        binp = Path(td) / 'p.bin'
        subprocess.run([str(ROOT / 'tools' / 'nasm' / 'nasm.exe'), '-f', 'bin', '-I', str(ROOT / 'src' / 'player') + '/',
                        '-DNILE', '-DBENCH', str(ROOT / 'src' / 'player' / 'player.asm'), '-o', str(binp)], check=True)
        data = binp.read_bytes()
        orig = rom.player_bin
        rom.player_bin = lambda name: data
        try:
            (outdir / 'KINDBNCH.wsc').write_bytes(rom.build_nile_rom(wsv.name, E.Settings()))
        finally:
            rom.player_bin = orig
    print('ok', wsv, wsv.stat().st_size, 'bytes,', ' '.join(f'{i + 1}={k}' for i, k in enumerate(KINDS)))


if __name__ == '__main__':
    main()
