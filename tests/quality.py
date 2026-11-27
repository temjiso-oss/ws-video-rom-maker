"""画質を数字で測る (変換の改良を比べるための道具)。

    python -X utf8 tests/quality.py 動画 開始秒 長さ秒 出力フォルダー [名前] [--k 3] [--sd 350] [--rom]

nileswan・60Hz（アプリの標準）で変換し、実機に映る画面（変換の中で作る、表示中のバッファ）と元の動画を比べる。
- PSNR（dB、大きいほど元に近い）：全コマの平均と、悪い方から 5% のコマの平均（動きの激しい所のくずれ）。
- 「減色だけ」の PSNR：そのコマの候補をすべて書けた場合。これと実際の差が「書ききれなかった」分。
- 何枚かのコマを、元・結果を並べた PNG にする（目で見る用）。
結果は 出力フォルダー/quality.jsonl に 1 行ずつ足す。動画や画像は手元で見るだけ（配らない）。
"""
import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from wsvideo import encoder  # noqa: E402


def psnr(mse):
    return 10 * np.log10(255.0 ** 2 / max(mse, 1e-9))


def _lab(img):
    """sRGB (H,W,3 0-255) → CIELAB"""
    c = img / 255.0
    c = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    m = np.array([[0.4124, 0.3576, 0.1805], [0.2126, 0.7152, 0.0722], [0.0193, 0.1192, 0.9505]])
    xyz = c @ m.T / np.array([0.9505, 1.0, 1.089])
    f = np.where(xyz > 0.008856, np.cbrt(xyz), 7.787 * xyz + 16 / 116)
    return np.stack([116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]), 200 * (f[..., 1] - f[..., 2])], -1)


def _blur(img):
    """3x3 のぼかし (ディザの細かい点は、目では混ざって見えるので)"""
    p = np.pad(img, ((1, 1), (1, 1), (0, 0)), mode='edge')
    k = [1, 2, 1]
    out = np.zeros_like(img, dtype=np.float32)
    for dy in range(3):
        for dx in range(3):
            out += k[dy] * k[dx] * p[dy:dy + img.shape[0], dx:dx + img.shape[1]]
    return out / 16


