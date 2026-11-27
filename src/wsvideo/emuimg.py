"""nileswan のエミュレーター (nileswan-mesem) 用の microSD のイメージ (nileswan.img) を作る (1.6.0〜)。

nileswan-mesem は nileswan.ipl0 を開くと、同じフォルダーの nileswan.spi (カートのフラッシュ) と
nileswan.img (microSD) を使う。IPL はカードの /NILESWAN/MENU.WS を起動するので、
変換した .wsc を MENU.WS として入れると、メニューを通さずにすぐ動画が始まる。.WSV はルートに置く。

イメージはパーティションなしの FAT32 (手元の確認用のイメージと同じ形)。ファイルは連続したクラスタに置く。
"""
import os
import shutil
import struct
import time
from pathlib import Path

SEC = 512
RSVD = 32
NFATS = 2
MIN_CLUSTERS = 66000          # FAT32 と見なされるのは 65525 クラスタ以上
# 64MB だと nileswan の IPL が「File system not found」になった (128MB・512MB は動く。mesem-mod で確認、2026-11-27)
MIN_BYTES = 128 << 20
# エミュレーターは microSD を標準容量のカード (番地 = バイト、32 ビット) として扱うので 4GB 未満
MAX_BYTES = (4 << 30) - (1 << 20)
COPY_CHUNK = 4 << 20

README = """\
エミュレーターで見るための microSD のイメージです（WS Video ROM Maker が作りました）

中身: nileswan.img … {wsv}（動画のデータ）と、{wsc} を /NILESWAN/MENU.WS として入れてあります。

見かた:
1. nileswan のエミュレーター「nileswan-mesem」を用意します。
   https://github.com/49bitcat/nileswan-mesem （README の「Development Builds」の Windows 版）
2. nileswan のファームウェアのファイル nileswan.ipl0 と nileswan.spi を、このフォルダーに置きます。
   手に入れ方は nileswan のエミュレーターの説明を見てください:
   https://49bitcat.com/docs/nileswan/developer/emulator/
3. nileswan-mesem で、このフォルダーの nileswan.ipl0 を開きます。少し待つと動画が始まります。

メモ:
- nileswan.img の名前は変えないでください（nileswan.ipl0 と同じ名前の .img を読みます）。
- エミュレーターは実機より少し速く動くことがあります。見るだけなら問題ありません。
- 一時停止中の B（nileswan のメニューへ戻る）は、エミュレーターでは同じ動画が最初から始まります。
"""


def _name83(name):
    base, _, ext = name.upper().partition('.')
    if len(base) > 8 or len(ext) > 3:
        raise ValueError(f'8.3 の名前ではありません: {name}')
    return (base.ljust(8) + ext.ljust(3)).encode('ascii')


def _dirent(name11, attr, first, size, stamp):
    e = bytearray(32)
    e[0:11] = name11
    e[11] = attr
    t, d = stamp
    struct.pack_into('<HHH', e, 14, t, d, d)       # 作成時刻・作成日・アクセス日
    struct.pack_into('<H', e, 20, first >> 16)
    struct.pack_into('<HH', e, 22, t, d)           # 更新時刻・更新日
    struct.pack_into('<H', e, 26, first & 0xFFFF)
    struct.pack_into('<I', e, 28, size)
    return bytes(e)


