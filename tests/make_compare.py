"""紹介用の比べる動画と静止画を作る (make_demo.py の出力を 2x2 に並べる)。

    python -X utf8 tests/make_compare.py

左上 = 元の動画 (同じ大きさに縮小しただけ)、右上 = 公開した 1.3.3 (標準)、左下 = 1.6 標準 (20fps)、右下 = 1.6 高画質 (30fps)。
音は 1.6 高画質の (実機で鳴る) 音。出力は files/demo-1.6/compare/ に、場面ごとの mp4・全部つないだ mp4・静止画。
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from wsvideo import media  # noqa: E402

D = ROOT / 'files' / 'demo-src'
DEMO = ROOT / 'files' / 'demo-1.6'
OUT = DEMO / 'compare'
# 最後 = 絵の合わせ方 (make_demo.py に渡したのと同じにする。Sintel は横長なので fill = 画面いっぱい)
CLIPS = [('mcar', D / 'morevna' / 'morevna-4.0.1-ru-hq.mp4', 244, 'Morevna ep.4 (CC BY-SA 4.0)', 'fit'),
         ('mfight', D / 'morevna' / 'morevna-4.0.1-ru-hq.mp4', 322, 'Morevna ep.4 (CC BY-SA 4.0)', 'fit'),
         ('schase', D / 'sintel' / 'Sintel.2010.1080p.mkv', 240, 'Sintel (CC BY 3.0) Blender Foundation', 'fill'),
         ('ssnow', D / 'sintel' / 'Sintel.2010.1080p.mkv', 42, 'Sintel (CC BY 3.0) Blender Foundation', 'fill')]
# 比べる相手は公開した版 (1.3.3)。未公開の 1.4.0・1.5.0 とは比べない (JISO、2026-11-27)
# 日本のアニメ風の素材 (MiniMax H3 でローカルに作ったオリジナル、files/demo-src/minimax。JISO の希望、2026-11-27)
M = D / 'minimax'
CLIPS_MINIMAX = [('xwitch', M / 'a_witch.mp4', 0, 'Generated locally with MiniMax H3 (original)', 'fill'),
                 ('xroof', M / 'b_rooftop_alt.mp4', 0, 'Generated locally with MiniMax H3 (original)', 'fill'),
                 ('xsword', M / 'c_swordclash.mp4', 0, 'Generated locally with MiniMax H3 (original)', 'fill'),
                 ('xmecha', M / 'd_mecha.mp4', 0, 'Generated locally with MiniMax H3 (original)', 'fill')]
LABELS =['Original (scaled down)', 'v1.3.3  standard  12fps', 'v1.6  standard  20fps', 'v1.6  high quality  30fps']
W, H, BAR = 448, 288, 30
LEN = 20
FONT = 'C\\:/Windows/Fonts/arial.ttf'
FF = media.ffmpeg_exe()


def label(i, text):
    return (f'[{i}:v]setpts=PTS-STARTPTS,fps=60,scale={W}:{H}:flags=neighbor,setsar=1,'
            f'pad={W}:{H + BAR}:0:{BAR}:0x101018,'
            f"drawtext=fontfile='{FONT}':text='{text}':fontcolor=white:fontsize=20:x=10:y=5[v{i}]")


def one(name, src, st, credit, mode='fit'):
    vids = [DEMO / f'{name}_{t}.mp4' for t in ('v133_nile60', 'new_nile60', 'new_nile60hq')]
    if not all(v.exists() for v in vids):
        print('skip', name)
        return None
    out = OUT / f'{name}.mp4'
    fit = (f'scale={W}:{H}:force_original_aspect_ratio=increase:flags=lanczos,crop={W}:{H}' if mode == 'fill' else
           f'scale={W}:{H}:force_original_aspect_ratio=decrease:flags=lanczos,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:black')
    fc = [f'[0:v]{fit},setsar=1[o]',
          label('o', LABELS[0]).replace('[o:v]', '[o]').replace('[vo]', '[v0]')]
    for i in range(1, 4):
        fc.append(label(i, LABELS[i]))
    fc.append('[v0][v1][v2][v3]xstack=inputs=4:layout=0_0|w0_0|0_h0|w0_h0[g]')
    fc.append(f"[g]pad=iw:ih+26:0:0:0x101018,drawtext=fontfile='{FONT}':text='{credit}':"
              f'fontcolor=0xa0a0b0:fontsize=16:x=10:y=h-22[out]')
    cmd = [FF, '-y', '-v', 'error', '-ss', str(st), '-t', str(LEN), '-i', str(src)]
    for v in vids:
        cmd += ['-t', str(LEN), '-i', str(v)]
    cmd += ['-filter_complex', ';'.join(fc), '-map', '[out]', '-map', '3:a?', '-t', str(LEN),
            '-c:v', 'libx264', '-crf', '16', '-preset', 'slow', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '160k',
            str(out)]
    subprocess.run(cmd, check=True)
    for t in STILLS:
        subprocess.run([FF, '-y', '-v', 'error', '-ss', str(t), '-i', str(out), '-frames:v', '1',
                        str(OUT / f'{name}_{t:02d}s.png')], check=True)
    print('ok', out)
    return out


STILLS = (5, 12)


def main():
    """引数なし = Morevna・Sintel (各 20 秒)、minimax = MiniMax H3 で作った 4 本 (各 5 秒)"""
    global LEN, STILLS
    clips, all_name = CLIPS, 'compare_all.mp4'
    if len(sys.argv) > 1 and sys.argv[1] == 'minimax':
        clips, all_name, LEN, STILLS = CLIPS_MINIMAX, 'compare_minimax_all.mp4', 5, (2, 4)
    OUT.mkdir(parents=True, exist_ok=True)
    done = [o for o in (one(*c) for c in clips) if o]
    if len(done) > 1:
        lst = OUT / 'list.txt'
        lst.write_text(''.join(f"file '{p.as_posix()}'\n" for p in done), encoding='utf-8')
        subprocess.run([FF, '-y', '-v', 'error', '-f', 'concat', '-safe', '0', '-i', str(lst), '-c', 'copy',
                        str(OUT / all_name)], check=True)
        print('ok', all_name)


if __name__ == '__main__':
    main()
