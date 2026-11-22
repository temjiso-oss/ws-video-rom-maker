"""コマンドライン版:  python -m wsvideo.cli 入力.mp4 [入力2 ...] -o 出力.wsc"""
import argparse
import sys
import time

from . import encoder


def main(argv=None):
    ap = argparse.ArgumentParser(description='動画をワンダースワンカラー用ROM(.wsc)に変換します')
    ap.add_argument('inputs', nargs='+')
    ap.add_argument('-o', '--out', required=True)
    ap.add_argument('--k', type=int, default=5, help='映像1コマあたりのVBlank数 (5 = 15.1fps)')
    ap.add_argument('--audio-div', type=int, default=1, choices=[0, 1, 2, 3],
                    help='1=12kHz 2=6kHz 3=4kHz 0=音声なし')
    ap.add_argument('--rom-mb', type=int, default=16, choices=encoder.ROM_SIZES)
    ap.add_argument('--quality', default='auto', choices=['auto'] + list(encoder.QUALITY_TAU))
    ap.add_argument('--mode', default='fit', choices=['fit', 'fill', 'stretch'])
    ap.add_argument('--dither', type=float, default=1.0)
    ap.add_argument('--start', type=float, default=0.0)
    ap.add_argument('--length', type=float, default=None)
    ap.add_argument('--preview', default=None, help='シミュレーション映像(mp4)の出力先')
    ap.add_argument('--speaker', type=int, default=1, choices=[0, 1, 2, 3],
                    help='内蔵スピーカーの音量 0=100%%(割れる) 1=50%% 2=25%% 3=12.5%%')
    ap.add_argument('--target', default='rom', choices=['rom', 'nile'],
                    help='rom=ROMの中に入れる / nile=nileswan の microSD から読む (.WSV を別に作る)')
    ap.add_argument('--sd-kbps', type=int, default=160, help='nileswan: microSD から読む速さの上限 (KB/秒)')
    ap.add_argument('--workers', type=int, default=None, help='解析に使うプロセス数')
    ap.add_argument('--hz60', action='store_true', help='60Hz モード (実験的)')
    ap.add_argument('--pal8', action='store_true', help='8パレット (121色、古い形式・変換が速い)')
    a = ap.parse_args(argv)
    s = encoder.Settings(k=a.k, audio_div=a.audio_div, max_rom_mb=a.rom_mb, quality=a.quality, mode=a.mode,
                         dither=a.dither, preview_path=a.preview, speaker_shift=a.speaker, target=a.target,
                         sd_kbps=a.sd_kbps, workers=a.workers, hz60=a.hz60, pal16=not a.pal8)
    items = [encoder.Item(p, a.start, a.length) for p in a.inputs]
    t0 = time.time()
    last = [0]

    def prog(frac, msg):
        if time.time() - last[0] > 2 or frac >= 1:
            last[0] = time.time()
            print(f'{frac * 100:5.1f}%  {msg}', flush=True)

    r = encoder.convert(items, s, a.out, prog, print)
    print(f'ROM {r.rom_mb}MB  {r.seconds:.1f}秒  {r.frames}コマ  平均 {r.avg_tiles:.0f} タイル/コマ  '
          f'tau={r.tau:.1f}  映像 {r.video_bytes / 1048576:.2f}MB 音声 {r.audio_bytes / 1048576:.2f}MB  '
          f'({time.time() - t0:.0f}秒)')
    if r.wsv_path:
        print(f'データファイル: {r.wsv_path} ({r.wsv_size / 1048576:.1f}MB)  速度制限が効いたコマ: {r.limited}')


if __name__ == '__main__':
    sys.exit(main())
