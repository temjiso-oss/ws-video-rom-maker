"""動画 → WSC プレイヤー用ストリームへの変換。

1. 解析パス: フレームごとに 8パレット x 15色(+黒) のパレットセットを決め、
   各タイル(8x8)を最適なパレットで減色した「候補」を一時ファイルに保存する。
   パレットセットは場面が大きく変わったときだけ作り直す(ちらつき防止・容量節約)。
2. 差分パス: ダブルバッファ(A/B)それぞれの画面内容をシミュレートし、
   表示中の内容より候補の方が tau 以上良くなるタイルだけを書き換える。
   ROM 容量に収まる最小の tau を二分探索で決める。
3. 書き出し: フレームデータをバンク境界をまたがないよう並べる。
"""
import math
import os
import struct
import tempfile
import time

import numpy as np

from . import media

W, H = 224, 144
TW, TH = 28, 18
NT = TW * TH
LINES = 159
CPU_HZ = 3072000
REFRESH = CPU_HZ / (256 * LINES)          # 75.47 Hz
BANK = 0x10000

# 色の差の重み (R, G, B)。1.5.0 で 0.30,0.59,0.11 (明るさの式) から変えた: 青を軽く見すぎて、紫や青の小さな物が
# 灰色になっていた。tests/quality.py の比較 (見た目の色の差 ΔE) で 5〜7% 良くなる。環境変数で変えられる (試す用)
WGT = np.array([float(x) for x in os.environ.get('WSVIDEO_WGT', '0.35,0.45,0.30').split(',')], np.float32)
WGT = WGT / WGT.sum() * 3
SW = np.sqrt(WGT).astype(np.float32)

_B8 = np.array([[0, 32, 8, 40, 2, 34, 10, 42], [48, 16, 56, 24, 50, 18, 58, 26],
                [12, 44, 4, 36, 14, 46, 6, 38], [60, 28, 52, 20, 62, 30, 54, 22],
                [3, 35, 11, 43, 1, 33, 9, 41], [51, 19, 59, 27, 49, 17, 57, 25],
                [15, 47, 7, 39, 13, 45, 5, 37], [63, 31, 55, 23, 61, 29, 53, 21]], np.float32)
BAYER = ((_B8 + 0.5) / 64 - 0.5).reshape(64)

# 色の近さを測る空間 (2026-11-27、試し。WSVIDEO_SPACE):
#   'rgb'   今まで: RGB (0〜15) に重み SW を掛けた空間
#   'oklab' 人の目に近い色空間 OKLab (pngquant などと同じ考え)。L の幅を灰色の端から端と同じ (15√3) に合わせる
# feat(色) = 距離を測る空間の座標、unfeat = 戻す (k-means の中心を RGB に戻して 4 ビットに丸める)。
# DDIR = ディザでずらす向き (明るさの方向)。
# 2026-11-27 に 'oklab'・色み 2 を標準にした (ΔE が 30fps で 3〜9%、20fps 差分で 10〜17%、ROM 版で 11〜22% 減。実機でも色移りが減った)
COLOR_SPACE = os.environ.get('WSVIDEO_SPACE', 'oklab')
_OK_S = np.float32(15 * np.sqrt(3))
_OK_C = float(os.environ.get('WSVIDEO_OKC', '2'))     # 色み (a・b) の重み (1 だと色みを軽く見すぎて悪い)
_OK_SV = np.array([_OK_S, _OK_S * _OK_C, _OK_S * _OK_C], np.float32)
_OK_M1 = np.array([[0.4122214708, 0.5363325363, 0.0514459929],
                   [0.2119034982, 0.6806995451, 0.1073969566],
                   [0.0883024619, 0.2817188376, 0.6299787005]], np.float32)
_OK_M2 = np.array([[0.2104542553, 0.7936177850, -0.0040720468],
                   [1.9779984951, -2.4285922050, 0.4505937099],
                   [0.0259040371, 0.7827717662, -0.8086757660]], np.float32)
_OK_M1I = np.linalg.inv(_OK_M1.astype(np.float64)).astype(np.float32)
_OK_M2I = np.linalg.inv(_OK_M2.astype(np.float64)).astype(np.float32)
_OK_LUT = None


def feat(x, xp=np):
    """色 (…,3)、RGB 0〜15 → 距離を測る空間の座標 (float32)"""
    if COLOR_SPACE != 'oklab':
        return x * xp.asarray(SW, xp.float32)
    c = xp.clip(x.astype(xp.float32) * xp.float32(1 / 15), 0, 1)
    lin = xp.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    lms = xp.cbrt(lin @ xp.asarray(_OK_M1.T))
    return (lms @ xp.asarray(_OK_M2.T)) * xp.asarray(_OK_SV)


def unfeat(f):
    """feat の逆 (numpy)。RGB 0〜15 (丸める前)"""
    if COLOR_SPACE != 'oklab':
        return f / SW
    lms = (np.asarray(f, np.float32) / _OK_SV) @ _OK_M2I.T
    lin = np.clip((lms ** 3) @ _OK_M1I.T, 0, 1)
    c = np.where(lin <= 0.0031308, lin * 12.92, 1.055 * lin ** (1 / 2.4) - 0.055)
    return c * 15


DDIR = SW.copy() if COLOR_SPACE != 'oklab' else np.array([np.sqrt(3), 0, 0], np.float32)

# 網目 (ディザ) の形 (2026-11-27、試し。WSVIDEO_DITHER): 'bayer' (今まで、8x8 の格子) / 'blue' (ブルーノイズ 64x64)
# BD_TILES (NT, 64) = 画面のタイルごとの網目の値 (-0.5〜0.5)
DITHER_KIND = os.environ.get('WSVIDEO_DITHER', 'bayer')