def delta_e(src, got):
    """見た目の色の差 (ぼかしてから CIELAB の距離の平均。小さいほど元に近い。1〜2 で差がわかる程度)"""
    return float(np.sqrt(((_lab(_blur(src)) - _lab(_blur(got))) ** 2).sum(-1)).mean())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('video')
    ap.add_argument('start', type=float)
    ap.add_argument('length', type=float)
    ap.add_argument('out')
    ap.add_argument('name', nargs='?', default='run')
    ap.add_argument('--k', type=int, default=3)
    ap.add_argument('--sd', type=int, default=350)
    ap.add_argument('--rom', action='store_true', help='ROM に入れる版 (標準は nileswan)')
    ap.add_argument('--dither', type=float, default=1.0)
    ap.add_argument('--shots', type=int, default=6)
    ap.add_argument('--units', type=float, default=None, help='展開の上限 MAX_UNITS[K] を変える')
    ap.add_argument('--rommb', type=int, default=16)
    ap.add_argument('--hz75', action='store_true')
    ap.add_argument('--vmode', default='delta', help='映像の方式: delta / raw / rawpal / raster (nileswan 版)')
    a = ap.parse_args()
    if a.units:
        encoder.MAX_UNITS[a.k] = a.units
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    s = encoder.Settings(k=a.k, target='rom' if a.rom else 'nile', hz60=not a.hz75, sd_kbps=a.sd, dither=a.dither,
                         saturation=1.2, max_rom_mb=a.rommb, video_mode=a.vmode)
    rec = dict(mse=[], qmse=[], de=[], flick=[], de2=[], de_orig=[])
    shots = {}
    # 前処理 (WSVIDEO_PREFILTER・POSTFILTER) があるときは、前処理なしの元の絵とも比べる (de_orig)
    refs = None
    fkeys = [k for k in ('WSVIDEO_PREFILTER', 'WSVIDEO_POSTFILTER') if os.environ.get(k)]
    if fkeys:
        from wsvideo import media
        saved = {k: os.environ.pop(k) for k in fkeys}
        try:
            refs = list(media.read_frames(a.video, encoder.fps_expr(s.k, s.lines), a.start, a.length,
                                          mode=s.mode, brightness=s.brightness, contrast=s.contrast,
                                          saturation=s.saturation, deinterlace=s.deinterlace))
        finally:
            os.environ.update(saved)
    orig = encoder.delta_pass

    def wrapped(analyses, *args, **kw):
        if not kw.get('write'):
            return orig(analyses, *args, **kw)
        inner = kw.get('on_frame')
        total = sum(an.n for an in analyses)
        pick = set(np.linspace(total * 0.1, total * 0.95, a.shots).astype(int).tolist())
        cnt = [0]

        def on_frame(an, f, disp):
            src = an.src[f].astype(np.float32)
            got = np.clip(np.round(disp), 0, 15) * 17.0
            rec['mse'].append(float(((got - src) ** 2).mean()))
            G = an.palsets[int(an.psid[f])]
            q = np.clip(np.round(G[an.pal[f][:, None], an.idx[f]]), 0, 15) * 17.0
            rec['qmse'].append(float(((q - src) ** 2).mean()))
            # ちらつき: 元の動画がほとんど変わっていない所 (各色 6 以下) で、表示が変わった量の平均
            if 'prev' in rec:
                ps_, pd_ = rec['prev']
                still = (np.abs(src - ps_).max(2) <= 6)
                if still.any():
                    rec['flick'].append(float(np.abs(got - pd_).max(2)[still].mean()))
            rec['prev'] = (src, got)
            sf = src.reshape(18, 28, 8, 8, 3).transpose(0, 2, 1, 3, 4).reshape(144, 224, 3)
            df = encoder.tiles_to_frame(disp).astype(np.float32)
            if cnt[0] % 3 == 0:
                rec['de'].append(delta_e(sf, df))
                if refs is not None and cnt[0] < len(refs):
                    rec['de_orig'].append(delta_e(refs[cnt[0]].astype(np.float32), df))
                # 2 コマを混ぜた見え方 (液晶の残像と目で、続く 2 コマは混ざって見える。時間方向のディザの効き目を見る)
                if 'pf' in rec:
                    psf, pdf = rec['pf']
                    rec['de2'].append(delta_e((sf + psf) / 2, (df + pdf) / 2))
            rec['pf'] = (sf, df)
            if cnt[0] in pick:
                shots[cnt[0]] = (encoder.tiles_to_frame(src / 17.0), encoder.tiles_to_frame(disp))
            cnt[0] += 1
            if inner:
                inner(an, f, disp)
        kw['on_frame'] = on_frame
        ret = orig(analyses, *args, **kw)
        rec['stats'] = ret[3]
        return ret

    encoder.delta_pass = wrapped
    t0 = time.time()
    with tempfile.TemporaryDirectory() as td:
        r = encoder.convert([encoder.Item(a.video, a.start, a.length)], s, os.path.join(td, 'q.wsc'))
        size = r.size
    el = time.time() - t0
    m = np.array(rec['mse'])
    q = np.array(rec['qmse'])
    per = np.array([psnr(x) for x in m])
    worst = np.sort(per)[:max(1, len(per) // 20)]
    res = dict(name=a.name, video=Path(a.video).name[:12], start=a.start, length=a.length, k=a.k,
               target=s.target, frames=len(m), flicker=round(float(np.mean(rec['flick'])), 3), de=round(float(np.mean(rec['de'])), 3),
               de2=round(float(np.mean(rec['de2'])), 3) if rec['de2'] else None,
               de_orig=round(float(np.mean(rec['de_orig'])), 3) if rec['de_orig'] else None,
               space=encoder.COLOR_SPACE, dkind=encoder.DITHER_KIND,
               prefilter=os.environ.get('WSVIDEO_PREFILTER'), postfilter=os.environ.get('WSVIDEO_POSTFILTER'),
               de_worst5=round(float(np.mean(np.sort(rec['de'])[-max(1, len(rec['de']) // 20):])), 3), psnr=round(float(psnr(m.mean())), 3),
               psnr_frame_avg=round(float(per.mean()), 3), psnr_worst5=round(float(worst.mean()), 3),
               psnr_quant=round(float(psnr(q.mean())), 3), avg_tiles=round(r.avg_tiles, 1),
               limited=int(getattr(r, 'limited', 0) or 0), bytes=int(size), sec=round(el, 1))
    st = rec.get('stats', {})
    nf = max(1, st.get('frames', 1))
    # 1 コマあたりの種類ごとの枚数 (c2・c4 = 色の少ないタイル = ブロックノイズのもと)
    res['per_frame'] = {k: round(st.get(k, 0) / nf, 1) for k in ('c2', 'c4', 'copies', 'solid', 'bulk')}
    res['raw_frames'] = st.get('raw', 0)
    print(json.dumps(res, ensure_ascii=False))
    with open(out / 'quality.jsonl', 'a', encoding='utf-8') as fp:
        fp.write(json.dumps(res, ensure_ascii=False) + '\n')
    from PIL import Image
    for i, (src, got) in sorted(shots.items()):
        img = np.concatenate([src, got], 1)
        Image.fromarray(img).resize((448 * 2, 144 * 2), Image.NEAREST).save(out / f'{a.name}_{i:05d}.png')


if __name__ == '__main__':
    main()
