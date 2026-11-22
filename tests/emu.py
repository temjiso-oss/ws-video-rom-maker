"""libretro の Beetle Cygne (mednafen_wswan) を Python から動かす小さなハーネス。
ROM を起動し、ボタンを押し、画面と音声と RAM を取り出す。"""
import ctypes as C
import os
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
CORE = Path(os.environ.get('WSWAN_CORE', ROOT / 'tools' / 'emu' / 'mednafen_wswan_libretro.dll'))
# 0 B, 3 START, 4 UP, 5 DOWN, 6 LEFT, 7 RIGHT, 8 A  (X1-X4 = 上右下左)
KEY = dict(B=0, START=3, UP=4, DOWN=5, LEFT=6, RIGHT=7, A=8, Y4=10, Y2=11, Y3=12, Y1=13)


class Game(C.Structure):
    _fields_ = [('path', C.c_char_p), ('data', C.c_void_p), ('size', C.c_size_t), ('meta', C.c_char_p)]


class Var(C.Structure):
    _fields_ = [('key', C.c_char_p), ('value', C.c_char_p)]


ENV = C.CFUNCTYPE(C.c_bool, C.c_uint, C.c_void_p)
VIDEO = C.CFUNCTYPE(None, C.c_void_p, C.c_uint, C.c_uint, C.c_size_t)
AUDIO = C.CFUNCTYPE(None, C.c_int16, C.c_int16)
BATCH = C.CFUNCTYPE(C.c_size_t, C.c_void_p, C.c_size_t)
POLL = C.CFUNCTYPE(None)
INPUT = C.CFUNCTYPE(C.c_int16, C.c_uint, C.c_uint, C.c_uint, C.c_uint)


class Emu:
    def __init__(self, rom):
        self.options = {b'wswan_sound_sample_rate': b'48000', b'wswan_gfx_colors': b'24bit',
                        b'wswan_60hz_mode': b'disabled', b'wswan_rotate_display': b'landscape',
                        b'wswan_rotate_keymap': b'disabled', b'wswan_frameskip': b'disabled',
                        b'wswan_sound_low_pass': b'disabled'}
        self.buttons = set()
        self.image = None
        self.audio = []
        self.directory = str(ROOT / 'tests' / 'out').encode()
        emu = self

        @ENV
        def environment(cmd, data):
            if cmd == 15:
                v = C.cast(data, C.POINTER(Var)).contents
                if v.key in emu.options:
                    v.value = emu.options[v.key]
                    return True
                return False
            if cmd == 17:
                C.cast(data, C.POINTER(C.c_bool))[0] = False
                return True
            if cmd in (9, 31):
                C.cast(data, C.POINTER(C.c_char_p))[0] = emu.directory
                return True
            return cmd in (1, 8, 10, 11, 16, 18, 32, 35, 36, 37)

        @VIDEO
        def video(data, w, h, pitch):
            if not data:
                return
            raw = C.string_at(data, pitch * h)
            emu.image = Image.frombytes('RGB', (w, h), raw, 'raw', 'BGRX', pitch)

        @AUDIO
        def audio(l, r):
            pass

        @BATCH
        def batch(data, count):
            emu.audio.append(C.string_at(data, count * 4))
            return count

        @POLL
        def poll():
            pass

        @INPUT
        def input_state(port, device, index, key):
            return int(port == 0 and key in emu.buttons)

        self._cb = (environment, video, audio, batch, poll, input_state)
        self.lib = C.CDLL(str(CORE))
        for name, typ, cb in [('environment', ENV, environment), ('video_refresh', VIDEO, video),
                              ('audio_sample', AUDIO, audio), ('audio_sample_batch', BATCH, batch),
                              ('input_poll', POLL, poll), ('input_state', INPUT, input_state)]:
            f = getattr(self.lib, 'retro_set_' + name)
            f.argtypes = [typ]
            f(cb)
        self.lib.retro_load_game.argtypes = [C.POINTER(Game)]
        self.lib.retro_load_game.restype = C.c_bool
        self.lib.retro_get_memory_data.argtypes = [C.c_uint]
        self.lib.retro_get_memory_data.restype = C.c_void_p
        self.lib.retro_get_memory_size.argtypes = [C.c_uint]
        self.lib.retro_get_memory_size.restype = C.c_size_t
        self.lib.retro_init()
        self.rom = Path(rom).read_bytes()
        self.rom_buf = C.create_string_buffer(self.rom, len(self.rom))
        game = Game(str(rom).encode(), C.cast(self.rom_buf, C.c_void_p), len(self.rom), None)
        assert self.lib.retro_load_game(C.byref(game)), 'ROM load failed'
        ptr = self.lib.retro_get_memory_data(2)
        self.ram = (C.c_uint8 * self.lib.retro_get_memory_size(2)).from_address(ptr)
        self.frames = 0

    def w(self, a):
        return self.ram[a] | (self.ram[a + 1] << 8)

    def run(self, n=1, keys=()):
        self.buttons = set(KEY[k] for k in keys)
        for _ in range(n):
            self.lib.retro_run()
            self.frames += 1

    def pcm(self):
        a = np.frombuffer(b''.join(self.audio), np.int16).reshape(-1, 2)
        return a

    def close(self):
        self.lib.retro_unload_game()
        self.lib.retro_deinit()


# プレイヤーの RAM 変数 (player.asm の absolute 0x0400 と同じ並び)
RAM = dict(TICK=0x400, NEXTT=0x402, PENDING=0x404, BUF=0x405, VBANK=0x406, ABANK=0x407, VPTR=0x408,
           APTR=0x40A, FRAMEI=0x40C, FRAMEN=0x40E, ITEM=0x410, STARTED=0x411, KEYS=0x412, PREVK=0x413,
           NEWK=0x414, VOL=0x415, TBASE=0x416, MBASE=0x418, LATE=0x41A, HBLCNT=0x41C, PAUSED=0x41E)