def _blue_noise(n=64, sigma=1.5, seed=1):
    """void-and-cluster 法でブルーノイズ (n x n、0〜n*n-1 の順位) を作る。トーラス状 (端がつながる)"""
    rng = np.random.default_rng(seed)
    fx = np.fft.fftfreq(n)
    g = np.exp(-2 * (np.pi * sigma) ** 2 * (fx[:, None] ** 2 + fx[None, :] ** 2))   # ガウスの周波数応答

    def blur(m):
        return np.real(np.fft.ifft2(np.fft.fft2(m) * g))
    N = n * n
    pat = np.zeros((n, n), bool)
    pat.flat[rng.choice(N, N // 10, replace=False)] = True
    while True:                     # 一番詰まった点を、一番空いた所へ動かす (落ち着くまで)
        e = blur(pat)
        c = np.argmax(np.where(pat, e, -np.inf))
        pat.flat[c] = False
        e = blur(pat)
        v = np.argmin(np.where(pat, np.inf, e))
        pat.flat[v] = True
        if v == c:
            break
    rank = np.zeros(N, np.int64)
    ones = int(pat.sum())
    p = pat.copy()
    for r in range(ones - 1, -1, -1):
        e = blur(p)
        c = np.argmax(np.where(p, e, -np.inf))
        p.flat[c] = False
        rank[c] = r
    p = pat.copy()
    for r in range(ones, N):
        e = blur(p)
        v = np.argmin(np.where(p, np.inf, e))
        p.flat[v] = True
        rank[v] = r
    return rank.reshape(n, n)


def _dither_tiles():
    if DITHER_KIND != 'blue':
        return np.tile(BAYER, (NT, 1)).astype(np.float32)
    cache = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'bluenoise64.npy')
    try:
        rank = np.load(cache)
    except Exception:
        rank = _blue_noise()
        try:
            np.save(cache, rank)
        except Exception:
            pass
    v = ((rank + 0.5) / rank.size - 0.5).astype(np.float32)
    scr = np.tile(v, (H // 64 + 1, W // 64 + 1))[:H, :W]
    return scr.reshape(TH, 8, TW, 8).transpose(0, 2, 1, 3).reshape(NT, 64).copy()


BD_TILES = _dither_tiles()

# 1フレームで書き換えられるタイル数の上限 (K = 映像1フレームあたりのVBlank数)。
# エミュレーターでの計測値をもとに余裕を持たせた値 (docs/SPEC.md 参照)。
# 計測(12kHz音声あり): K=2 は 200 タイルまで間に合い 260 で遅れる、K=3 以上は全504タイルでも間に合う。
# 実機との差を見込んで2割ほど余裕を取る。K=1 (75fps) は 100 タイルでも間に合わないので使わない。
MAX_TILES = {2: 160, 3: 330}
OSD_PAL = 7               # 色14=黒・色15=白に固定するパレット (プレイヤーの時間表示が使う)
KEYFRAME_SEC = 10         # ジャンプ先 (両バッファ全体を書き直すコマ) の間隔 (秒)
QUALITY_TAU ={'best': 6.0, 'high': 20.0, 'normal': 60.0, 'low': 160.0}
ROM_SIZES = [1, 2, 4, 8, 16]                # MB
ROM_SIZE_CODE = {1: 0x03, 2: 0x04, 4: 0x06, 8: 0x08, 16: 0x09}


class Cancelled(Exception):
    pass


def fps_expr(k, lines=None):
    return f'{CPU_HZ}/{256 * (lines or LINES) * k}'


def frame_to_tiles(fr):
    t = fr.reshape(TH, 8, TW, 8, 3).transpose(0, 2, 1, 3, 4).reshape(NT, 64, 3)
    return t.astype(np.float32) * (15.0 / 255.0)


def tiles_to_frame(t):
    img = np.clip(np.round(t), 0, 15).astype(np.uint8) * 17
    return img.reshape(TH, TW, 8, 8, 3).transpose(0, 2, 1, 3, 4).reshape(H, W, 3)


# ---------------------------------------------------------------------------
# 減色
# ---------------------------------------------------------------------------
def kmeans(x, k, iters=10, rng=None):
    n = len(x)
    if n == 0:
        return np.zeros((k, x.shape[1]), np.float32)
    rng = rng or np.random.default_rng(1)
    if n > 6000:
        x = x[rng.choice(n, 6000, replace=False)]
        n = 6000
    c = [x[rng.integers(n)]]
    d2 = ((x - c[0]) ** 2).sum(1)
    for _ in range(1, k):
        s = d2.sum()
        if s <= 1e-9:
            c.append(c[-1])
            continue
        i = rng.choice(n, p=d2 / s)
        c.append(x[i])
        d2 = np.minimum(d2, ((x - x[i]) ** 2).sum(1))
    c = np.array(c, np.float32)
    for _ in range(iters):
        d = (x * x).sum(1)[:, None] - 2 * x @ c.T + (c * c).sum(1)[None]
        a = d.argmin(1)
        cnt = np.bincount(a, minlength=k)
        new = c.copy()
        ok = cnt > 0
        for dim in range(x.shape[1]):
            s = np.bincount(a, weights=x[:, dim], minlength=k)
            new[ok, dim] = s[ok] / cnt[ok]
        if np.allclose(new, c, atol=1e-3):
            c = new
            break
        c = new
    return c


# ---------------------------------------------------------------------------
# GPU (NVIDIA、CuPy) で計算する (1.5.0〜)。CuPy が入っていて GPU が使えるときだけ。
# 環境変数 WSVIDEO_NOGPU=1 で使わない。結果は CPU と同じ式 (浮動小数の丸めの差で、ごくまれに選ぶ色が変わる)。
# ---------------------------------------------------------------------------
_GPU = None


def gpu():
    """CuPy のモジュール (使えなければ None)"""
    global _GPU
    if _GPU is None:
        _GPU = False
        if os.environ.get('WSVIDEO_NOGPU') != '1':
            try:
                import warnings
                with warnings.catch_warnings():
                    warnings.simplefilter('ignore')
                    import cupy as cp
                    if cp.cuda.runtime.getDeviceCount() > 0:
                        cp.zeros(1).sum()           # 実際に動くか
                        _GPU = cp
            except Exception:
                _GPU = False
                if os.environ.get('WSVIDEO_GPUDEBUG') == '1':
                    import traceback
                    traceback.print_exc()
    return _GPU or None


def gpu_name():
    cp = gpu()
    if not cp:
        return None
    try:
        return cp.cuda.runtime.getDeviceProperties(0)['name'].decode()
    except Exception:
        return 'GPU'


def _bd(nt, dither, pos):
    """タイルの網目の値 (nt*64,)。pos = 画面のどのタイルか (None なら全部の順)"""
    if pos is not None:
        return (BD_TILES[pos] * np.float32(dither)).reshape(-1)
    if nt == NT:
        return (BD_TILES * np.float32(dither)).reshape(-1)
    return np.tile(BAYER * np.float32(dither), nt)


def _quant_all_gpu(cp, tiles, P, dither, pos=None):
    npal = P.shape[0]
    nt = len(tiles)
    ts = feat(cp.asarray(tiles, cp.float32), cp).reshape(-1, 3)
    ps = feat(cp.asarray(P, cp.float32), cp).reshape(-1, 3)
    swp = ps @ cp.asarray(DDIR)
    if dither:
        bd = cp.asarray(_bd(nt, dither, pos))
    else:
        bd = cp.zeros(nt * 64, cp.float32)
    xa = cp.concatenate([ts, bd[:, None]], 1)
    pa = cp.concatenate([ps, swp[:, None]], 1)
    key = xa @ pa.T
    key *= -2
    key += (ps * ps).sum(1)
    key = key.reshape(nt, 64, npal, 16)
    idx = key.argmin(-1)
    picked = cp.take_along_axis(key, idx[..., None], 3)[..., 0]
    picked += 2 * bd.reshape(nt, 64, 1) * swp.reshape(npal, 16)[cp.arange(npal), idx]
    e = picked.sum(1) + (ts * ts).reshape(nt, 64, 3).sum((1, 2))[:, None]
    return cp.asnumpy(idx.astype(cp.uint8)), cp.asnumpy(cp.maximum(e, 0))


def quant_all(tiles, P, dither=0.0, pos=None):
    """tiles (NT,64,3) をパレットセット P (np,16,3) の各パレットで減色。pos = タイルが画面のどこか (一部だけのとき)
    戻り値: idx (NT,64,np) uint8, err (NT,np)"""
    cp = gpu()
    if cp:
        return _quant_all_gpu(cp, tiles, P, dither, pos)
    # 距離 |a - p|^2 = |a|^2 - 2 a・p + |p|^2 を行列積で求める。
    # ディザは明るさ方向 (SW に比例) のずれなので、x・p = t・p + b・(SW・p) と外積で足せる。
    # ディザ込みの点 x = t + b・SW との内積は [t, b]・[p, SW・p] なので、4列の行列積1回で出せる。
    npal = P.shape[0]
    nt = len(tiles)
    ts = feat(tiles).reshape(-1, 3).astype(np.float32)
    ps = feat(P).reshape(-1, 3).astype(np.float32)
    swp = (ps @ DDIR).astype(np.float32)             # SW・p
    bd = _bd(nt, dither, pos) if dither else np.zeros(nt * 64, np.float32)
    xa = np.empty((nt * 64, 4), np.float32)
    xa[:, :3] = ts
    xa[:, 3] = bd
    pa = np.empty((npal * 16, 4), np.float32)
    pa[:, :3] = ps
    pa[:, 3] = swp
    key = xa @ pa.T                                  # x・p  (NT*64, np*16)
    key *= -2
    key += (ps * ps).sum(1)                          # |p|^2 - 2 x・p  (|x|^2 は比較に要らない)
    key = key.reshape(nt, 64, npal, 16)
    idx = key.argmin(-1).astype(np.uint8)
    # 元の点 t との誤差 = (|p|^2 - 2 x・p) + 2 b (SW・p) + |t|^2
    picked = np.take_along_axis(key, idx[..., None].astype(np.intp), 3)[..., 0]
    picked += 2 * bd.reshape(nt, 64, 1) * swp.reshape(npal, 16)[np.arange(npal), idx]
    e = picked.sum(1) + (ts * ts).reshape(nt, 64, 3).sum((1, 2))[:, None]
    return idx, np.maximum(e, 0)


def _kmeans_batch_gpu(cp, xs, k, iters=10, seed=7, maxn=16384, init=None):
    """kmeans を、グループ (パレット) ごとに GPU でまとめて行う。xs = 点の配列 (n_i, 3) のリスト。
    戻り値: (len(xs), k, 3) numpy。初めの中心は k-means++ (乱数は GPU 側なので CPU 版と同じ値にはならない)。
    init (len(xs), k, 3) があれば、それを初めの中心にする (k-means++ を省く。2026-11-26、コマごとのパレット用)"""
    rng = np.random.default_rng(seed)
    if isinstance(xs, tuple):
        X, M = xs                       # すでに詰めてある (_pack_groups)
        g, n = M.shape
    else:
        g = len(xs)
        n = min(max(max((len(x) for x in xs), default=1), 1), maxn)
        X = np.zeros((g, n, 3), np.float32)
        M = np.zeros((g, n), np.float32)
        for i, x in enumerate(xs):
            if len(x) > n:
                x = x[rng.choice(len(x), n, replace=False)]
            X[i, :len(x)] = x
            M[i, :len(x)] = 1
    X = cp.asarray(X)
    M = cp.asarray(M)
    cnt_pts = M.sum(1)
    crng = cp.random.default_rng(seed)
    ar = cp.arange(g)
    if init is not None:
        C = cp.asarray(init, cp.float32)
        kpp = range(0)
    else:
        # k-means++
        u = crng.random(g)
        first = cp.minimum((u * cnt_pts).astype(cp.int64), cp.maximum(cnt_pts.astype(cp.int64) - 1, 0))
        C = cp.zeros((g, k, 3), cp.float32)
        C[:, 0] = X[ar, first]
        d2 = ((X - C[:, :1]) ** 2).sum(2) * M
        kpp = range(1, k)
    for j in kpp:
        s = d2.sum(1)
        cum = cp.cumsum(d2, 1)
        r = crng.random(g) * s
        pick = cp.minimum((cum < r[:, None]).sum(1), n - 1)
        newc = cp.where((s > 1e-9)[:, None], X[ar, pick], C[:, j - 1])
        C[:, j] = newc
        d2 = cp.minimum(d2, ((X - newc[:, None]) ** 2).sum(2) * M)
    # Lloyd
    for _ in range(iters):
        d = (X * X).sum(2)[:, :, None] - 2 * cp.matmul(X, C.transpose(0, 2, 1)) + (C * C).sum(2)[:, None, :]
        a = d.argmin(2)
        oh = (a[:, :, None] == cp.arange(k)[None, None, :]).astype(cp.float32) * M[:, :, None]   # (g, n, k)
        cnt = oh.sum(1)                                                                           # (g, k)
        sums = cp.matmul(oh.transpose(0, 2, 1), X)                                                # (g, k, 3)
        C = cp.where((cnt > 0)[:, :, None], sums / cp.maximum(cnt, 1)[:, :, None], C)
    return cp.asnumpy(C)


# パレットの作り方 (1.5.0 で見直し。tests/quality.py で比べた。合わせて ΔE が 8〜12% 小さく、ちらつきは同じくらい)
PAL_ROUNDS = int(os.environ.get('WSVIDEO_ROUNDS', '6'))       # 「タイルの割り当て → 色を決め直す」の回数 (前は 3)
KM_ITERS = int(os.environ.get('WSVIDEO_KMITERS', '10'))
PAL_GROW = float(os.environ.get('WSVIDEO_PALGROW', '1.12'))   # 誤差がこれだけ増えたらパレットを作り直してみる (前は 1.25)
PAL_GAIN = float(os.environ.get('WSVIDEO_PALGAIN', '0.88'))   # 作り直して誤差がこれ以下になったら切り替える (前は 0.8)
# ROM 版は容量が少ないので、パレットの切り替え (全体の書き直し 2 コマ分) を減らす。
# 2 分の激しい場面を 16MB に入れる試しで、1.12/0.88 は ΔE 7.08 (ブロックノイズだらけ)、1.25/0.8 は 4.13、1.6/0.65 は 3.83。
PAL_GROW_ROM = float(os.environ.get('WSVIDEO_PALGROW_ROM', '1.6'))
PAL_GAIN_ROM = float(os.environ.get('WSVIDEO_PALGAIN_ROM', '0.65'))


def pal_change(s):
    """パレットを作り直す条件 (増え方, 良くなり方)。nileswan 版と ROM 版で変える"""
    return (PAL_GROW, PAL_GAIN) if s.target == 'nile' else (PAL_GROW_ROM, PAL_GAIN_ROM)


def make_palset(tiles, npal=8, rounds=None, init=None):
    """1フレーム分のタイルから 8パレット x (黒 + 15色) を作る。
    init (前のコマのパレット、同じ本数) があれば、それでタイルを割り当ててから WARM_ROUNDS 回だけ作り直す (GPU のとき)"""
    rounds = rounds or PAL_ROUNDS
    cp = gpu()
    if cp:
        return _make_palset_gpu(cp, tiles, npal, rounds, init)
    rng = np.random.default_rng(7)
    ts = feat(tiles)
    means = ts.mean(1)
    c = kmeans(means, npal, rng=rng)
    a = ((means[:, None, :] - c[None]) ** 2).sum(-1).argmin(1)
    P = np.zeros((npal, 16, 3), np.float32)
    err = None
    for _ in range(rounds):
        worst = np.argsort(-err.min(1)) if err is not None else np.arange(NT)
        wi = 0
        for j in range(npal):
            sel = a == j
            if not sel.any():
                sel = np.zeros(NT, bool)
                sel[worst[wi:wi + 24]] = True
                wi += 24
            px = ts[sel].reshape(-1, 3)
            cols = unfeat(kmeans(px, 15, rng=rng))
            P[j, 1:] = np.clip(np.round(cols), 0, 15)
            P[j, 0] = 0
            if j == OSD_PAL:
                P[j, 14] = 0            # 時間表示の背景 (黒)
                P[j, 15] = 15           # 時間表示の文字 (白)
        _, err = quant_all(tiles, P)
        a = err.argmin(1)
    return P


# ---------------------------------------------------------------------------
# ラスター (2026-11-26、試し): 画面を 16 ラインずつ 9 本の帯に分け、帯ごとに自分の 8 パレットを持つ。
# 帯 k のパレットは、パレット RAM の (k&1)*8 から 8 本に置く (2 組を交互に使う)。プレイヤーは帯 k を描いている間に、
# 使っていない方の組へ帯 k+1 のパレットを写す (nile.inc ではなく player.asm の isr_line)。
# 変換の中では 72 本 (帯 x 8) のパレットとして扱い、タイルは自分の帯の 8 本からだけ選ぶ。マップのパレット番号は (番号 & 15)。
# ---------------------------------------------------------------------------
RASTER = os.environ.get('WSVIDEO_RASTER') == '1'
TDITHER = float(os.environ.get('WSVIDEO_TDITHER', '0'))   # 時間方向のディザの強さ (0 = 使わない。analyse)
# 帯の形 (2026-11-26 夜、WSVIDEO_BAND で選ぶ。試し):
#   '16'    16 ライン x 9 帯、帯ごとに 8 パレット (72 本)。帯 k はパレット RAM の (k&1)*8 から。
#   '8'     8 ライン x 18 帯、帯ごとに 4 パレット (72 本)。
#   'slide' 8 ラインごとに 4 パレットの組を 1 つ入れ替える (72 本)。タイルの行 r は組 r-1 と r の 8 本を使う (行 0 は組 0・1)。
#           組 g はパレット RAM の (g&3)*4 から (4 組を順に使う)。
#   '8x8'   8 ライン x 18 帯、帯ごとに 8 パレット (144 本。比べる用、プレイヤーは未対応)。
# ROWPAL[r] = タイルの行 r が使えるパレットの番号 (変換の中の通し番号)。マップのパレット番号は (番号 & 15)。
BAND_KIND = os.environ.get('WSVIDEO_BAND', '16')
if BAND_KIND == 'slide':
    PPB = 4
    NPAL_BANDED = TH * PPB
    ROWPAL = np.array([[g * PPB + j for g in ((0, 1) if r == 0 else (r - 1, r)) for j in range(PPB)]
                       for r in range(TH)])
else:
    _rows, PPB = {'16': (2, 8), '8': (1, 4), '8x8': (1, 8)}[BAND_KIND]
    NPAL_BANDED = TH // _rows * PPB
    ROWPAL = np.array([[(r // _rows) * PPB + j for j in range(PPB)] for r in range(TH)])
RPK = ROWPAL.shape[1]                                   # 1 行が使えるパレット数
TILE_ROW = np.arange(NT) // TW
ALLOWED = np.zeros((NT, NPAL_BANDED), bool)            # (NT, 72) タイルが使えるパレット
ALLOWED[np.arange(NT)[:, None], ROWPAL[TILE_ROW]] = True
OSD_MASK = (np.arange(NPAL_BANDED) & 15) == OSD_PAL    # パレット RAM の 7 番に入るパレット


def _quant_banded(tiles, G, dither=0.0, want_idx=True):
    """帯のパレット (72 本) での減色。各帯のタイルを、その帯の 8 パレットでだけ計算する (全部で計算するより 9 倍軽い)。
    戻り値は quant_all と同じ形: idx (NT,64,72) (want_idx=False なら None)、err (NT,72) (他の帯は無限大)"""
    cp = gpu()
    xp = cp or np
    t = xp.asarray(tiles, xp.float32).reshape(TH, TW * 64, 3)     # タイルの行ごと
    ts = feat(t, xp)
    ps = feat(xp.asarray(G, xp.float32)[xp.asarray(ROWPAL)], xp).reshape(TH, RPK * 16, 3)   # 行が使えるパレット
    swp = ps @ xp.asarray(DDIR)                                     # (18, 128)
    bd = xp.asarray(BD_TILES * np.float32(dither)).reshape(TH, TW * 64) if dither else \
        xp.zeros((TH, TW * 64), xp.float32)
    key = xp.matmul(ts, ps.transpose(0, 2, 1))                      # (18, 1792, 128)
    key += bd[:, :, None] * swp[:, None, :]
    key *= -2
    key += (ps * ps).sum(2)[:, None, :]
    key = key.reshape(TH, TW, 64, RPK, 16)
    idx = key.argmin(-1)                                            # (18, 28, 64, 8)
    picked = xp.take_along_axis(key, idx[..., None], 4)[..., 0]
    swp5 = swp.reshape(TH, 1, 1, RPK, 16)
    picked += 2 * bd.reshape(TH, TW, 64, 1) * xp.take_along_axis(
        xp.broadcast_to(swp5, (TH, TW, 64, RPK, 16)), idx[..., None], 4)[..., 0]
    e = picked.sum(2) + (ts * ts).reshape(TH, TW, 64, 3).sum((2, 3))[:, :, None]   # (18, 28, 8)
    e = xp.maximum(e, 0)
    if cp:
        e = cp.asnumpy(e)
        idx = cp.asnumpy(idx.astype(cp.uint8)) if want_idx else None
    elif want_idx:
        idx = idx.astype(np.uint8)
    full = np.full((NT, NPAL_BANDED), np.inf, np.float32)
    e = e.reshape(NT, RPK)
    rows = np.arange(NT)
    cols = ROWPAL[TILE_ROW]                                         # (NT, 8)
    for j in range(RPK):
        full[rows, cols[:, j]] = e[:, j]
    if not want_idx:
        return None, full
    fidx = np.zeros((NT, 64, NPAL_BANDED), np.uint8)
    idx = idx.reshape(NT, 64, RPK)
    for j in range(RPK):
        fidx[rows, :, cols[:, j]] = idx[:, :, j]
    return fidx, full


def _pack_groups(ts, tile_of, group_of, g):
    """タイル (ts: (NT, 64, 3)) を組に分けて、k-means 用の (X (g, n, 3), M (g, n)) に詰める (Python のくり返しなし)。
    tile_of・group_of: 「このタイルはこの組」の組の並び (同じタイルが 2 つの組に入ってもよい)"""
    order = np.argsort(group_of, kind='stable')
    tile_of, group_of = tile_of[order], group_of[order]
    cnt = np.bincount(group_of, minlength=g)
    start = np.concatenate([[0], np.cumsum(cnt)[:-1]])
    slot = np.arange(len(group_of)) - start[group_of]          # 組の中で何枚目か
    n = max(1, int(cnt.max()) * 64)
    X = np.zeros((g, n, 3), np.float32)
    M = np.zeros((g, n), np.float32)
    rows = np.repeat(group_of, 64)
    cols = (np.repeat(slot * 64, 64) + np.tile(np.arange(64), len(slot)))
    X[rows, cols] = ts[tile_of].reshape(-1, 3)
    M[rows, cols] = 1
    return X, M


def quant_g(tiles, G, dither=0.0, want_idx=True):
    """quant_all と同じ。帯のパレット (72 本) なら、自分の帯の 8 本だけで計算し、他の帯のパレットの誤差は無限大"""
    if G.shape[0] == NPAL_BANDED:
        return _quant_banded(tiles, G, dither, want_idx)
    return quant_all(tiles, G, dither)


def make_palset_banded(tiles, rounds=None, init=None):
    """帯ごとに 8 パレットを作る (72 本)。make_palset の GPU 版と同じやり方を、帯の中だけで行う。
    init (前のコマのパレット) があれば、それでタイルを割り当ててから始める (回数は WARM_ROUNDS)"""
    rounds = rounds or PAL_ROUNDS
    cp = gpu()
    rng = np.random.default_rng(7)
    ts = feat(tiles)
    means = ts.mean(1)
    a = np.zeros(NT, np.int64)
    err = None
    warm = init is not None and init.shape[0] == NPAL_BANDED
    if warm:
        _, err = quant_g(tiles, init, want_idx=False)
        a = err.argmin(1)
        rounds = WARM_ROUNDS
    else:
        # 同じパレットを使う行をまとめて、その行のタイルの平均色を k-means で分け、行のパレットへ割り当てる
        for sel in np.unique(ROWPAL, axis=0):
            rws = np.nonzero((ROWPAL == sel).all(1))[0]
            m = np.isin(TILE_ROW, rws)
            c = kmeans(means[m], RPK, rng=rng)
            a[m] = sel[((means[m][:, None, :] - c[None]) ** 2).sum(-1).argmin(1)]
    P = np.zeros((NPAL_BANDED, 16, 3), np.float32)
    for r in range(rounds):
        worst = np.argsort(-err.min(1)) if err is not None else np.arange(NT)
        tile_of, group_of = np.arange(NT), a.astype(np.int64)
        empty = np.nonzero(np.bincount(group_of, minlength=NPAL_BANDED) == 0)[0]
        if len(empty):
            # 空のパレット: その帯で一番合わないタイルを 4 枚 (そのタイルは元の組にも残す)
            used = np.zeros(NT, bool)
            et, eg = [], []
            for j in empty:
                cand = [t for t in worst if ALLOWED[t, j] and not used[t]][:4]
                used[cand] = True
                et += cand
                eg += [j] * len(cand)
            tile_of = np.concatenate([tile_of, np.array(et, np.int64)])
            group_of = np.concatenate([group_of, np.array(eg, np.int64)])
        xs = _pack_groups(ts, tile_of, group_of, NPAL_BANDED)
        if not cp:
            X, M = xs
            xs = [X[j][M[j] > 0] for j in range(NPAL_BANDED)]
        if cp:
            # 前のコマから始めるときは、前のパレットの色を k-means の初めの中心にして、くり返しも減らす
            ini = feat((init if r == 0 else P)[:, 1:].astype(np.float32)) if warm else None
            cols = unfeat(_kmeans_batch_gpu(cp, xs, 15, iters=WARM_KM_ITERS if warm else KM_ITERS, seed=7 + r,
                                            init=ini))
        else:
            cols = unfeat(np.stack([kmeans(x, 15, rng=rng) for x in xs]))
        P[:, 1:] = np.clip(np.round(cols), 0, 15)
        P[:, 0] = 0
        P[OSD_MASK, 14] = 0             # 時間表示 (パレット RAM の 7 番の色 14・15) は、そこに入るどのパレットでも固定
        P[OSD_MASK, 15] = 15
        _, err = quant_g(tiles, P, want_idx=False)
        a = err.argmin(1)
    return P


def _make_palset_gpu(cp, tiles, npal, rounds, init=None):
    """make_palset の GPU 版: 各回で、全パレットの kmeans (15色) をまとめて GPU で行う"""
    rng = np.random.default_rng(7)
    ts = feat(tiles)
    means = ts.mean(1)
    err = None
    if init is not None and init.shape[0] == npal:
        _, err = quant_all(tiles, init)
        a = err.argmin(1)
        rounds = WARM_ROUNDS
    else:
        c = kmeans(means, npal, rng=rng)
        a = ((means[:, None, :] - c[None]) ** 2).sum(-1).argmin(1)
    P = np.zeros((npal, 16, 3), np.float32)
    for r in range(rounds):
        worst = np.argsort(-err.min(1)) if err is not None else np.arange(NT)
        wi = 0
        xs = []
        for j in range(npal):
            sel = a == j
            if not sel.any():
                sel = np.zeros(NT, bool)
                sel[worst[wi:wi + 24]] = True
                wi += 24
            xs.append(ts[sel].reshape(-1, 3))
        cols = unfeat(_kmeans_batch_gpu(cp, xs, 15, iters=KM_ITERS, seed=7 + r))
        P[:, 1:] = np.clip(np.round(cols), 0, 15)
        P[:, 0] = 0
        if OSD_PAL < npal:
            P[OSD_PAL, 14] = 0
            P[OSD_PAL, 15] = 15
        _, err = quant_all(tiles, P)
        a = err.argmin(1)
    return P


# ---------------------------------------------------------------------------
# 色の少ないタイル (2色 / 4色)。読み込み速度が足りないとき、粗くても多くのタイルを書き換えるため
# ---------------------------------------------------------------------------
CX_BYTES = 30           # 2色: 色の番号2 + 8バイト、4色: 色の番号4 + 16バイト
C2_BYTES, C4_BYTES = 12, 21     # ストリーム上の大きさ (マップ値2 + 種類1 + 中身)
_AR = np.arange(NT)


def compact_approx(tiles, G, pal):
    """各タイルを、そのタイルのパレットの中の 2色 / 4色 だけで描いた近似。
    色は「加えると誤差が一番減る色」を順に選ぶ (2色の選び方は4色の最初の2つ)。
    戻り値: cx (NT, 30) uint8, ex (NT, 2) float32 (2色の誤差, 4色の誤差)"""
    cp = gpu()
    if cp:
        return _compact_approx_gpu(cp, tiles, G, pal)
    nt = len(tiles)
    ar = np.arange(nt)
    ps = feat(G[pal]).astype(np.float32)                     # (NT, 16, 3)
    ts = feat(tiles).astype(np.float32)                      # (NT, 64, 3)
    D = ((ts * ts).sum(2)[:, :, None] - 2 * np.matmul(ts, ps.transpose(0, 2, 1))
         + (ps * ps).sum(2)[:, None, :])                      # (NT, 64, 16) = |t-p|^2
    np.maximum(D, 0, out=D)
    best = np.full((nt, 64), np.inf, np.float32)
    S = np.zeros((nt, 4), np.intp)
    e2 = None
    for step in range(4):
        cand = np.minimum(best[:, :, None], D).sum(1)        # (NT, 16)
        for j in range(step):
            cand[ar, S[:, j]] = np.inf
        c = cand.argmin(1)
        S[:, step] = c
        best = np.minimum(best, D[ar, :, c])
        if step == 1:
            e2 = best.sum(1)
    e4 = best.sum(1)
    a2 = np.take_along_axis(D, S[:, None, :2].repeat(64, 1), 2).argmin(2)      # (NT, 64) 0/1
    a4 = np.take_along_axis(D, S[:, None, :].repeat(64, 1), 2).argmin(2)       # (NT, 64) 0-3
    cx = np.zeros((nt, CX_BYTES), np.uint8)
    cx[:, 0:2] = S[:, :2]
    cx[:, 2:10] = np.packbits(a2.reshape(nt, 8, 8).astype(np.uint8), axis=2)[:, :, 0]
    cx[:, 10:14] = S
    q = a4.reshape(nt, 16, 4).astype(np.uint8)
    cx[:, 14:30] = (q[:, :, 0] << 6) | (q[:, :, 1] << 4) | (q[:, :, 2] << 2) | q[:, :, 3]
    return cx, np.stack([e2, e4], 1).astype(np.float32)


def _compact_approx_gpu(cp, tiles, G, pal):
    nt = len(tiles)
    ar = cp.arange(nt)
    ps = feat(cp.asarray(G, cp.float32)[cp.asarray(pal)], cp)
    ts = feat(cp.asarray(tiles, cp.float32), cp)
    D = ((ts * ts).sum(2)[:, :, None] - 2 * cp.matmul(ts, ps.transpose(0, 2, 1))
         + (ps * ps).sum(2)[:, None, :])
    cp.maximum(D, 0, out=D)
    best = cp.full((nt, 64), cp.inf, cp.float32)
    S = cp.zeros((nt, 4), cp.int64)
    e2 = None
    for step in range(4):
        cand = cp.minimum(best[:, :, None], D).sum(1)
        for j in range(step):
            cand[ar, S[:, j]] = cp.inf
        c = cand.argmin(1)
        S[:, step] = c
        best = cp.minimum(best, D[ar, :, c])
        if step == 1:
            e2 = best.sum(1)
    e4 = best.sum(1)
    a2 = cp.take_along_axis(D, cp.repeat(S[:, None, :2], 64, 1), 2).argmin(2)
    a4 = cp.take_along_axis(D, cp.repeat(S[:, None, :], 64, 1), 2).argmin(2)
    S, a2, a4 = cp.asnumpy(S), cp.asnumpy(a2), cp.asnumpy(a4)
    cx = np.zeros((nt, CX_BYTES), np.uint8)
    cx[:, 0:2] = S[:, :2]
    cx[:, 2:10] = np.packbits(a2.reshape(nt, 8, 8).astype(np.uint8), axis=2)[:, :, 0]
    cx[:, 10:14] = S
    q = a4.reshape(nt, 16, 4).astype(np.uint8)
    cx[:, 14:30] = (q[:, :, 0] << 6) | (q[:, :, 1] << 4) | (q[:, :, 2] << 2) | q[:, :, 3]
    return cx, cp.asnumpy(cp.stack([e2, e4], 1).astype(cp.float32))


def compact_idx(cx, kind):
    """cx の行 (n, 30) → パレット内の色番号 (n, 64)。kind 2 / 4"""
    n = len(cx)
    if kind == 2:
        bits = np.unpackbits(cx[:, 2:10], axis=1)            # (n, 64) MSB から
        return cx[np.arange(n)[:, None], bits.astype(np.intp)]
    d = cx[:, 14:30]
    a = np.stack([(d >> 6) & 3, (d >> 4) & 3, (d >> 2) & 3, d & 3], 2).reshape(n, 64)
    return cx[np.arange(n)[:, None], 10 + a.astype(np.intp)]


# ---------------------------------------------------------------------------
# 解析パス
# ---------------------------------------------------------------------------
class Analysis:
    """1本の動画の解析結果(一時ファイル上)。"""

    def __init__(self, tmpdir, tag):
        self.tmpdir = tmpdir
        self.paths = {k: os.path.join(tmpdir, f'{tag}_{k}.bin') for k in ('src', 'idx', 'pal', 'cerr', 'cx', 'ex')}
        self.files = {k: open(p, 'wb') for k, p in self.paths.items()}
        self.psid = []
        self.palsets = []
        self.motion = []        # コマごとの画面全体の動き (前のコマのどこから来たか: dx, dy)
        self.n = 0

    def add(self, src_u8, idx, pal, cerr, psid, cx, ex):
        self.files['cx'].write(cx.tobytes())
        self.files['ex'].write(ex.astype(np.float32).tobytes())
        self.files['src'].write(src_u8.tobytes())
        self.files['idx'].write(idx.tobytes())
        self.files['pal'].write(pal.tobytes())
        self.files['cerr'].write(cerr.astype(np.float32).tobytes())
        self.psid.append(psid)
        self.n += 1

    def finish(self):
        for f in self.files.values():
            f.close()
        if self.n == 0:
            raise RuntimeError('映像フレームを1枚も読み込めませんでした。')
        self._map()

    def desc(self):
        """別プロセスで開き直すための情報"""
        return dict(paths=self.paths, n=self.n, palsets=self.palsets, psid=self.psid, motion=self.motion)

    @classmethod
    def reopen(cls, d):
        an = cls.__new__(cls)
        an.paths, an.n, an.palsets = d['paths'], d['n'], d['palsets']
        an.psid, an.motion = d['psid'], d['motion']
        an.files = {}
        an._map(arrays_ready=True)
        return an

    def _map(self, arrays_ready=False):
        n = max(self.n, 1)
        self.src = np.memmap(self.paths['src'], np.uint8, 'r', shape=(n, NT, 64, 3))
        self.idx = np.memmap(self.paths['idx'], np.uint8, 'r', shape=(n, NT, 64))
        self.pal = np.memmap(self.paths['pal'], np.uint8, 'r', shape=(n, NT))
        self.cerr = np.memmap(self.paths['cerr'], np.float32, 'r', shape=(n, NT))
        self.cx = np.memmap(self.paths['cx'], np.uint8, 'r', shape=(n, NT, CX_BYTES))
        self.ex = np.memmap(self.paths['ex'], np.float32, 'r', shape=(n, NT, 2))
        if arrays_ready:
            return
        self.psid = np.array(self.psid, np.int32)
        self.motion = np.array(self.motion, np.int16).reshape(-1, NPEAKS, 2)
        if len(self.motion) < self.n:
            self.motion = np.zeros((self.n, NPEAKS, 2), np.int16)

    def close(self):
        for f in self.files.values():
            if not f.closed:
                f.close()
        for k in ('src', 'idx', 'pal', 'cerr', 'cx', 'ex'):
            m = getattr(self, k, None)
            if m is not None:
                m._mmap.close()
                setattr(self, k, None)


_HANN = None


def global_motion(prev_f, cur_f, maxdx=32, maxdy=24):
    """位相相関で、画面全体が前のコマからどれだけ動いたかを出す。
    戻り値 [(mx, my), ...] (強い順に NPEAKS 個): 今のコマの点 (x, y) は前のコマの (x+mx, y+my) にあった。"""
    global _HANN
    if _HANN is None:
        _HANN = np.outer(np.hanning(H), np.hanning(W)).astype(np.float32)
    a = np.fft.rfft2(prev_f * _HANN)
    b = np.fft.rfft2(cur_f * _HANN)
    r = b * np.conj(a)
    r /= np.abs(r) + 1e-6
    corr = np.fft.irfft2(r, s=(H, W))
    corr[maxdy + 1:H - maxdy, :] = -1
    corr[:, maxdx + 1:W - maxdx] = -1
    out = []
    for _ in range(NPEAKS):
        dy, dx = np.unravel_index(int(corr.argmax()), corr.shape)
        if corr[dy, dx] <= 0:
            break
        corr[np.arange(dy - 2, dy + 3)[:, None] % H, np.arange(dx - 2, dx + 3) % W] = -1
        if dy > H // 2:
            dy -= H
        if dx > W // 2:
            dx -= W
        out.append((-int(dx), -int(dy)))
    while len(out) < NPEAKS:
        out.append((0, 0))
    return out


NPEAKS = 3          # 動きの候補 (位相相関の山) の数。背景と前景が別々に動く場面のため

def luma(fr):
    return (fr[..., 0] * 0.30 + fr[..., 1] * 0.59 + fr[..., 2] * 0.11).astype(np.float32)


def vmode_flags(vmode):
    """映像の方式 (Settings.video_mode) → (全部描き直し, パレットを作り直す間隔, 替える条件, ラスター)。
    環境変数 (WSVIDEO_ALLRAW など、試し用) が立っていれば、そちらも効く"""
    raw = ALL_RAW or vmode in ('raw', 'rawpal', 'raster')
    pe = PAL_EVERY or (1 if vmode in ('rawpal', 'raster') else 0)
    gain = PAL_EVERY_GAIN if PAL_EVERY else 0.9
    return raw, pe, gain, RASTER or vmode == 'raster'


def analyse(frames, an, dither=1.0, progress=None, total=None, cancel=None, finish=True, npal=16, palc=None,
            vmode='delta'):
    pal_grow, pal_gain = palc or (PAL_GROW, PAL_GAIN)
    raw, pal_every, every_gain, raster = vmode_flags(vmode)
    G = None
    e_base = 0.0
    last_try = last_change = -999
    prev = None
    prev_lum = None
    ar = np.arange(NT)
    d0 = dither
    for f, fr in enumerate(frames):
        if cancel and cancel():
            raise Cancelled()
        if raw and TDITHER:
            # 時間方向のディザ (試し): 1 コマおきに網目を反転し、続く 2 コマで中間の色に見せる (全面描き直しのときだけ)
            dither = d0 * TDITHER * (-1 if f & 1 else 1)
        tiles = frame_to_tiles(fr)
        cut = prev is not None and float(np.abs(tiles - prev).mean()) > 1.2
        prev = tiles
        if raw:
            an.motion.append([(0, 0)] * NPEAKS)     # 全部描き直しでは写しを使わないので、動きは調べない
        else:
            lum = luma(fr)
            an.motion.append(global_motion(prev_lum, lum) if f else [(0, 0)] * NPEAKS)
            prev_lum = lum
        E = None
        if G is not None:
            idx, e = quant_g(tiles, G, dither)
            best = e.argmin(1)
            E = float(e[ar, best].sum())
        try_new = G is None or (
            (f - last_try >= 6 or (cut and f - last_change >= 2)) and E > e_base * pal_grow + 60)
        if pal_every and G is not None:
            # 2026-11-26: パラパラ漫画の方式では毎コマ全体を書き直すので、パレットを替えても手間が増えない。
            # 毎コマ (pal_every コマごと) にパレットを作り直し、今のより良ければ替える
            try_new = f - last_try >= pal_every
        if try_new:
            last_try = f
            warm = G if (pal_every and WARM_ROUNDS and not cut) else None
            P = make_palset_banded(tiles, init=warm) if raster else make_palset(tiles, npal, init=warm)
            idx2, e2 = quant_g(tiles, P, dither)
            best2 = e2.argmin(1)
            E2 = float(e2[ar, best2].sum())
            if G is None or E2 < E * (every_gain if pal_every else pal_gain):
                G = P
                an.palsets.append(P)
                idx, e, best, E = idx2, e2, best2, E2
                last_change = f
            e_base = E
        sel = np.take_along_axis(idx, best[:, None, None], 2)[:, :, 0]
        if raw:
            # 全部描き直しでは 2色・4色のタイルを使わないので、近似は作らない
            cx, ex = np.zeros((NT, CX_BYTES), np.uint8), np.full((NT, 2), np.inf, np.float32)
        else:
            cx, ex = compact_approx(tiles, G, best)
        an.add(fr.reshape(TH, 8, TW, 8, 3).transpose(0, 2, 1, 3, 4).reshape(NT, 64, 3),
               sel, best.astype(np.uint8), e[ar, best], len(an.palsets) - 1, cx, ex)
        if progress and f % 5 == 0:
            progress(f, total)
    if finish:
        an.finish()
    return an



# ---------------------------------------------------------------------------
# 並列解析: 動画を時間で区切り、区間ごとに別プロセスで解析してつなげる
# ---------------------------------------------------------------------------
def _seg_worker(args):
    """(別プロセス) 1区間を解析し、一時ファイルの場所と結果を返す。"""
    path, fexpr, start, nframes, length, vkw, dither, tmpdir, tag, npal, palc, vmode = args
    an = Analysis(tmpdir, tag)
    try:
        frames = media.read_frames(path, fexpr, start, length, max_frames=nframes, **vkw)
        analyse(frames, an, dither, finish=False, npal=npal, palc=palc, vmode=vmode)
    finally:
        for f in an.files.values():
            f.close()
    return dict(tag=tag, paths=an.paths, n=an.n, palsets=an.palsets, psid=an.psid, motion=an.motion)


def _size_worker(args):
    """(別プロセス) 画質パラメータ tau のときの映像データの大きさ"""
    descs, tau, maxt, kf = args
    ans = [Analysis.reopen(d) for d in descs]
    try:
        sw, _, _, _ = delta_pass(ans, tau, maxt, kf=kf)
        return sw.pos
    finally:
        for an in ans:
            an.close()


def analyse_parallel(item, s, tmpdir, tag, est, workers, progress=None, cancel=None, pool=None):
    """1本の動画を区間に分けて並列に解析し、1つの Analysis にまとめる。"""
    fps = s.fps
    seg = int(min(900, max(150, math.ceil(est / max(1, workers * 2)))))
    nseg = max(1, math.ceil(est / seg))
    vkw = dict(mode=s.mode, brightness=s.brightness, contrast=s.contrast, saturation=s.saturation,
               deinterlace=s.deinterlace)
    jobs = []
    for i in range(nseg):
        st = item.start + i * seg / fps
        last = i == nseg - 1
        length = None
        if item.length:
            length = max(0.001, item.length - i * seg / fps)
        jobs.append((item.path, fps_expr(s.k, s.lines), st, None if last else seg, length, vkw, s.dither, tmpdir,
                     f'{tag}_s{i:04d}', s.npal, pal_change(s), s.video_mode))
    results = {}
    done_frames = 0
    it = pool.imap_unordered(_seg_worker, jobs) if pool else map(_seg_worker, jobs)
    for r in it:
        if cancel and cancel():
            raise Cancelled()
        results[r['tag']] = r
        done_frames += r['n']
        if progress:
            progress(done_frames, est)
    an = Analysis(tmpdir, tag)
    for job in jobs:
        r = results[job[8]]         # tag
        if r['n'] == 0:
            continue
        base = len(an.palsets)
        an.palsets.extend(r['palsets'])
        an.psid.extend([p + base for p in r['psid']])
        an.motion.extend(r['motion'])
        an.n += r['n']
        for k, p in r['paths'].items():
            with open(p, 'rb') as fp:
                while True:
                    blk = fp.read(1 << 22)
                    if not blk:
                        break
                    an.files[k].write(blk)
            os.unlink(p)
    an.finish()
    return an


# ---------------------------------------------------------------------------
# 差分パス
# ---------------------------------------------------------------------------
def runs_of(upd):
    """更新するタイル番号(昇順)を (skip, count) の並びにする。"""
    out = []
    p = 0
    i = 0
    n = len(upd)
    while i < n:
        t = upd[i]
        skip = t - p
        while skip > 255:
            out.append((255, 0))
            skip -= 255
            p += 255
        j = i
        while j + 1 < n and upd[j + 1] == upd[j] + 1 and j + 1 - i < 255:
            j += 1
        out.append((skip, j - i + 1))
        p = t + (j - i + 1)
        i = j + 1
    while p < NT:
        s = min(255, NT - p)
        out.append((s, 0))
        p += s
    return out


BULK_MIN = 3        # 普通のタイルがこれ以上続いたら「まとめ書き」にする
BULK_MAX = 64       # 1回のまとめ書きの最大 (2KB。一般 DMA の間 VBlank を待たせすぎないため)
RUN_MAX = 127       # 普通の区間の最大 (count の bit7 はまとめ書きの印)
# まとめ書きの重み (普通のタイル 1 = 約 194 サイクル): 1枚 = マップの値を書く約 30 + 一般 DMA 32 サイクル、
# 1回 = 見出し・一般 DMA の手間 約 100 サイクル
UNIT_BULK_TILE = 0.33
UNIT_BULK_RUN = 0.5
UNIT_FULL_EST = 0.45    # 選ぶときの普通のタイルの見積もり (まとめ書きになる前提。選んだあと実際の重みで確かめる)


def segments_of(sel, plain, use_bulk=True):
    """書き換えるタイル (sel、昇順) を区間に分ける。plain[j] = sel[j] が普通のタイル (まとめ書きできる)。
    戻り値: [(skip, j0, n, bulk)] (j0 = sel の中の位置、n = 枚数、n=0 は読み飛ばしだけ)"""
    out = []
    j = 0
    for skip, cnt in runs_of(list(sel)):
        if cnt == 0:
            out.append((skip, j, 0, False))
            continue
        k = j
        end = j + cnt
        first = True
        while k < end:
            m = k
            if use_bulk and plain[k]:
                while m < end and plain[m] and m - k < BULK_MAX:
                    m += 1
                if m - k >= BULK_MIN:
                    out.append((skip if first else 0, k, m - k, True))
                    first = False
                    k = m
                    continue
            # 普通の区間: 次のまとめ書きの始まりの手前まで (最大 RUN_MAX)
            m = k
            while m < end and m - k < RUN_MAX:
                if use_bulk and plain[m]:
                    r = m
                    while r < end and plain[r] and r - m < BULK_MIN:
                        r += 1
                    if r - m >= BULK_MIN:
                        break
                m += 1
            if m == k:
                m = k + 1
            out.append((skip if first else 0, k, m - k, False))
            first = False
            k = m
        j = end
    return out


def layout_size(segs, per, head):
    """区間の並びから、コマの大きさを出す (head = 先頭とパレットのバイト数。コマは偶数の番地から始まる)"""
    off = head
    for skip, j0, n, bulk in segs:
        off += 2
        if bulk:
            off += 2 * n
            off += off & 1
            off += 32 * n
        elif n:
            off += int(per[j0:j0 + n].sum())
    return off


def layout_units(segs, unt_sel, bulk_tile, bulk_run=None):
    """区間の並びの、実際の展開 (と読み込み) の重み。unt_sel = 選んだタイルそれぞれの普通の重み、
    bulk_tile = まとめ書きのタイル1枚の重み (読み込みを含む)"""
    bulk_run = UNIT_BULK_RUN if bulk_run is None else bulk_run
    u = 0.0
    for skip, j0, n, bulk in segs:
        if bulk:
            u += bulk_run + bulk_tile * n
        elif n:
            u += float(unt_sel[j0:j0 + n].sum())
    return u


def pal_bytes(P, b):
    v = np.clip(P, 0, 15).astype(np.uint16)
    words = (v[..., 0] << 8) | (v[..., 1] << 4) | v[..., 2]
    words[:, 0] = 0
    return words.astype('<u2').tobytes()


class StreamWriter:
    def __init__(self, write):
        self.pos = 0
        self.buf = bytearray() if write else None
        self.frame_pos = []

    def place(self, data_or_size):
        """コマを置く。コマは偶数の番地から始める (一般 DMA のため。間の詰め物は 0x40 = プレイヤーが読み飛ばす)。"""
        size = data_or_size if isinstance(data_or_size, int) else len(data_or_size)
        odd = self.pos & 1
        if (self.pos & 0xFFFF) + odd + size + 1 > BANK:
            nxt = (self.pos | 0xFFFF) + 1
            if self.buf is not None:
                self.buf += b'\x80' + b'\xff' * (nxt - self.pos - 1)
            self.pos = nxt
        elif odd:
            if self.buf is not None:
                self.buf += b'\x40'
            self.pos += 1
        self.frame_pos.append(self.pos)
        if self.buf is not None:
            self.buf += data_or_size
        self.pos += size

    def place_raw(self, size_np, palb, build=None):
        """全面描き直しのコマを置く。コマは偶数の番地から始まるので、マップの始まり (先頭 1 + パレット palb
        バイトの後) を偶数にそろえる詰め物は (1 + palb) & 1 バイト。build(pad) がデータを返す。戻り値: 大きさ"""
        pad = (1 + palb) & 1
        self.place(build(pad) if build else size_np + pad)
        return size_np + pad

    def align(self):
        """次のバンク(=チャンク)の先頭まで埋める (nileswan 版で動画の始まりをそろえる)。"""
        if self.pos & 0xFFFF:
            nxt = (self.pos | 0xFFFF) + 1
            if self.buf is not None:
                self.buf += b'\xff' * (nxt - self.pos)
            self.pos = nxt


def frame_size(full, nruns, n, ns, npal=8):
    return 1 + (32 * npal if full else 0) + 2 * nruns + 34 * n - 31 * ns


# 1タイルの展開にかかる時間の重み (フル = 1)。プレイヤーの計測にもとづく
UNIT_FULL, UNIT_SOLID, UNIT_COPY0, UNIT_COPYV, UNIT_COPYH = 1.0, 0.6, 0.6, 1.2, 4.0
UNIT_C2, UNIT_C4 = 1.45, 2.6     # 2色・4色タイルの展開 (tests/measure_units.py で K2・K3 の限界から決定)
# 種類ごとの重みの表。ROM 版は上の値のまま (UNITS_ROM)。
UNITS_ROM = dict(full=UNIT_FULL, solid=UNIT_SOLID, copy0=UNIT_COPY0, copyv=UNIT_COPYV, copyh=UNIT_COPYH,
                 c2=UNIT_C2, c4=UNIT_C4, bulk_tile=UNIT_BULK_TILE, bulk_run=UNIT_BULK_RUN, full_est=UNIT_FULL_EST,
                 raw=100)
# nileswan 版 (1.5.0〜): 実機で種類ごとに測ったタイル 1 枚のサイクル (KINDBNCH、TV スワン、2026-11-25)
#   まとめ書き 54・普通 126・4色 588・2色 388・1色 100・写し 224 / 364 / 692 (mesem との差は 1〜9%)
# を、NILE_CYC_PER_UNIT サイクル = 1 単位として使う。1コマの予算 (unit_budget) は mesem の試し (tests/make_hwtest.py の
# STATS で LATE 0) で決めた NILE_BUDGET_SCALE を掛ける。
NILE_CYC_PER_UNIT = 223.0
# 2026-11-26: 2色タイル (固定の表) と写し (ずれごとの専用コード) を速くした。実機の値に mesem-mod の KINDBNCH の
# 縮み方を掛けた: 2色 388 x 830/1229 = 262、縦ずれ 364 x 1177/1388 = 309、横ずれ 692 x 1293/2346 = 381。
_NC = {'full': 126, 'solid': 100, 'copy0': 224, 'copyv': 309, 'copyh': 381, 'c2': 262, 'c4': 588, 'bulk_tile': 54}
UNITS_NILE = {k: v / NILE_CYC_PER_UNIT for k, v in _NC.items()}
UNITS_NILE.update(bulk_run=0.5, full_est=(54 + 8) / NILE_CYC_PER_UNIT, raw=100)
NILE_BUDGET_SCALE = float(os.environ.get('WSVIDEO_NILEBUDGET', '1.0'))
NILE_WEIGHTS = os.environ.get('WSVIDEO_NILEW', 'new')     # 'old' なら nileswan 版も前の重み (比べる用)
# 全面描き直しのコマ (1.5.0〜): マップ 504 語 + タイル 504 x 32 バイトを一般 DMA で写す (1 バイト 1 サイクル、
# 26 回の手間込みで約 1.8 万サイクル ≒ 普通のタイル 94 枚分)。読み込みの重みは別に足す。
RAW_UNITS = 100
# 試し (2026-11-26、JISO のパラパラ漫画の案): 全部のコマを全面描き直しにする (差分を使わない)。WSVIDEO_ALLRAW=1
ALL_RAW = os.environ.get('WSVIDEO_ALLRAW') == '1'
# 試し: パレットを PAL_EVERY コマごとに作り直す (0 = 前の方法)。替えるのは誤差が PAL_EVERY_GAIN 倍より小さくなるとき
PAL_EVERY = int(os.environ.get('WSVIDEO_PALEVERY', '0'))
PAL_EVERY_GAIN = float(os.environ.get('WSVIDEO_PALEVERYGAIN', '1.0'))
# コマごとに作り直すとき、前のパレットから始める回数 (0 = 毎回はじめから作る)
WARM_ROUNDS = int(os.environ.get('WSVIDEO_WARMROUNDS', '2'))
WARM_KM_ITERS = int(os.environ.get('WSVIDEO_WARMKMITERS', '4'))     # そのときの k-means のくり返し (はじめからは KM_ITERS)
RAW_BYTES = 2 * 504 + 32 * 504
# 1コマに使える重みの合計 (K = 映像1コマあたりの VBlank 数)
MAX_UNITS = {2: 160, 3: 330, 4: 600, 5: 800}
COPY_BYTES = 4
# nileswan 版の CPU 時間の見積もり (1.3.2)。1.3.0 は読み込みの時間を見ておらず、激しい場面で
# 展開と読み込みの両方が CPU の時間を取り合い、コマ落ち → (1.3.1 で展開だけ減らしたら) 読み込みが追いつかず音飛び・早回し。
# 1バイト読む時間 ≒ 0.02 単位 (普通のタイル1枚の展開 ≒ 200 サイクル、1バイト ≒ 4 サイクルの見積もり。
# 250KB/秒を読み続けると CPU の約4割)。実機で測った値ではない。
NILE_UNIT_MARGIN = 0.90       # 一度に4セクタ読む遅れ・全体を書き直すコマ (上限の外) の分
# 実機の展開の速さ (1.3.3)。測定用 ROM (-DBENCH、変身シーン 192 コマ) で、実機 745 VBlank / エミュレーター 655 VBlank。
# その 192 コマの重みの合計から、実機で 1コマ (60Hz・K5) に展開できるのは約 805 単位 = MAX_UNITS から出す 1001 の 0.80 倍。
# 1.4.0: 音を音の DMA で鳴らすようにして (毎ラインの割り込みがなくなり)、同じ測定が実機 456 VBlank になった (−39%)。
# 1コマで展開できるのは 805 x 745 / 456 ≒ 1315 単位 = 1001 の 1.31 倍。1単位 ≒ 194 サイクル。
# 1.4.0 の後 (全面描き直し・まとめ書き・nileswan の trick): 新しい形式の測定 (60秒の区間の 183 コマ、重みの合計 91,222)
# が実機 400 VBlank = 1単位 ≒ 223 サイクル だった (4色タイルが多く、その重みが軽すぎるらしい)。安全側に全体を 194/223 倍。
NILE_HW_FACTOR = 1.13
# 読み込み: nileswan の trick で実機 143 VBlank (4096 セクタ、60Hz) = 1バイト約 3.47 サイクル ≒ 0.0156 単位 (223 サイクル)。
# (trick なしは 219 VBlank = 5.3 サイクルで 0.028。1.3.x は 1単位 316 サイクルで 0.02)
# 2026-11-26: データ 512 バイトを受ける間に前のセクタを写すようにして (nile.inc の .fast)、mesem-mod
# (実機の速さに合わせた版、SPDTRICK が実機と一致) で 143 → 104 VBlank = 1バイト約 2.52 サイクル ≒ 0.0113 単位。前と同じ余裕で 0.0123。
NILE_READ_UNIT = float(os.environ.get('WSVIDEO_NILEREAD', '0.0123'))

def unit_budget(s):
    """1コマで展開できるタイルの重みの合計 (60Hz は1画面が長いのでその分増やす)"""
    u = MAX_UNITS.get(s.k, 160 * s.k)
    u = u * s.lines / LINES
    if s.target == 'nile':
        # nileswan 版: 映像のデータを読む CPU 時間は delta_pass の read_cost で各タイルに足す。
        # ここでは音声のデータを読む分と、コマの合間に一度に最大4セクタ読むことによる遅れの分を引く。
        abytes = (s.audio_rate or 0) / s.fps
        u = u * NILE_HW_FACTOR * NILE_UNIT_MARGIN - NILE_READ_UNIT * abytes
        if NILE_WEIGHTS != 'old':
            u *= NILE_BUDGET_SCALE
    return u


def nile_weights():
    return UNITS_ROM if NILE_WEIGHTS == 'old' else UNITS_NILE


def keyframe_interval(s):
    """ジャンプ先の間隔 (コマ数、偶数)。0 = ジャンプなし"""
    if not KEYFRAME_SEC:
        return 0
    n = int(round(KEYFRAME_SEC * s.fps))
    return max(2, n + (n & 1))


_TX = (np.arange(NT) % TW) * 8
_TY = (np.arange(NT) // TW) * 8
_R8 = np.arange(8)


def tiles_img(t):
    """(NT, 64[, c]) → (H, W[, c])"""
    sh = t.shape[2:]
    return t.reshape((TH, TW, 8, 8) + sh).swapaxes(1, 2).reshape((H, W) + sh)


def _copy_unit(mx, my, w=None):
    w = w or UNITS_ROM
    return w['copy0'] if (mx % 8 == 0 and my % 8 == 0) else (w['copyv'] if mx % 8 == 0 else w['copyh'])


def copy_candidates(m_list, oidx_img, opal_img, G, src, valid_ps, w=None):
    """もう一方のバッファ (表示中) から写す候補を調べる。
    戻り値: err (NT,), idx (NT,64), pal (NT,), sx (NT,), sy (NT,), unit (NT,)  (使えない所は err=inf)"""
    cp = gpu()
    if cp and valid_ps and len(m_list):
        return _copy_candidates_gpu(cp, m_list, oidx_img, opal_img, G, src, w)
    best_err = np.full(NT, np.inf, np.float32)
    fsrc = feat(src)
    fG = feat(np.asarray(G, np.float32))
    best_idx = np.zeros((NT, 64), np.uint8)
    best_pal = np.zeros(NT, np.uint8)
    best_sx = np.zeros(NT, np.int16)
    best_sy = np.zeros(NT, np.int16)
    best_unit = np.zeros(NT, np.float32)
    if not valid_ps:
        return best_err, best_idx, best_pal, best_sx, best_sy, best_unit
    for mx, my in m_list:
        sx = _TX + mx
        sy = _TY + my
        ok = (sx >= 0) & (sx <= W - 8) & (sy >= 0) & (sy <= H - 8)
        if not ok.any():
            continue
        sxc = np.clip(sx, 0, W - 8)
        syc = np.clip(sy, 0, H - 8)
        rows = syc[:, None] + _R8
        cols = sxc[:, None] + _R8
        blk = oidx_img[rows[:, :, None], cols[:, None, :]].reshape(NT, 64)
        pb = opal_img[rows[:, :, None], cols[:, None, :]].reshape(NT, 64)
        same = (pb == pb[:, :1]).all(1)
        ok &= same
        p = pb[:, 0]
        err =((fG[p[:, None], blk] - fsrc) ** 2).sum((1, 2))
        err = np.where(ok, err, np.inf)
        better = err < best_err
        if better.any():
            best_err[better] = err[better]
            best_idx[better] = blk[better]
            best_pal[better] = p[better]
            best_sx[better] = sx[better]
            best_sy[better] = sy[better]
            best_unit[better] = _copy_unit(mx, my, w)
    return best_err, best_idx, best_pal, best_sx, best_sy, best_unit


def _copy_candidates_gpu(cp, m_list, oidx_img, opal_img, G, src, w=None):
    """copy_candidates の GPU 版 (同じ計算)。候補をまとめて計算する"""
    ms = np.array(m_list, np.int32).reshape(-1, 2)
    nc = len(ms)
    sx = _TX[None, :] + ms[:, :1]                                   # (nc, NT)
    sy = _TY[None, :] + ms[:, 1:]
    ok = (sx >= 0) & (sx <= W - 8) & (sy >= 0) & (sy <= H - 8)
    sxc = np.clip(sx, 0, W - 8)
    syc = np.clip(sy, 0, H - 8)
    oi = cp.asarray(oidx_img)
    op = cp.asarray(opal_img)
    rows = cp.asarray(syc)[:, :, None, None] + cp.asarray(_R8)[None, None, :, None]
    cols = cp.asarray(sxc)[:, :, None, None] + cp.asarray(_R8)[None, None, None, :]
    blk = oi[rows, cols].reshape(nc, NT, 64)
    pb = op[rows, cols].reshape(nc, NT, 64)
    okg = cp.asarray(ok) & (pb == pb[:, :, :1]).all(2)
    p = pb[:, :, 0]
    col = feat(cp.asarray(G, cp.float32), cp)[p[:, :, None], blk]    # (nc, NT, 64, 3) 距離の空間の座標
    err = ((col - feat(cp.asarray(src, cp.float32), cp)[None]) ** 2).sum((2, 3))
    err = cp.where(okg, err, cp.inf)
    # 候補の順に「より良ければ置き換える」= 最小 (同じ値なら先の候補)
    k = err.argmin(0)
    ar = cp.arange(NT)
    best_err = err[k, ar]
    blk_b = blk[k, ar]
    p_b = p[k, ar]
    k_h, best_err_h = cp.asnumpy(k), cp.asnumpy(best_err)
    units = np.array([_copy_unit(mx, my, w) for mx, my in ms], np.float32)
    fin = np.isfinite(best_err_h)
    arh = np.arange(NT)
    best_sx = np.where(fin, sx[k_h, arh], 0).astype(np.int16)
    best_sy = np.where(fin, sy[k_h, arh], 0).astype(np.int16)
    best_unit = np.where(fin, units[k_h], 0).astype(np.float32)
    best_idx = np.where(fin[:, None], cp.asnumpy(blk_b), 0).astype(np.uint8)
    best_pal = np.where(fin, cp.asnumpy(p_b), 0).astype(np.uint8)
    return best_err_h.astype(np.float32), best_idx, best_pal, best_sx, best_sy, best_unit


def delta_pass(analyses, tau, maxt, write=False, on_frame=None, cancel=None, align=False, rate=None,
               burst=None, kf=0, maxunits=None, use_copy=True, dither=1.0, fps_hint=15.0, use_compact=True,
               read_cost=0.0, use_raw=True, use_bulk=True, progress=None, weights=None, all_raw=False):
    """全動画の映像ストリームを作る。
    各タイルについて「そのまま」「新しく送る (32バイト / 1色なら1バイト)」「表示中のバッファから写す (2バイト)」
    のうち、誤差 + λ x バイト数 が一番小さいものを選ぶ (λ = tau / 34: 新しく送るのは誤差が tau 以上減るとき)。
    rate/burst: 1コマあたりの平均バイト数とため込める上限 (nileswan 版の読み込み速度の制限)。
    戻り値: (StreamWriter, 各動画の開始位置, 各動画のコマ位置の始まり, 統計)"""
    uw = weights or UNITS_ROM
    all_raw = all_raw or ALL_RAW            # 全部のコマを全面描き直し (パラパラ漫画の方式)
    sw = StreamWriter(write)
    starts = []
    fstarts = []
    stats = dict(tiles=0, frames=0, palframes=0, solid=0, limited=0, copies=0)
    lam = tau / 34.0
    if maxunits is None:
        maxunits = maxt
    total_frames = sum(an.n for an in analyses)
    done_frames = 0
    for an in analyses:
        if align:
            sw.align()
        starts.append(sw.pos)
        fstarts.append(len(sw.frame_pos))
        buf = np.zeros((2, NT, 64, 3), np.float32)
        bidx = np.zeros((2, NT, 64), np.uint8)
        bpal = np.zeros((2, NT), np.uint8)
        bufps = [-1, -1]
        bucket = burst if rate else None
        active = -1             # 今使っているパレットセット
        deferred = 0            # パレットの切り替えを待たせているコマ数
        for f in range(an.n):
            if cancel and f % 50 == 0 and cancel():
                raise Cancelled()
            b = f & 1
            ps = int(an.psid[f])
            pal = an.pal[f]
            idx = an.idx[f]
            cerr_f = an.cerr[f]
            cx_f = an.cx[f]
            ex_f = an.ex[f]
            keyframe = bool(kf and f % kf < 2)
            if ps != active:
                # 読み込み速度の余裕がないときは、パレットの切り替え (全体の書き直し2コマ分) を少し待つ。
                # 待つ間は今のパレットで減色し直す。ジャンプ先・最初・待ちすぎ (2秒) のときは切り替える。
                full_cost = 2 * (1 + 32 * an.palsets[ps].shape[0] + 34 * NT)
                if (all_raw or bucket is None or active < 0 or keyframe or deferred >= max(2, int(2 * fps_hint))
                        or bucket >= full_cost or bufps[0] != active or bufps[1] != active):
                    active = ps
                    deferred = 0
                else:
                    deferred += 1
                    stats['deferred'] = stats.get('deferred', 0) + 1
                    # 今のパレットで減色し直す (時間がかかるので、画面との誤差が大きい 200 タイルだけ。
                    # 残りは「そのまま」か「写す」しか選ばれないようにする)
                    Ga = an.palsets[active]
                    tiles = an.src[f].astype(np.float32) * (15.0 / 255.0)
                    curb = ((feat(buf[b]) - feat(tiles)) ** 2).sum((1, 2))
                    sub = np.argsort(-curb)[:200]
                    qi, qe = quant_all(tiles[sub], Ga, dither, pos=sub)
                    best = qe.argmin(1)
                    idx = np.zeros((NT, 64), np.uint8)
                    pal = np.zeros(NT, np.uint8)
                    cerr_f = np.full(NT, np.inf, np.float32)
                    idx[sub] = np.take_along_axis(qi, best[:, None, None], 2)[:, :, 0]
                    pal[sub] = best
                    cerr_f[sub] = qe[np.arange(len(sub)), best]
                    cx_f = np.zeros((NT, CX_BYTES), np.uint8)
                    ex_f = np.full((NT, 2), np.inf, np.float32)
                    cx_f[sub], ex_f[sub] = compact_approx(tiles[sub], Ga, pal[sub])
                    ps = active
            G = an.palsets[ps]
            # パレットが変わったコマと、ジャンプ先 (kf ごとの2コマ = A と B の両方) は全体を書き直す
            full = bufps[b] != ps or keyframe or all_raw
            raw = False                             # 全面描き直しの形式で書く
            if full:
                sel = np.arange(NT)
                kind = np.zeros(NT, np.int8)        # 0 = 新しく送る, 1 = 写す, 2 = 2色, 3 = 4色
                raw = use_raw
            else:
                src = an.src[f].astype(np.float32) * (15.0 / 255.0)
                cur = ((feat(buf[b]) - feat(src)) ** 2).sum((1, 2))
                ti_all = np.asarray(idx, np.uint8)
                solid_all = (ti_all == ti_all[:, :1]).all(1)
                # 選択肢: 0 そのまま / 1 新しく送る / 2 写す / 3 2色 / 4 4色
                err = np.empty((NT, 5), np.float32)
                byt = np.empty((NT, 5), np.float32)
                unt = np.empty((NT, 5), np.float32)
                err[:, 0], byt[:, 0], unt[:, 0] = cur, 0, 0
                err[:, 1] = cerr_f
                byt[:, 1] = np.where(solid_all, 3, 34)
                unt[:, 1] = np.where(solid_all, uw['solid'], uw['full_est'] if use_bulk else uw['full'])
                err[:, 2], byt[:, 2], unt[:, 2] = np.inf, COPY_BYTES, 0
                if use_copy and bufps[b ^ 1] == ps:
                    cands = [(0, 0)]
                    for mx, my in (an.motion[f].tolist() if f < len(an.motion) else []):
                        cnd = (2 * int(round(mx / 2)), int(my))      # 横は 2ドット単位
                        if cnd not in cands:
                            cands.append(cnd)
                    oidx_img = tiles_img(bidx[b ^ 1])
                    opal_img = np.repeat(np.repeat(bpal[b ^ 1].reshape(TH, TW), 8, 0), 8, 1)
                    cerr_c, cidx, cpal, csx, csy, cunit = copy_candidates(cands, oidx_img, opal_img, G, src, True, uw)
                    err[:, 2], unt[:, 2] = cerr_c, cunit
                err[:, 3], byt[:, 3], unt[:, 3] = ex_f[:, 0], C2_BYTES, uw['c2']
                err[:, 4], byt[:, 4], unt[:, 4] = ex_f[:, 1], C4_BYTES, uw['c4']
                if not use_compact:
                    err[:, 3:] = np.inf
                if read_cost:
                    # nileswan 版: そのデータを microSD から読む CPU 時間も、同じコマの時間から出す
                    unt += read_cost * byt
                bbud = (bucket - 1 - 2 * 64) if bucket is not None else np.inf
                lim0 = stats['limited']
                ch = choose_options(err, byt, unt, lam, bbud, maxunits)
                if ch is None:
                    ch = choose_options(err, byt, unt, lam, bbud, maxunits, force=True)
                    stats['limited'] += 1
                elif ch[1]:
                    stats['limited'] += 1
                if use_bulk:
                    # 選ぶときは普通のタイルを「まとめ書き」の重みで見積もった。実際の区間の分け方で重みを出し、
                    # 予算を超えていたら予算を少し下げて選び直す
                    unt_true = unt.copy()
                    unt_true[:, 1] = np.where(solid_all, uw['solid'], uw['full']) + read_cost * byt[:, 1]
                    bulk_tile = uw['bulk_tile'] + read_cost * 34

                    def real_units(c):
                        s_ = np.nonzero(c != 0)[0]
                        if not len(s_):
                            return 0.0
                        pl = (c[s_] == 1) & ~solid_all[s_]
                        return layout_units(segments_of(s_, pl), unt_true[s_, c[s_]], bulk_tile, uw['bulk_run'])

                    mu = maxunits
                    for _ in range(4):
                        uu = real_units(ch[0])
                        if uu <= maxunits:
                            break
                        mu *= maxunits / uu * 0.97
                        c2 = choose_options(err, byt, unt, lam, bbud, mu)
                        if c2 is None:
                            c2 = choose_options(err, byt, unt, lam, bbud, mu, force=True)
                        ch = (c2[0], True)
                        if stats['limited'] == lim0:
                            stats['limited'] += 1
                limited = stats['limited'] > lim0
                ch = ch[0]
                if use_raw and limited:
                    # 上限で絞ったコマ: 全部を新しく送る (全面描き直し) 方が誤差が小さく、予算に収まるならそうする
                    rsize = 2 + RAW_BYTES
                    runits = uw['raw'] + read_cost * rsize
                    if runits <= maxunits and rsize <= bbud:
                        ar = np.arange(NT)
                        j_raw = float(cerr_f.sum()) + lam * rsize
                        j_del = float(err[ar, ch].sum()) + lam * float(byt[ar, ch].sum())
                        if j_raw < j_del:
                            raw = True
                            ch = np.ones(NT, np.int64)
                            stats['limited'] -= 1
                sel = np.nonzero(ch != 0)[0]
                kind = (ch[sel] - 1).astype(np.int8)
            n = len(sel)
            # 書き換えるタイルの中身 (パレット内の色番号) とパレット番号
            newidx = np.empty((n, 64), np.uint8)
            newpal = np.empty(n, np.uint8)
            for kd in range(4):
                m = kind == kd
                if not m.any():
                    continue
                s_ = sel[m]
                if kd == 1:
                    newidx[m] = cidx[s_]
                    newpal[m] = cpal[s_]
                else:
                    newidx[m] = idx[s_] if kd == 0 else compact_idx(np.asarray(cx_f[s_]), 2 if kd == 2 else 4)
                    newpal[m] = pal[s_]
            if n:
                buf[b][sel] = G[newpal[:, None], newidx]
                bidx[b][sel] = newidx
                bpal[b][sel] = newpal
            bufps[b] = ps
            solid = (kind == 0) & (newidx == newidx[:, :1]).all(1) if n else np.zeros(0, bool)
            per = np.select([solid, kind == 0, kind == 1, kind == 2], [3, 34, COPY_BYTES, C2_BYTES], C4_BYTES) \
                if n else np.zeros(0, int)
            palb = 32 * G.shape[0] if full else 0
            segs = [] if raw else segments_of(sel, (kind == 0) & ~solid, use_bulk)
            if raw:
                size = None             # 置き場所で 0〜1 バイト変わる (place_raw で決める)
                stats['raw'] = stats.get('raw', 0) + 1
            else:
                size = layout_size(segs, per, 1 + palb)
                stats['bulk'] = stats.get('bulk', 0) + sum(s_[2] for s_ in segs if s_[3])
            stats['tiles'] += n
            stats['solid'] += int(solid.sum())
            stats['copies'] += int((kind == 1).sum())
            stats['c2'] = stats.get('c2', 0) + int((kind == 2).sum())
            stats['c4'] = stats.get('c4', 0) + int((kind == 3).sum())
            stats['frames'] += 1
            stats['palframes'] += int(full)
            if raw:
                pal16 = G.shape[0] == 16
                banded = G.shape[0] == NPAL_BANDED
                band8 = BAND_KIND == '8x8'

                def build_raw(pad):
                    pn = (newpal.astype(np.uint16) & 15) if banded else (newpal.astype(np.uint16) + (0 if pal16 else 8 * b))
                    mw = (np.arange(NT, dtype=np.uint16) | (pn << 9) | (b << 13)).astype('<u2')
                    packed = ((newidx[:, 0::2] << 4) | newidx[:, 1::2]).astype(np.uint8)
                    # 先頭: bit0 パレットあり, bit1 16 パレット, bit2 全面描き直し, bit3 帯のパレット (72 本、ラスター)
                    # bit4 (bit3 と一緒) = 8 ラインの帯 (144 本)。詰め物 1 バイトを先に置き、パレットを偶数の番地から (一般 DMA で写す)
                    if banded and full and band8:
                        return bytes([0x1F]) + b'\0' * pad + pal_bytes(G, b) + mw.tobytes() + packed.tobytes()
                    head = bytes([((0x0B if banded else 3 if pal16 else 1) if full else 0) | 4])
                    return (head + (pal_bytes(G, b) if full else b'') + b'\0' * pad
                            + mw.tobytes() + packed.tobytes())

                size = sw.place_raw(1 + palb + RAW_BYTES, palb, build_raw if write else None)
            if bucket is not None:
                bucket = min(burst, bucket + rate) - size
            if raw:
                pass
            elif write:
                pal16 = G.shape[0] == 16
                # 先頭: bit0 = パレットあり, bit1 = 16パレット (画面を切り替えるときに書く)
                parts = [bytes([(3 if pal16 else 1) if full else 0])]
                if full:
                    parts.append(pal_bytes(G, b))
                recs = []
                if n:
                    packed = ((newidx[:, 0::2] << 4) | newidx[:, 1::2]).astype(np.uint8)
                    flag = np.select([solid, kind == 1, kind >= 2], [0x8000, 0x4000, 0xC000], 0).astype(np.uint16)
                    mw = (sel.astype(np.uint16) | ((newpal.astype(np.uint16) + (0 if pal16 else 8 * b)) << 9)
                          | (b << 13) | flag).astype('<u2')
                    mwb = mw.view(np.uint8).reshape(-1, 2)
                    for j in range(n):
                        t = int(sel[j])
                        kd = int(kind[j])
                        if kd == 1:
                            recs.append(mwb[j].tobytes() + bytes((int(csx[t]), int(csy[t]))))
                        elif kd == 2:
                            q = cx_f[t]
                            recs.append(mwb[j].tobytes() + bytes((1, int(q[0]) << 4 | int(q[1]))) + bytes(q[2:10]))
                        elif kd == 3:
                            q = cx_f[t]
                            recs.append(mwb[j].tobytes() + bytes((2, int(q[10]) << 4 | int(q[11]),
                                                                  int(q[12]) << 4 | int(q[13]))) + bytes(q[14:30]))
                        elif solid[j]:
                            recs.append(mwb[j].tobytes() + packed[j, :1].tobytes())
                        else:
                            recs.append(mwb[j].tobytes() + packed[j].tobytes())
                off = sum(len(p_) for p_ in parts)
                for skip, j0, cnt, bulk in segs:
                    if bulk:
                        # まとめ書き: マップの値を n 個、偶数にそろえ、タイルを n 枚
                        parts.append(bytes([skip, 0x80 | cnt]))
                        parts.append(mw[j0:j0 + cnt].tobytes())
                        off += 2 + 2 * cnt
                        if off & 1:
                            parts.append(b'\0')
                            off += 1
                        parts.append(packed[j0:j0 + cnt].tobytes())
                        off += 32 * cnt
                    else:
                        parts.append(bytes([skip, cnt]))
                        off += 2
                        if cnt:
                            parts.extend(recs[j0:j0 + cnt])
                            off += sum(len(r_) for r_ in recs[j0:j0 + cnt])
                data = b''.join(parts)
                assert len(data) == size, (len(data), size)
                sw.place(data)
            else:
                sw.place(size)
            if raw and write:
                assert sw.pos - sw.frame_pos[-1] == size
            if on_frame:
                on_frame(an, f, buf[b])
            done_frames += 1
            if progress and done_frames % 30 == 0:
                progress(done_frames, total_frames)
    return sw, starts, fstarts, stats


def choose_options(err, byt, unt, lam, bbud, maxunits, force=False):
    """各タイルの選択肢から「誤差 + λ x バイト + ν x 展開時間」が最小のものを選ぶ。
    バイトの予算 bbud と展開時間の予算 maxunits に収まるように λ・ν を二分探索で上げる。
    戻り値: (選んだ番号の配列, 予算で絞ったか) / 収まらなければ None (force なら効果の小さいものから外す)"""
    ar = np.arange(len(err))

    def pick(l, nu):
        J = err + l * byt + nu * unt
        c = J.argmin(1)
        return c, float(byt[ar, c].sum()), float(unt[ar, c].sum())

    def ok(r):
        return r[1] <= bbud and r[2] <= maxunits

    r = pick(lam, 0.0)
    if ok(r):
        return r[0], False
    l, nu = lam, 0.0
    for _ in range(3):
        if r[1] > bbud:                          # バイトが多い → λ を上げる
            lo, hi = l, max(l, 1e-3) * 2
            while not pick(hi, nu)[1] <= bbud and hi < 1e9:
                hi *= 2
            for _ in range(10):
                mid = (lo * hi) ** 0.5
                if pick(mid, nu)[1] <= bbud:
                    hi = mid
                else:
                    lo = mid
            l = hi
            r = pick(l, nu)
        if r[2] > maxunits:                      # 展開が間に合わない → ν を上げる
            lo, hi = nu, max(nu, 1e-3) * 2
            while not pick(l, hi)[2] <= maxunits and hi < 1e9:
                hi *= 2
            lo = lo if lo > 0 else hi * 1e-6
            for _ in range(10):
                mid = (lo * hi) ** 0.5
                if pick(l, mid)[2] <= maxunits:
                    hi = mid
                else:
                    lo = mid
            nu = hi
            r = pick(l, nu)
        if ok(r):
            return r[0], True
    if not force:
        return None
    # それでも収まらないときは、効果の小さいものから「そのまま」に戻す
    c = r[0].copy()
    gain = err[:, 0] - err[ar, c]
    order = np.argsort(gain)
    B, U = r[1], r[2]
    for t in order:
        if B <= bbud and U <= maxunits:
            break
        if c[t] == 0:
            continue
        B -= byt[t, c[t]]
        U -= unt[t, c[t]]
        c[t] = 0
    return c, True
# ---------------------------------------------------------------------------
# nileswan 用の .WSV ファイル
# ---------------------------------------------------------------------------
WSV_HDR_ITEMS = 0x10
WSV_HDR_BITMAP = 0x210
WSV_HDR_MAXSEC = 20      # 内蔵RAM 0xC200-0xE9FF に入る大きさ (2026-11-26 に 29 → 20。0xEA00- はラスターの帯のパレット、
                         # 0xFC00- はパレットの置き場。プレイヤーの HDRMAX と同じにする)
NILE_PREBUF = 6           # プレイヤーが再生前に読む映像チャンク数 (player.asm の PREBUF_V)


def wsv_name(rom_path):
    """ROM のファイル名から、microSD に置くデータファイルの 8.3 名を作る。"""
    import hashlib
    import re
    stem = os.path.splitext(os.path.basename(rom_path))[0]
    base = re.sub(r'[^A-Z0-9]', '', stem.upper())[:5] or 'VIDEO'
    h = hashlib.md5(stem.encode('utf-8')).hexdigest()[:3].upper()
    return f'{base}{h}.WSV'


def write_wsv(path, sw, starts, fstarts, audios, nframes, s, kf=0):
    """映像と音声を 64KB チャンクに分け、必要になる順に並べて .WSV に書く。
    kf > 0 なら kf コマごとのジャンプ表もヘッダーに入れる。"""
    vdata = sw.buf
    item_chunks = []
    item_seeks = []
    ends = starts[1:] + [len(vdata)]
    for i, (vs, ve) in enumerate(zip(starts, ends)):
        fp = sw.frame_pos[fstarts[i]:fstarts[i] + nframes[i]]
        chunks = []            # (時刻, 種類(0=音声 1=映像), 種類内の番号, データ)
        nv = math.ceil((ve - vs) / BANK)
        fi = 0
        for k in range(nv):
            lo = vs + k * BANK
            while fi + 1 < len(fp) and fp[fi + 1] < lo:
                fi += 1
            first = fi if fp[fi] >= lo or k == 0 else fi + 1
            t = max(0, (first - 1) * s.k)
            data = bytes(vdata[lo:min(ve, lo + BANK)]).ljust(BANK, b'\xff')
            chunks.append((t, 1, k, data))
        a = audios[i]
        if len(a):
            per_tick = s.lines * s.audio_bpl / s.audio_div       # 1 VBlank あたりの音のバイト数
            for j in range(math.ceil(len(a) / BANK)):
                data = a[j * BANK:(j + 1) * BANK].tobytes().ljust(BANK, b'\x80')
                chunks.append((j * BANK / per_tick, 0, j, data))
        chunks.sort(key=lambda c: (c[0], c[1], c[2]))
        item_chunks.append(chunks)
        # ジャンプ表: (読み始めるチャンク(動画内), それまでの映像/音声チャンク数, 映像の位置, 音声の位置)
        seeks = []
        if kf:
            pos = {(typ, n): idx for idx, (_, typ, n, _) in enumerate(chunks)}
            vbefore = np.cumsum([0] + [1 if c[1] == 1 else 0 for c in chunks])
            abefore = np.cumsum([0] + [1 if c[1] == 0 else 0 for c in chunks])
            for f in range(0, nframes[i], kf):
                vp = fp[f] - vs
                vk, voff = vp >> 16, vp & 0xFFFF
                start = pos[(1, vk)]
                ak = aoff = 0
                if len(a):
                    smp = f * s.k * s.lines // s.audio_div * s.audio_bpl      # バイトの位置 (ステレオでも偶数 = 左から)
                    ak, aoff = smp >> 16, smp & 0xFFFF
                    start = min(start, pos[(0, ak)])
                seeks.append((start, int(vbefore[start]), int(abefore[start]), vk, voff, ak, aoff))
        item_seeks.append(seeks)
    total = 1 + sum(len(c) for c in item_chunks)
    bitmap = bytearray(math.ceil(total / 8))
    seek_off = (WSV_HDR_BITMAP + len(bitmap) + 15) & ~15
    hsize = seek_off + 16 * sum(len(x) for x in item_seeks)
    hsec = math.ceil(hsize / 512)
    if hsec > WSV_HDR_MAXSEC:
        raise RuntimeError('動画が長すぎます（データファイルは約4.9GBまで）。')
    hdr = bytearray(BANK)
    hdr[0:4] = b'WSVD'
    hdr[4:8] = bytes([2 | (0x80 if s.lines != LINES else 0), s.k, s.audio_div | (s.audio_flags if s.audio_div else 0),
                      len(item_chunks)])
    hdr[8:12] = total.to_bytes(4, 'little')
    hdr[12:14] = hsec.to_bytes(2, 'little')
    hdr[14:16] = kf.to_bytes(2, 'little')
    c = 1
    so = seek_off
    with open(path, 'wb') as fpw:
        fpw.write(b'\0' * BANK)
        for i, chunks in enumerate(item_chunks):
            o = WSV_HDR_ITEMS + 8 * i
            hdr[o:o + 4] = c.to_bytes(4, 'little')
            hdr[o + 4:o + 6] = nframes[i].to_bytes(2, 'little')
            hdr[o + 6:o + 8] = so.to_bytes(2, 'little')
            for st, vb, ab, vk, voff, ak, aoff in item_seeks[i]:
                hdr[so:so + 16] = struct.pack('<IHHHHHH', c + st, vb, ab, vk, voff, ak, aoff)
                so += 16
            for t, typ, n, data in chunks:
                if typ == 0:
                    bitmap[c >> 3] |= 1 << (c & 7)
                fpw.write(data)
                c += 1
        hdr[WSV_HDR_BITMAP:WSV_HDR_BITMAP + len(bitmap)] = bitmap
        fpw.seek(0)
        fpw.write(hdr)
    return total * BANK

# ---------------------------------------------------------------------------
# 全体
# ---------------------------------------------------------------------------
class Item:
    def __init__(self, path, start=0.0, length=None, title=None):
        self.path = path
        self.start = start or 0.0
        self.length = length
        self.title = title or os.path.basename(path)


class Settings:
    def __init__(self, k=5, audio_div=1, max_rom_mb=16, quality='auto', mode='fit', dither=1.0,
                 brightness=0.0, contrast=1.0, saturation=1.0, normalize=True, volume=1.0,
                 deinterlace=False, preview_path=None, speaker_shift=1, target='rom', sd_kbps=160,
                 workers=None, hz60=False, pal16=True, audio_mode='mono', video_mode='delta'):
        # 音 (2026-11-26、nileswan 版だけ): 'mono' = 12kHz モノラル (今まで) / 'hi' = 24kHz モノラル /
        # 'stereo' = 12kHz ステレオ (左右交互。ヘッドホン・TV スワンはハイパーボイスで左右、本体のスピーカーは 24kHz で交互に鳴らす)
        self.audio_mode = audio_mode if target == 'nile' else 'mono'
        # 映像の方式 (2026-11-26、nileswan 版だけ): 'delta' = 変わった所だけ書く (今まで) / 'raw' = 毎コマ全部描き直す (パラパラ) /
        # 'rawpal' = それ + コマごとのパレット / 'raster' = それ + ラスター (16 ラインごとに帯のパレット)
        self.video_mode = video_mode if target == 'nile' else 'delta'
        # 16パレット (241色) を毎コマ使う。False なら 8パレット (121色、古い形式・変換が速い)
        self.npal = 16 if pal16 else 8
        # 60Hz モード (実験的): 1画面 199 ライン = 60.3Hz (通常は 159 ライン = 75.47Hz)。
        # ポート0x16 は偶数 (198) にする (SwanCrystal で奇数は液晶に悪いとされるため)
        self.lines = 199 if hz60 else LINES
        # 内蔵スピーカーのシフト (ポート0x91): 0=100%(音割れする) 1=50%(標準) 2=25% 3=12.5%
        self.speaker_shift = speaker_shift
        self.k = k
        self.audio_div = audio_div
        self.max_rom_mb = max_rom_mb
        self.quality = quality
        self.mode = mode
        self.dither = dither
        self.brightness = brightness
        self.contrast = contrast
        self.saturation = saturation
        self.normalize = normalize
        self.volume = volume
        self.deinterlace = deinterlace
        self.preview_path = preview_path
        self.target = target            # 'rom' = 通常の ROM / 'nile' = nileswan の microSD から読む
        self.sd_kbps = sd_kbps          # nileswan 版: microSD から読む速さの上限 (KB/秒)
        self.workers = workers          # 解析に使うプロセス数 (None = 自動)

    @property
    def fps(self):
        return CPU_HZ / (256 * self.lines) / self.k

    @property
    def audio_bpl(self):
        """1 回 (R ライン) あたりの音のバイト数: 'hi'・'stereo' は 2"""
        return 2 if self.audio_mode in ('hi', 'stereo') else 1

    @property
    def audio_rate(self):
        """音のデータの 1 秒あたりのバイト数"""
        return 12000 * self.audio_bpl // self.audio_div if self.audio_div else 0

    @property
    def audio_channels(self):
        return 2 if self.audio_mode == 'stereo' else 1

    @property
    def audio_flags(self):
        """.WSV のヘッダーの音の設定 (R) に足すビット: bit7 = 1 回 2 バイト (音の DMA 24kHz)、bit6 = ステレオ"""
        return (0x80 if self.audio_bpl == 2 else 0) | (0x40 if self.audio_mode == 'stereo' else 0)


class Result:
    pass


def audio_len(nframes, s):
    if not s.audio_div:
        return 0
    return math.ceil(nframes * s.k * s.lines / s.audio_div) * s.audio_bpl + 2048


def default_workers():
    n = max(1, min(16, (os.cpu_count() or 2) - 1))
    if gpu():
        # GPU を使うときは、重い計算は GPU がするので、プロセスは少なくてよい (1つごとに GPU の準備に数秒・数百MB)
        n = min(n, 8)
    return n


def lower_priority():
    """変換中もパソコンのほかの操作が重くならないように、このプロセスの優先度を「通常以下」にする。
    Windows では、あとから起動する ffmpeg や並列処理の子プロセスもこの優先度を引き継ぐ。"""
    if os.name != 'nt':
        return
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.SetPriorityClass(k32.GetCurrentProcess(), 0x4000)   # BELOW_NORMAL_PRIORITY_CLASS
    except Exception:
        pass


class AnalysisCache:
    """解析結果を覚えておき、設定(形式・画質・容量など)だけ変えてやり直すときに使い回す。
    映像の見た目に関わる設定 (fps・画面の合わせ方・明るさ等・ディザ) が同じなら再利用する。"""

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix='wsvideo_cache_')
        self.ans = {}
        self.audio = {}
        self.n = 0

    def tag(self):
        self.n += 1
        return f'c{self.n}'

    def prune(self, keep, akeep):
        for k in list(self.ans):
            if k not in keep:
                an = self.ans.pop(k)
                an.close()
                for p in an.paths.values():
                    try:
                        os.unlink(p)
                    except OSError:
                        pass
        for k in list(self.audio):
            if k not in akeep:
                del self.audio[k]
        used = {os.path.normcase(p) for an in self.ans.values() for p in an.paths.values()}
        for fn in os.listdir(self.dir):
            p = os.path.join(self.dir, fn)
            if os.path.normcase(p) not in used:
                try:
                    os.unlink(p)
                except OSError:
                    pass

    def close(self):
        self.prune(set(), set())
        try:
            os.rmdir(self.dir)
        except OSError:
            pass


def _file_id(path):
    try:
        st = os.stat(path)
        return (os.path.abspath(path), st.st_size, st.st_mtime)
    except OSError:
        return (os.path.abspath(path),)


NO_FIT_HINT = ('\n「出力の形式」を「nileswan の microSD から再生」にすると長さの制限なく入ります'
               '（今回の解析結果は覚えているので、アプリを閉じなければ解析はやり直しません）。'
               '\nROM に入れる場合は、動画を短くするか、フレームレート・音声の品質を下げてください。')


def rom_fit_precheck(durs, s):
    """解析の前に、ROM 版に明らかに入らないかを調べる (入らなければ説明文、入りそうなら None)。"""
    cap = (s.max_rom_mb * 16 - 1) * BANK
    tot = sum(durs)
    audio = tot * s.audio_rate
    frames = tot * s.fps
    # どんなに画質を落としても 1コマに最低 ~300 バイト (画面の切り替わり・パレット更新) はかかる
    if audio + frames * 300 > cap:
        return (f'合計 {int(tot // 60)}分{int(tot % 60):02d}秒 は ROM {s.max_rom_mb}MB に入りません'
                f'（音声だけで約{audio / 1048576:.1f}MB）。\n'
                f'「出力の形式」を「nileswan の microSD から再生」にすると長さの制限なく入ります。'
                f'（ROM に入れる場合は、動画を短くするか、フレームレート・音声の品質を下げてください）')
    return None


def convert(items, s, out_path, progress=None, log=None, cancel=None, cache=None):
    """items: [Item], s: Settings。ROM を out_path に書き、Result を返す。
    s.target == 'nile' のときは out_path の隣に microSD 用のデータファイル (.WSV) も書く。
    cache (AnalysisCache) を渡すと、前回と同じ動画・見た目の設定なら解析をやり直さない。"""
    import multiprocessing as mp
    from . import rom as romlib
    prog = progress or (lambda frac, msg: None)
    say = log or (lambda msg: None)
    nile = s.target == 'nile'
    if not items:
        raise RuntimeError('動画が指定されていません。')
    if len(items) > 64:
        raise RuntimeError('1つのROMに入れられる動画は64本までです。')
    lower_priority()
    maxt = unit_budget(s)
    tmpdir = cache.dir if cache else tempfile.mkdtemp(prefix='wsvideo_')
    analyses = []
    audios = []
    keys, akeys = set(), set()
    pool = None
    t0 = time.time()
    pw = None
    try:
        infos = [media.probe(it.path) for it in items]
        durs = []
        for it, inf in zip(items, infos):
            if not inf.has_video:
                raise RuntimeError(f'映像がありません: {it.path}')
            d = inf.duration or 0
            d = max(0.0, d - it.start)
            if it.length:
                d = min(d, it.length)
            durs.append(d)
        if not nile:
            msg = rom_fit_precheck(durs, s)
            if msg:
                raise RuntimeError(msg)
        total_d = sum(durs) or 1
        workers = s.workers or default_workers()
        vkeys = [_file_id(it.path) + (it.start, it.length, s.k, s.lines, s.mode, s.brightness, s.contrast, s.saturation,
                                      s.deinterlace, s.dither, s.npal, pal_change(s), s.video_mode) for it in items]
        need_pool = not cache or any(k not in cache.ans for k in vkeys)
        if need_pool and workers > 1 and sum(durs) * s.fps > 300:
            os.environ.setdefault('OMP_NUM_THREADS', '1')
            os.environ.setdefault('OPENBLAS_NUM_THREADS', '1')
            os.environ.setdefault('MKL_NUM_THREADS', '1')
            pool = mp.get_context('spawn').Pool(workers)
            gn = gpu_name()
            say(f'解析に {workers} プロセスを使います。' + (f'GPU（{gn}）で計算します。' if gn else 'CPU で計算します。'))
        done_d = 0.0
        for i, (it, inf, d) in enumerate(zip(items, infos, durs)):
            say(f'[{i + 1}/{len(items)}] 解析中: {it.title}')
            est = max(1, int(d * s.fps))

            def p(f, _t, d0=done_d, d=d, est=est, title=it.title):
                el = time.time() - t0
                prog(0.85 * (d0 + d * min(1, f / est)) / total_d, f'解析中 {title}  {f}/{est} コマ')

            vk = vkeys[i]
            if cache and vk in cache.ans:
                an = cache.ans[vk]
                say('    前回の解析結果を使います')
            else:
                an = analyse_parallel(it, s, tmpdir, cache.tag() if cache else f'v{i}', est, workers, p, cancel,
                                      pool)
                if cache:
                    cache.ans[vk] = an
            keys.add(vk)
            analyses.append(an)
            done_d += d
            if s.audio_div:
                need = audio_len(an.n, s)
                ak = _file_id(it.path) + (it.start, it.length, s.audio_rate, s.normalize, s.volume, s.audio_channels)
                akeys.add(ak)
                if cache and ak in cache.audio:
                    a = cache.audio[ak]
                else:
                    a = media.read_audio(it.path, s.audio_rate // s.audio_channels, it.start, it.length, s.normalize,
                                         s.volume, s.audio_channels) \
                        if inf.has_audio else np.zeros(0, np.uint8)
                    if cache:
                        cache.audio[ak] = a
                a = a[:need - 2048]
                a = np.concatenate([a, np.full(need - len(a), 0x80, np.uint8)])
                audios.append(a)
            else:
                audios.append(np.zeros(0, np.uint8))
            say(f'    {an.n} コマ ({an.n / s.fps:.1f} 秒), パレット切替 {len(an.palsets)} 回')
        if pool:
            pool.close()
            pool.join()
            pool = None
        audio_total = sum(len(a) for a in audios)
        kf = keyframe_interval(s)
        r = Result()
        # プレビュー動画は、最後にデータを作るのと同じ回で書き出す (差分をもう1回計算しない)
        pw = None
        on_frame = None
        if s.preview_path:
            pa = None
            if s.audio_div:
                pa = np.concatenate([a[:len(a) - 2048] for a in audios])
            pw = media.PreviewWriter(s.preview_path, fps_expr(s.k, s.lines), pa,
                                     (s.audio_rate // s.audio_channels) or 12000, channels=s.audio_channels)

            def on_frame(an, f, disp):
                pw.write(tiles_to_frame(disp))

        if nile:
            # microSD から読む速さに合わせて、1コマあたりのデータ量を制限する
            tau = QUALITY_TAU['best'] if s.quality == 'auto' else QUALITY_TAU[s.quality]
            abytes = (s.audio_rate or 0) / s.fps
            rate = max(2000.0, s.sd_kbps * 1024 * 0.95 / s.fps - abytes)     # 5% の余裕を見る
            burst = NILE_PREBUF * BANK
            prog(0.86, 'データを作成中…')
            sw, starts, fstarts, stats = delta_pass(
                analyses, tau, maxt, write=True, cancel=cancel, align=True, rate=rate, burst=burst, kf=kf,
                dither=s.dither, fps_hint=s.fps, read_cost=NILE_READ_UNIT, on_frame=on_frame, weights=nile_weights(),
                all_raw=vmode_flags(s.video_mode)[0],
                progress=lambda c, t: prog(0.86 + 0.12 * c / t, f'データを作成中  {c}/{t} コマ'))
            prog(0.98, 'データファイルを書き出し中…')
            wsv = os.path.join(os.path.dirname(os.path.abspath(out_path)), wsv_name(out_path))
            wsv_size = write_wsv(wsv, sw, starts, fstarts, audios, [an.n for an in analyses], s, kf)
            rom = romlib.build_nile_rom(os.path.basename(wsv), s)
            with open(out_path, 'wb') as fp:
                fp.write(rom)
            r.rom_mb = len(rom) >> 20
            r.wsv_path = wsv
            r.wsv_size = wsv_size
            r.size = wsv_size
            r.limited = stats['limited']
        else:
            def total_size(tau):
                sw, _, _, _ = delta_pass(analyses, tau, maxt, cancel=cancel, kf=kf)
                return sw.pos + audio_total

            def fits(size):
                for mb in ROM_SIZES:
                    if mb <= s.max_rom_mb and size <= (mb * 16 - 1) * BANK:
                        return mb
                return None

            prog(0.87, '容量を計算中…')
            size = None
            if s.quality == 'auto':
                # 画質パラメータの候補をいくつか同時に (別プロセスで) 試し、入る中で一番きれいなものに絞り込む
                nproc = max(1, min(8, (os.cpu_count() or 2) - 1))
                spool = mp.get_context('spawn').Pool(nproc) if nproc > 1 else None
                descs = [an.desc() for an in analyses]

                def sizes(taus):
                    args = [(descs, t, maxt, kf) for t in taus]
                    res = spool.map(_size_worker, args) if spool else [_size_worker(x) for x in args]
                    return [sz + audio_total for sz in res]

                try:
                    lo, hi = QUALITY_TAU['best'], 1e6
                    best = None
                    for rnd in range(3):
                        prog(0.87 + 0.03 * rnd, '容量に合わせて画質を調整中…')
                        if cancel and cancel():
                            raise Cancelled()
                        taus = list(np.geomspace(lo, hi, nproc + 1 if rnd else max(nproc, 4)))
                        res = sizes(taus)
                        ok = [i for i, sz in enumerate(res) if fits(sz)]
                        if not ok:
                            if rnd == 0:
                                raise RuntimeError(f'ROM {s.max_rom_mb}MB に収まりません。{NO_FIT_HINT}')
                            break
                        i = ok[0]
                        best = (taus[i], res[i])
                        if i == 0:
                            break
                        lo, hi = taus[i - 1], taus[i]
                finally:
                    if spool:
                        spool.close()
                        spool.join()
                tau, size = best
            else:
                tau = QUALITY_TAU[s.quality]
            if size is None:
                size = total_size(tau)
            mb = fits(size)
            if mb is None:
                raise RuntimeError(
                    f'ROM {s.max_rom_mb}MB に収まりません(必要: 約{size / 1048576:.1f}MB)。画質を「自動」にしてください。'
                    f'{NO_FIT_HINT}')
            prog(0.93, 'データを作成中…')
            sw, starts, fstarts, stats = delta_pass(
                analyses, tau, maxt, write=True, cancel=cancel, kf=kf, on_frame=on_frame,
                progress=lambda c, t: prog(0.93 + 0.05 * c / t, f'データを作成中  {c}/{t} コマ'))
            prog(0.98, 'ROM を書き出し中…')
            data = sw.buf
            astarts = []
            for a in audios:
                astarts.append(len(data))
                data += a.tobytes()
            seeks = []
            for i, an in enumerate(analyses):
                lst = []
                for f in range(0, an.n, kf) if kf else []:
                    ap = astarts[i] + (f * s.k * s.lines // s.audio_div if s.audio_div else 0)
                    lst.append((sw.frame_pos[fstarts[i] + f], ap))
                seeks.append(lst)
            rom = romlib.build_rom(data, [(vs, ast, an.n) for vs, ast, an in zip(starts, astarts, analyses)],
                                   s, mb, seeks=seeks, kf=kf)
            with open(out_path, 'wb') as fp:
                fp.write(rom)
            r.rom_mb = mb
            r.size = size
            r.wsv_path = None
        if pw:
            prog(0.99, 'プレビュー動画を仕上げ中…')
            pw.close()
            pw = None
        r.tau = tau
        r.frames = stats['frames']
        r.seconds = stats['frames'] / s.fps
        r.avg_tiles = stats['tiles'] / max(1, stats['frames'])
        r.palframes = stats['palframes']
        r.video_bytes = sw.pos
        r.audio_bytes = audio_total
        r.elapsed = time.time() - t0
        prog(1.0, '完了')
        return r
    finally:
        if pool:
            pool.terminate()
        if pw:
            try:
                pw.close()
            except Exception:
                pass
        if cache:
            # 今回使った解析結果だけ残す (中止した途中のファイルは消える)
            cache.prune(keys, akeys)
        else:
            for an in analyses:
                an.close()
            try:
                for fn in os.listdir(tmpdir):
                    os.unlink(os.path.join(tmpdir, fn))
                os.rmdir(tmpdir)
            except OSError:
                pass
