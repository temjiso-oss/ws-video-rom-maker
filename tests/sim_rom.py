"""ROM のストリームを Python で読み戻して画面を再現する(プレイヤーと同じ手順)。
使い方: python tests/sim_rom.py rom.wsc 出力フォルダ [間引き]"""
import struct
import sys
from pathlib import Path

import numpy as np
from PIL import Image

NT = 504


def decode(rom):
    nb = len(rom) // 65536
    code = rom[-65536:]
    assert code[16:20] == b'WSVP'
    k, r, flags, count = code[20:24]
    ram = bytearray(65536)
    for it in range(count):
        vb, vo, ab, ao, n = struct.unpack_from('<BHBHH', code, 0x20 + 8 * it)
        bank = (vb + nb) & 0xFF
        si = vo
        buf = 0
        for f in range(n):
            tbase, mbase, pbase = (0x4000, 0x1000, 0xFE00) if buf == 0 else (0x8000, 0x1800, 0xFF00)
            while True:
                hdr = rom[bank * 65536 + si]
                si += 1
                if hdr & 0x80:
                    bank += 1
                    si = 0
                    continue
                break
            base = bank * 65536
            if hdr & 2:                         # 16パレット: 表示する瞬間にパレットRAM全体へ
                ram[0xFE00:0x10000] = rom[base + si:base + si + 512]
                si += 512
            elif hdr & 1:
                ram[pbase:pbase + 256] = rom[base + si:base + si + 256]
                si += 256
            dx = 0
            while dx < NT:
                skip, cnt = rom[base + si], rom[base + si + 1]
                si += 2
                dx += skip
                for _ in range(cnt):
                    mw = bytearray(rom[base + si:base + si + 2])
                    mo = ((dx // 28) * 32 + dx % 28) * 2
                    if (mw[1] & 0xC0) == 0xC0:          # 色の少ないタイル
                        mw[1] &= 0x3F
                        sub = rom[base + si + 2]
                        if sub == 1:
                            c = rom[base + si + 3]
                            cols = [c >> 4, c & 15]
                            bits = rom[base + si + 4:base + si + 12]
                            px = [cols[(bits[y] >> (7 - x)) & 1] for y in range(8) for x in range(8)]
                            si += 12
                        else:
                            c0, c1 = rom[base + si + 3], rom[base + si + 4]
                            cols = [c0 >> 4, c0 & 15, c1 >> 4, c1 & 15]
                            data = rom[base + si + 5:base + si + 21]
                            px = []
                            for byte in data:
                                px += [cols[(byte >> s) & 3] for s in (6, 4, 2, 0)]
                            si += 21
                        ram[tbase + dx * 32:tbase + dx * 32 + 32] = bytes(px[i] << 4 | px[i + 1] for i in range(0, 64, 2))
                    elif mw[1] & 0x80:
                        mw[1] &= 0x7F
                        ram[tbase + dx * 32:tbase + dx * 32 + 32] = bytes([rom[base + si + 2]]) * 32
                        si += 3
                    elif mw[1] & 0x40:
                        mw[1] &= 0xBF
                        sx, sy = rom[base + si + 2], rom[base + si + 3]
                        ram[tbase + dx * 32:tbase + dx * 32 + 32] = copy_tile(ram, 0x8000 if buf == 0 else 0x4000, sx, sy)
                        si += 4
                    else:
                        ram[tbase + dx * 32:tbase + dx * 32 + 32] = rom[base + si + 2:base + si + 34]
                        si += 34
                    ram[mbase + mo:mbase + mo + 2] = mw
                    dx += 1
            yield it, f, render(ram, mbase)
            buf ^= 1


def copy_tile(ram, obase, sx, sy):
    """表示中のバッファの画面座標 (sx, sy) から 8x8 を取り出す (4bpp パックド 32 バイト)"""
    out = bytearray()
    for r in range(8):
        y = sy + r
        row = bytearray()
        for bx in range(4):
            x = sx + bx * 2                       # sx は偶数なので 1 バイト = 2 ドット単位
            t = (y // 8) * 28 + x // 8
            row.append(ram[obase + t * 32 + (y % 8) * 4 + (x % 8) // 2])
        out += row
    return bytes(out)


def render(ram, mbase):
    pal = np.frombuffer(bytes(ram[0xFE00:0x10000]), '<u2').reshape(16, 16)
    rgb = np.stack([(pal >> 8) & 15, (pal >> 4) & 15, pal & 15], -1).astype(np.uint8) * 17
    img = np.zeros((144, 224, 3), np.uint8)
    for ty in range(18):
        for tx in range(28):
            e = ram[mbase + (ty * 32 + tx) * 2] | (ram[mbase + (ty * 32 + tx) * 2 + 1] << 8)
            t = (e & 0x1FF) | ((e >> 13) & 1) << 9
            p = (e >> 9) & 15
            data = ram[0x4000 + t * 32:0x4000 + t * 32 + 32]
            px = np.frombuffer(bytes(data), np.uint8)
            idx = np.stack([px >> 4, px & 15], 1).reshape(8, 8)
            col = rgb[p][idx]
            col[idx == 0] = rgb[0][0]
            img[ty * 8:ty * 8 + 8, tx * 8:tx * 8 + 8] = col
    return img


def main():
    rom = Path(sys.argv[1]).read_bytes()
    out = Path(sys.argv[2])
    step = int(sys.argv[3]) if len(sys.argv) > 3 else 15
    out.mkdir(parents=True, exist_ok=True)
    for it, f, img in decode(rom):
        if f % step == 0:
            Image.fromarray(img).save(out / f'i{it}_f{f:05d}.png')
    print('ok')


if __name__ == '__main__':
    main()
