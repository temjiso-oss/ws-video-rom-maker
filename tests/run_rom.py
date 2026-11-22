"""ROM をエミュレーターで再生し、タイミング・音声・画面を確認する。
使い方: python tests/run_rom.py rom.wsc 出力フォルダ [フレーム数]"""
import sys
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from emu import Emu, RAM  # noqa: E402


def main():
    rom, out = sys.argv[1], Path(sys.argv[2])
    n = int(sys.argv[3]) if len(sys.argv) > 3 else 600
    out.mkdir(parents=True, exist_ok=True)
    e = Emu(rom)
    e.run(10)
    h0 = e.w(RAM['HBLCNT'])
    t0 = e.w(RAM['TICK'])
    log = []
    for i in range(n):
        e.run(1)
        if i % 75 == 0:
            e.image.save(out / f'f{i:05d}.png')
        log.append((e.w(RAM['TICK']), e.w(RAM['FRAMEI']), e.ram[RAM['ITEM']], e.w(RAM['LATE'])))
    h1 = e.w(RAM['HBLCNT'])
    t1 = e.w(RAM['TICK'])
    print('ticks', t1 - t0, 'hbl irq', (h1 - h0) & 0xFFFF, 'per tick', ((h1 - h0) & 0xFFFF) / max(1, t1 - t0))
    print('last: tick %d frame %d item %d late %d' % log[-1])
    pcm = e.pcm()
    print('audio samples', len(pcm), 'min/max', pcm.min(), pcm.max())
    with wave.open(str(out / 'audio.wav'), 'wb') as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(48000)
        w.writeframes(pcm.astype('<i2').tobytes())
    e.close()


if __name__ == '__main__':
    main()