def _layout(total_sec):
    """クラスタの大きさと FAT の大きさを決める"""
    spc = 64
    while spc > 1 and total_sec // spc < MIN_CLUSTERS:
        spc //= 2
    fatsz = 1
    for _ in range(8):
        nclus = (total_sec - RSVD - NFATS * fatsz) // spc
        fatsz = -(-(nclus + 2) * 4 // SEC)
    nclus = (total_sec - RSVD - NFATS * fatsz) // spc
    return spc, fatsz, nclus


def make_image(img_path, files, label='WSVIDEO'):
    """files: [(カードの中のパス 'NAME.EXT' か 'DIR/NAME.EXT', ローカルのファイル)]。フォルダーは 1 段まで。"""
    sizes = [os.path.getsize(src) for _, src in files]
    total = max(MIN_BYTES, int(sum(sizes) * 1.03) + (16 << 20))
    total_sec = -(-total // (1 << 20)) * (1 << 20) // SEC        # 1MB 単位
    if total_sec * SEC > MAX_BYTES:
        raise RuntimeError('動画が大きすぎて、エミュレーター用のイメージ（4GB まで）に入りません')
    spc, fatsz, nclus = _layout(total_sec)
    csize = spc * SEC
    if nclus < 65525:
        raise RuntimeError('イメージが小さすぎます')
    lt = time.localtime()
    stamp = (lt.tm_hour << 11 | lt.tm_min << 5 | lt.tm_sec // 2, (lt.tm_year - 1980) << 9 | lt.tm_mon << 5 | lt.tm_mday)

    fat = {0: 0x0FFFFFF8, 1: 0x0FFFFFFF, 2: 0x0FFFFFFF}      # 2 = ルート
    nxt = 3
    dirs = {}                       # フォルダー名 → クラスタ
    root = [_dirent(label.upper()[:11].ljust(11).encode('ascii'), 0x08, 0, 0, stamp)]    # ボリュームの名前
    sub = {}                        # クラスタ → エントリーの並び
    for path, _ in files:
        if '/' in path:
            d = path.split('/')[0]
            if d not in dirs:
                dirs[d] = nxt
                fat[nxt] = 0x0FFFFFFF
                root.append(_dirent(_name83(d), 0x10, nxt, 0, stamp))
                sub[nxt] = [_dirent(b'.          ', 0x10, nxt, 0, stamp), _dirent(b'..         ', 0x10, 0, 0, stamp)]
                nxt += 1
    place = []
    for (path, src), size in zip(files, sizes):
        n = max(1, -(-size // csize))
        first = nxt
        for c in range(first, first + n - 1):
            fat[c] = c + 1
        fat[first + n - 1] = 0x0FFFFFFF
        nxt += n
        if nxt > nclus + 2:
            raise RuntimeError('イメージに入りきりません')
        name = path.split('/')[-1]
        ent = _dirent(_name83(name), 0x20, first, size, stamp)
        (sub[dirs[path.split('/')[0]]] if '/' in path else root).append(ent)
        place.append((src, first))
    if len(root) * 32 > csize or any(len(v) * 32 > csize for v in sub.values()):
        raise RuntimeError('ファイルが多すぎます')

    data0 = RSVD + NFATS * fatsz

    def coff(c):
        return (data0 + (c - 2) * spc) * SEC

    bpb = bytearray(SEC)
    bpb[0:3] = b'\xEB\x58\x90'
    bpb[3:11] = b'MSWIN4.1'
    struct.pack_into('<HBHBHHBHHHII', bpb, 11, SEC, spc, RSVD, NFATS, 0, 0, 0xF8, 0, 63, 255, 0, total_sec)
    struct.pack_into('<IHHIHH', bpb, 36, fatsz, 0, 0, 2, 1, 6)
    struct.pack_into('<BBBI', bpb, 64, 0x80, 0, 0x29, int(time.time()) & 0xFFFFFFFF)
    bpb[71:82] = label.upper()[:11].ljust(11).encode('ascii')
    bpb[82:90] = b'FAT32   '
    bpb[510:512] = b'\x55\xAA'
    fsinfo = bytearray(SEC)
    struct.pack_into('<I', fsinfo, 0, 0x41615252)
    struct.pack_into('<III', fsinfo, 484, 0x61417272, nclus - (nxt - 2), nxt)
    struct.pack_into('<I', fsinfo, 508, 0xAA550000)
    fatb = bytearray(fatsz * SEC)
    for c, v in fat.items():
        struct.pack_into('<I', fatb, c * 4, v)

    img_path = Path(img_path)
    img_path.parent.mkdir(parents=True, exist_ok=True)
    with open(img_path, 'wb') as f:
        f.truncate(total_sec * SEC)
        for base in (0, 6):
            f.seek(base * SEC)
            f.write(bpb)
            f.write(fsinfo)
        for k in range(NFATS):
            f.seek((RSVD + k * fatsz) * SEC)
            f.write(fatb)
        f.seek(coff(2))
        f.write(b''.join(root))
        for c, ents in sub.items():
            f.seek(coff(c))
            f.write(b''.join(ents))
        for src, first in place:
            f.seek(coff(first))
            with open(src, 'rb') as g:
                shutil.copyfileobj(g, f, COPY_CHUNK)
    return img_path


def make_for(wsc_path, wsv_path):
    """変換した .wsc と .WSV から、横に「名前_emu」フォルダーを作り、nileswan.img と説明を置く。フォルダーを返す。"""
    wsc_path, wsv_path = Path(wsc_path), Path(wsv_path)
    folder = wsc_path.with_name(wsc_path.stem + '_emu')
    make_image(folder / 'nileswan.img', [(wsv_path.name, wsv_path), ('NILESWAN/MENU.WS', wsc_path)])
    (folder / 'はじめにお読みください.txt').write_text(
        README.format(wsv=wsv_path.name, wsc=wsc_path.name), encoding='utf-8-sig')
    return folder
