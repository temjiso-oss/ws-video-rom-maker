"""ffmpeg を使った動画・音声の読み込み。

ffmpeg は次の順で探す:
  1. exe 化したアプリに同梱した ffmpeg.exe
  2. imageio-ffmpeg パッケージに入っている ffmpeg
  3. PATH 上の ffmpeg
"""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np

W, H = 224, 144
NO_WINDOW = 0x08000000 if os.name == 'nt' else 0


def ffmpeg_exe():
    if getattr(sys, 'frozen', False):
        p = Path(getattr(sys, '_MEIPASS', Path(sys.executable).parent)) / 'ffmpeg.exe'
        if p.exists():
            return str(p)
        p = Path(sys.executable).parent / 'ffmpeg.exe'
        if p.exists():
            return str(p)
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    p = shutil.which('ffmpeg')
    if p:
        return p
    raise RuntimeError('ffmpeg が見つかりません。')


class MediaInfo:
    def __init__(self, path, duration, has_video, has_audio, desc):
        self.path = path
        self.duration = duration
        self.has_video = has_video
        self.has_audio = has_audio
        self.desc = desc


def probe(path):
    r = subprocess.run([ffmpeg_exe(), '-hide_banner', '-i', str(path)], capture_output=True,
                       creationflags=NO_WINDOW)
    err = r.stderr.decode('utf-8', errors='replace')
    m = re.search(r'Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)', err)
    dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else None
    vids = [ln for ln in err.splitlines() if re.search(r'Stream #.*Video:', ln)]
    has_v = any('attached pic' not in ln for ln in vids)
    has_a = bool(re.search(r'Stream #.*Audio:', err))
    vm = re.search(r'Video:\s*([^,\n]+)[^\n]*?(\d{2,5})x(\d{2,5})', err)
    desc = f'{vm.group(1).split()[0]} {vm.group(2)}x{vm.group(3)}' if vm else ''
    if dur is None and not has_v:
        raise RuntimeError(f'動画として読めませんでした: {path}')
    return MediaInfo(path, dur, has_v, has_a, desc)


def _trim_args(start, length):
    a = []
    if start:
        a += ['-ss', f'{start:.3f}']
    return a, (['-t', f'{length:.3f}'] if length else [])


def video_filter(fps_expr, mode='fit', brightness=0.0, contrast=1.0, saturation=1.0, deinterlace=False):
    f = []
    if deinterlace:
        f.append('yadif')
    f.append(f'fps={fps_expr}')
    f.append('scale=trunc(iw*sar/2)*2:ih,setsar=1')
    if mode == 'fill':
        f.append(f'scale={W}:{H}:force_original_aspect_ratio=increase:flags=lanczos,crop={W}:{H}')
    elif mode == 'stretch':
        f.append(f'scale={W}:{H}:flags=lanczos')
    else:
        f.append(f'scale={W}:{H}:force_original_aspect_ratio=decrease:flags=lanczos,'
                 f'pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:black')
    if brightness or contrast != 1.0 or saturation != 1.0:
        f.append(f'eq=brightness={brightness:.3f}:contrast={contrast:.3f}:saturation={saturation:.3f}')
    f.append('format=rgb24')
    return ','.join(f)


def read_frames(path, fps_expr, start=0.0, length=None, max_frames=None, **kw):
    """(144, 224, 3) uint8 のフレームを順に返す。"""
    pre, post = _trim_args(start, length)
    if max_frames:
        post = post + ['-frames:v', str(max_frames)]
    cmd = [ffmpeg_exe(), '-hide_banner', '-loglevel', 'error', '-nostdin'] + pre + ['-i', str(path)] + post + \
          ['-an', '-sn', '-vf', video_filter(fps_expr, **kw), '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-']
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=NO_WINDOW)
    size = W * H * 3
    try:
        while True:
            buf = p.stdout.read(size)
            if len(buf) < size:
                break
            yield np.frombuffer(buf, np.uint8).reshape(H, W, 3)
    finally:
        p.stdout.close()
        err = p.stderr.read().decode('utf-8', errors='replace')
        p.wait()
    if p.returncode not in (0, None) and err.strip():
        raise RuntimeError('ffmpeg エラー: ' + err.strip()[-500:])


def read_audio(path, rate, start=0.0, length=None, normalize=True, volume=1.0):
    """8bit 符号なしモノラル (中心 0x80) の numpy 配列。音声がなければ空配列。"""
    pre, post = _trim_args(start, length)
    af = []
    if normalize:
        af.append('dynaudnorm=f=200:g=11:p=0.9')
    if volume != 1.0:
        af.append(f'volume={volume:.3f}')
    cmd = [ffmpeg_exe(), '-hide_banner', '-loglevel', 'error', '-nostdin'] + pre + ['-i', str(path)] + post + \
          ['-vn', '-sn', '-ac', '1', '-ar', str(rate)] + (['-af', ','.join(af)] if af else []) + \
          ['-f', 'u8', '-']
    r = subprocess.run(cmd, capture_output=True, creationflags=NO_WINDOW)
    return np.frombuffer(r.stdout, np.uint8)


class PreviewWriter:
    """変換結果のシミュレーション映像を mp4 に書き出す。write(frame) を順に呼び、最後に close()。"""

    def __init__(self, path, fps_expr, audio=None, audio_rate=12000, scale=2):
        import tempfile
        self.tmp = None
        cmd = [ffmpeg_exe(), '-hide_banner', '-loglevel', 'error', '-y', '-f', 'rawvideo', '-pix_fmt', 'rgb24',
               '-s', f'{W}x{H}', '-framerate', fps_expr, '-i', '-']
        if audio is not None and len(audio):
            self.tmp = tempfile.NamedTemporaryFile(suffix='.u8', delete=False)
            self.tmp.write(audio.tobytes())
            self.tmp.close()
            cmd += ['-f', 'u8', '-ar', str(audio_rate), '-ac', '1', '-i', self.tmp.name]
        cmd += ['-vf', f'scale={W * scale}:{H * scale}:flags=neighbor', '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
                '-crf', '18']
        if self.tmp:
            cmd += ['-c:a', 'aac', '-b:a', '96k', '-shortest']
        cmd += [str(path)]
        self.p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)

    def write(self, frame):
        self.p.stdin.write(np.ascontiguousarray(frame, np.uint8).tobytes())

    def close(self):
        self.p.stdin.close()
        self.p.wait()
        if self.tmp:
            os.unlink(self.tmp.name)
