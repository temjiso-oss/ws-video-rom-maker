"""nileswan-medem (nileswan 対応の Mednafen) で ROM を動かし、決めた時刻に画面を保存する。
SD カードのイメージ (nileswan.img) は ROM と同じフォルダに置くこと。

    python tests/run_nile_emu.py rom.wsc 出力フォルダ 2,5,10 [--keys 6:START,9:START]

画面は Mednafen 自身のスクリーンショット (F9) で保存する (デスクトップは撮らない)。
キーはエミュレーターのウィンドウにだけメッセージで送る。
"""
import ctypes
import ctypes.wintypes as W
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# 環境変数 MEDEM=mod で、実機の速さに合わせて直したエミュレーター (tools/nileswan-emu/medem-mod) を使う
EXE = ROOT / 'tools' / 'nileswan-emu' / ('medem-mod' if os.environ.get('MEDEM') == 'mod' else 'medem') / 'nileswan-medem.exe'
u32 = ctypes.windll.user32
WM_KEYDOWN, WM_KEYUP, WM_CLOSE = 0x100, 0x101, 0x10
# Mednafen の WonderSwan の初期キー割り当て (X1-4 = W D S A, A = テンキー3, B = テンキー2, START = Enter)
VK = {'START': 0x0D, 'A': 0x63, 'B': 0x62, 'UP': 0x57, 'RIGHT': 0x44, 'DOWN': 0x53, 'LEFT': 0x41,
      'F9': 0x78, 'Y1': 0x26, 'Y2': 0x27, 'Y3': 0x28, 'Y4': 0x25}   # Y ボタン = 矢印キー
EXTENDED = {0x25, 0x26, 0x27, 0x28}


def find_window(pid):
    res = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, W.HWND, W.LPARAM)
    def cb(h, _):
        p = W.DWORD()
        u32.GetWindowThreadProcessId(h, ctypes.byref(p))
        if p.value == pid and u32.IsWindowVisible(h):
            res.append(h)
        return True

    u32.EnumWindows(cb, 0)
    return res[0] if res else None


def press(h, name, hold=0.15):
    if name in ('FF+', 'FF-'):          # 早送り (` キー) を押しっぱなし / 離す
        vk = 0xC0
        sc = 0x29                       # 物理キーの番号 (日本語配列でも同じ)
        if name == 'FF+':
            u32.PostMessageW(h, WM_KEYDOWN, vk, (sc << 16) | 1)
        else:
            u32.PostMessageW(h, WM_KEYUP, vk, (sc << 16) | 0xC0000001)
        return
    vk = VK[name]
    sc = u32.MapVirtualKeyW(vk, 0)
    ext = (1 << 24) if vk in EXTENDED else 0
    u32.PostMessageW(h, WM_KEYDOWN, vk, (sc << 16) | ext | 1)
    time.sleep(hold)
    u32.PostMessageW(h, WM_KEYUP, vk, (sc << 16) | ext | 0xC0000001)


def main():
    rom = Path(sys.argv[1]).resolve()
    out = Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    times = [float(x) for x in sys.argv[3].split(',')]
    keys = []
    if '--keys' in sys.argv:
        for kv in sys.argv[sys.argv.index('--keys') + 1].split(','):
            t, k = kv.split(':')
            keys.append((float(t), k))
    home = rom.parent / 'home'
    snaps = home / 'snaps'
    if snaps.exists():
        shutil.rmtree(snaps)
    home.mkdir(exist_ok=True)
    env = dict(os.environ, MEDNAFEN_HOME=str(home))
    log = open(out / 'emu_log.txt', 'w', encoding='utf-8', errors='replace')
    p = subprocess.Popen([str(EXE), str(rom)], cwd=str(rom.parent), env=env, stdout=log, stderr=subprocess.STDOUT)
    t0 = time.time()
    h = None
    events = sorted([(t, 'F9') for t in times] + keys)
    try:
        for t, k in events:
            while time.time() - t0 < t:
                time.sleep(0.02)
            h = h or find_window(p.pid)
            if h:
                press(h, k)
        time.sleep(0.5)
        if h:
            u32.PostMessageW(h, WM_CLOSE, 0, 0)
        try:
            p.wait(5)
        except subprocess.TimeoutExpired:
            p.kill()
    finally:
        if p.poll() is None:
            p.kill()
        log.close()
    shots = sorted(snaps.glob('*.png'), key=lambda x: x.stat().st_mtime) if snaps.exists() else []
    for i, s in enumerate(shots):
        tt = times[i] if i < len(times) else i
        shutil.copy(s, out / f'shot_{tt:05.1f}.png')
    print('screenshots:', len(shots))


if __name__ == '__main__':
    main()
