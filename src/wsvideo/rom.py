"""プレイヤー(player.bin)とストリームから .wsc ROM を組み立てる。"""
import struct
import sys
from pathlib import Path

from .encoder import BANK, ROM_SIZE_CODE  # noqa: E402

CFG = 0x10          # 'WSVP'
ITEMS = 0x20


def player_bin(name='player.bin'):
    base = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
    for p in (base / name, base / 'wsvideo' / name, Path(__file__).resolve().parent / name):
        if p.exists():
            return p.read_bytes()
    raise RuntimeError(f'{name} が見つかりません。build_player.py を実行してください。')


def build_nile_rom(wsv_name, s):
    """nileswan 版の ROM (1MB)。動画データは microSD の wsv_name から読む。"""
    nb = 16
    code = bytearray(player_bin('player_nile.bin'))
    assert code[CFG:CFG + 4] == b'WSVP'
    sh = getattr(s, 'speaker_shift', 1)
    code[CFG + 10] = sh
    code[CFG + 11] = min(1, sh)
    base, _, ext = wsv_name.upper().partition('.')
    code[0x20:0x2B] = (base.ljust(8) + ext.ljust(3)).encode('ascii')
    rom = bytearray(b'\xff' * (nb * BANK))
    rom[-BANK:] = code
    rom[-6] = ROM_SIZE_CODE[1]
    rom[-2:] = struct.pack('<H', sum(rom[:-2]) & 0xFFFF)
    return bytes(rom)


def bank_reg(pos, nbanks):
    """ROM 上の位置 → バンク番号レジスタの値。ROMの末尾を 0xFF とする数え方
    (ミラーのないフラッシュカートでも正しく動くように)。"""
    return ((pos >> 16) - nbanks) & 0xFF


SEEKIDX = 0x220     # 動画ごとのジャンプ表の始まり (u16 x 64)
SEEKTAB = 0x8000    # ジャンプ表 (8バイト: 映像バンク, 映像オフセット, 音声バンク, 音声オフセット, 予備)
SEEKMAX = (0xFFF0 - SEEKTAB) // 8


def build_rom(data, items, s, mb, flags=0, seeks=None, kf=0):
    """data: 映像+音声のストリーム、items: [(映像位置, 音声位置, フレーム数)]
    seeks: 動画ごとの [(映像位置, 音声位置)] (kf コマごとのジャンプ先)"""
    nb = mb * 16
    if len(data) > (nb - 1) * BANK:
        raise RuntimeError('ROM の容量を超えています。')
    code = bytearray(player_bin())
    assert code[CFG:CFG + 4] == b'WSVP'
    code[CFG + 4] = s.k
    code[CFG + 5] = s.audio_div
    code[CFG + 6] = flags
    code[CFG + 7] = len(items)
    code[CFG + 14] = getattr(s, 'lines', 159) - 1     # ポート0x16 (60Hz モードなら 199)
    if seeks and kf and sum(len(x) for x in seeks) <= SEEKMAX:
        assert len(code[:SEEKTAB].rstrip(b'\xff')) < SEEKTAB, 'プレイヤーがジャンプ表の場所まで大きくなった'
        code[CFG + 12:CFG + 14] = struct.pack('<H', kf)
        n = 0
        for i, lst in enumerate(seeks):
            code[SEEKIDX + 2 * i:SEEKIDX + 2 * i + 2] = struct.pack('<H', n)
            for vpos, apos in lst:
                o = SEEKTAB + 8 * n
                code[o:o + 8] = struct.pack('<BHBHH', bank_reg(vpos, nb), vpos & 0xFFFF,
                                            bank_reg(apos, nb), apos & 0xFFFF, 0)
                n += 1
    sh = getattr(s, 'speaker_shift', 1)
    code[CFG + 10] = sh             # 内蔵スピーカーのシフト初期値
    code[CFG + 11] = min(1, sh)     # 上ボタンで上げられる上限
    for i, (vpos, apos, n) in enumerate(items):
        o = ITEMS + 8 * i
        code[o:o + 8] = struct.pack('<BHBHH', bank_reg(vpos, nb), vpos & 0xFFFF,
                                    bank_reg(apos, nb), apos & 0xFFFF, n)
    rom = bytearray(b'\xff' * (nb * BANK))
    rom[:len(data)] = data
    rom[-BANK:] = code
    rom[-6] = ROM_SIZE_CODE[mb]
    rom[-2:] = struct.pack('<H', sum(rom[:-2]) & 0xFFFF)
    return bytes(rom)
