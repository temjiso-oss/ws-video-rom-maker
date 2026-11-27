"""紹介用の比べる動画を作る。前の版 (release/src = 公開した 1.4.0) と今の版 (src) で同じ場面を変換し、
実機に映る絵 (変換の中で作る表示中の画面) をプレビューの mp4 にする。ROM と .WSV も残す。

    python -X utf8 tests/make_demo.py 版 プリセット 動画 開始秒 長さ秒 出力フォルダー 名前 [合わせ方]
      合わせ方: fit (標準、黒帯あり) / fill (画面いっぱい、はみ出しはカット) / stretch
      版: v133 (公開した 1.3.3、files/old-1.3.3) / old (release/src = 未公開の 1.4.0) / new (src)
      プリセット: nile60 (標準) / nile60hq (高画質、new だけ) / rom

出力: 出力フォルダー/名前.mp4 (2 倍に拡大、音つき)・名前.wsc・(nileswan 版は) .WSV
"""
import inspect
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# 並列の処理 (multiprocessing の spawn) はこのファイルを読み直すので、本体は __main__ の中に置く
# (置かないと子が引数の読み取りで落ち、作り直しが延々と続く。2026-11-27)
SRC = {'old': ROOT / 'release/src', 'new': ROOT / 'src',
       # 公開した 1.3.3 (ソースが無いので、配布 zip から tests/extract_release.py 1.3.3 で取り出した .pyc)
       'v133': ROOT / 'files/old-1.3.3'}
_ver = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] in SRC else os.environ.get('WSDEMO_VER', 'new')
os.environ['WSDEMO_VER'] = _ver
sys.path.insert(0, str(SRC[_ver]))
from wsvideo import encoder, __version__  # noqa: E402

# 各版の GUI のプリセットと同じ値 (old = 1.4.0 の gui.PRESETS、new = webgui.PRESETS)
PRESETS = {
    'old': {'nile60': dict(target='nile', hz60=True, k=5, sd_kbps=250),
            'rom': dict(target='rom', hz60=False, k=5)},
    'v133': {'nile60': dict(target='nile', hz60=True, k=5, sd_kbps=250),     # 1.3.3 の gui.PRESETS
             'rom': dict(target='rom', hz60=False, k=5)},
    'new': {'nile60': dict(target='nile', hz60=True, k=3, sd_kbps=350, video_mode='delta'),
            'nile60hq': dict(target='nile', hz60=True, k=2, sd_kbps=900, video_mode='raster'),
            'rom': dict(target='rom', hz60=False, k=5, video_mode='delta')},
}


def main():
    ver, preset, video, start, length, out, name = sys.argv[1:8]
    kw = dict(PRESETS[ver][preset], saturation=1.2, max_rom_mb=16, dither=1.0)
    if len(sys.argv) > 8:      # 8 番目 = 絵の合わせ方 (fit / fill / stretch)。横長の映画は fill
        kw['mode'] = sys.argv[8]
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    kw['preview_path'] = str(out / f'{name}.mp4')
    ok = inspect.signature(encoder.Settings.__init__).parameters
    s = encoder.Settings(**{k: v for k, v in kw.items() if k in ok})
    print(f'{name}: {ver} {__version__} {preset} {s.fps:.2f}fps', flush=True)
    r = encoder.convert([encoder.Item(video, float(start), float(length))], s, str(out / f'{name}.wsc'))
    print(f'{name}: done size={r.size} avg_tiles={getattr(r, "avg_tiles", 0):.0f}', flush=True)


if __name__ == '__main__':
    main()
