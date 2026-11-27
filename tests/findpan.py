"""動画全体から、横パン（画面全体の横ずれ）が多い区間を探す。"""
import subprocess, sys
import numpy as np
sys.path.insert(0, r'J:\01_AI-Projects\projects\ws-video-rom-maker\src')
from wsvideo import media

video = sys.argv[1]
W, H, FPS = 224, 144, 10
cmd = [media.ffmpeg_exe(), '-v', 'error', '-i', video, '-vf', f'fps={FPS},scale={W}:{H},format=gray',
       '-f', 'rawvideo', '-']
raw = subprocess.run(cmd, capture_output=True, check=True).stdout
fr = np.frombuffer(raw, np.uint8).reshape(-1, H, W).astype(np.float32)
print('frames', len(fr))
win = np.hanning(W)[None, :] * np.hanning(H)[:, None]
dx = np.zeros(len(fr)); conf = np.zeros(len(fr))
F = np.fft.rfft2(fr * win)
for i in range(1, len(fr)):
    R = F[i] * np.conj(F[i - 1]); R /= np.abs(R) + 1e-6
    c = np.fft.irfft2(R, s=(H, W))
    y, x = np.unravel_index(np.argmax(c), c.shape)
    if x > W // 2: x -= W
    if y > H // 2: y -= H
    dx[i] = x if abs(y) <= 2 else 0
    conf[i] = c.max()
# 横パン: 1 コマ (0.1 秒) に 3 画素以上の横ずれで、相関がはっきりしている
pan = (np.abs(dx) >= 3) & (conf > 0.08)
L = 30 * FPS
cs = np.concatenate([[0], np.cumsum(pan)])
score = cs[L:] - cs[:-L]
order = np.argsort(-score)
picked = []
for i in order:
    if all(abs(i - j) > L for j in picked):
        picked.append(i)
    if len(picked) >= 6: break
for i in picked:
    t = i / FPS
    print(f'{int(t // 60)}:{t % 60:04.1f} ({t:.0f}s)  横パンのコマ {score[i]}/{L}  平均ずれ {np.abs(dx[i:i+L][pan[i:i+L]]).mean():.1f}')
