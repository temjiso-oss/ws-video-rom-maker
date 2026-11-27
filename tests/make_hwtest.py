"""実機の確認を 1 回で済ませるための一式を作る (1.5.0 の開発、2026-11-25)。

    python -X utf8 tests/make_hwtest.py 動画 出力フォルダー [名前 ...]

同じ 2 つの場面 (激しい場面・細かい背景の場面) を、設定を変えて何通りか作る。
ROM は試し用のプレイヤー (-DSTATS) で作るので、動画が終わるたびに
「コマ数・遅れたコマ・読み込み待ち・音ズレ補正」の表が出る (A で次へ)。
ROM は 1MB の .wsc、データは .WSV (microSD のルートに置く)。動画は手元の確認用 (配らない)。
"""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from wsvideo import encoder as E, rom  # noqa: E402

BUDGET0 = E.NILE_BUDGET_SCALE
NASM = ROOT / 'tools' / 'nasm' / 'nasm.exe'
PSRC = ROOT / 'src' / 'player'

# 場面 (開始秒, 長さ秒, 名前)。標準は JISO が変換したアニメの第 1 話 (変身シーン 17:29〜、エンディング 22:10〜)
# 2026-11-25 (hwtest-1.5b): 横パンの多い場面 (14:10〜30 秒、森の背景が流れる) を足した。
# 位相相関で探した (横ずれ 3 画素以上のコマが一番多い 30 秒)。
SCENES = [(1049, 120, 'henshin'), (1330, 100, 'ending'), (850, 30, 'pan')]

# 名前: (K, 60Hz か, 展開の上限 MAX_UNITS[K] を変えるなら, 説明)
VARIANTS = {
    'Q20': (3, True, None, '20fps (今の標準)。画質の改善の確認'),
    'F30A': (2, True, 220, '30fps・展開の上限 220 (控えめ)'),
    'F30S': (2, True, 180, '30fps・展開の上限 180 (安全)'),
    # hwtest-1.5b: 30fps で 1コマの予算 (NILE_BUDGET_SCALE) を 1.3 倍。mesem で 1.5 倍は LATE 3、2.0 倍は LATE 2339
    'F30C': (2, True, 220, '30fps・上限 220・予算 1.3 倍', 1.3),
    # hwtest-1.6 (2026-11-26): SD の読み込みを速くした (データを受ける間に前のセクタを写す) ので、30fps は 500KB/秒にする。
    # 6 つ目 = 読み込みの上限 (KB/秒)
    'F30N': (2, True, 220, '30fps・500KB/秒 (読み込みを速くした版)', None, 500),
    # パラパラ漫画の方式 (全部のコマを全面描き直し、差分なし)。7 つ目 = 'raw'
    'P20': (3, True, None, '20fps・全部描き直し (約 440KB/秒)', None, 800, 'raw'),
    'P30': (2, True, 220, '30fps・全部描き直し (約 660KB/秒)', None, 800, 'raw'),
    # 'rawpal': パラパラ + コマごとにパレットを作り直す (誤差が 1 割以上減るとき替える)
    'P30X': (2, True, 220, '30fps・全部描き直し・コマごとのパレット', None, 800, 'rawpal'),
    # 'raster': P30X + ラスター (16 ラインずつ 9 本の帯、帯ごとに 8 パレット)
    'P30R': (2, True, 220, '30fps・全部描き直し・コマごとのパレット・ラスター', None, 900, 'raster'),
    'P20R': (3, True, None, '20fps・全部描き直し・コマごとのパレット・ラスター', None, 900, 'raster'),
    # 8 つ目 = 音 ('hi' 24kHz モノラル / 'stereo' 12kHz ステレオ)。映像は P30R と同じ
    'S30H': (2, True, 220, 'P30R + 音 24kHz モノラル', None, 900, 'raster', 'hi'),
    'S30S': (2, True, 220, 'P30R + 音 ステレオ (ヘッドホン・TV スワン)', None, 900, 'raster', 'stereo'),
    # 9 つ目 = 時間方向のディザの強さ (1 コマおきに網目を反転。WSVIDEO_TDITHER)。映像は P30R と同じ
    'P30T': (2, True, 220, 'P30R + 時間方向のディザ (網目を 1 コマおきに反転)', None, 900, 'raster', 'mono', 1.0),
    # 8 ラインの帯・144 パレット (2026-11-26 深夜)。帯の形は encoder を読み込む時に決まるので、
    # $env:WSVIDEO_BAND='8x8' にしてから、この名前だけで動かす
    'P30W': (2, True, 220, 'P30R の帯を 8 ライン x 18 本・144 パレットに', None, 900, 'raster'),
    # 色の近さを OKLab (色みの重み 2) で測る (2026-11-27)。$env:WSVIDEO_SPACE='oklab'; $env:WSVIDEO_OKC='2' にして、
    # 名前が K で終わる物だけで動かす
    'P30K': (2, True, 220, 'P30R の色の選び方を OKLab に', None, 900, 'raster'),
    'Q20K': (3, True, None, 'Q20 の色の選び方を OKLab に'),
    # 'F30B' (上限 280) は mesem でも 60 コマ遅れたので外した
    'F25': (3, False, None, '25fps (75Hz、60Hz にしない)。純正の液晶でも映るはず'),
    'N19': (4, False, None, '18.9fps (75Hz)。アプリの「純正の液晶」の標準'),
    'R15': (5, False, None, 'ROM に入れる版 (15fps・16MB)。nileswan のメニューから普通の ROM として起動'),
}


def stats_player():
    with tempfile.TemporaryDirectory() as td:
        binp = Path(td) / 'p.bin'
        subprocess.run([str(NASM), '-f', 'bin', '-I', str(PSRC) + '/', '-DNILE', '-DSTATS',
                        str(PSRC / 'player.asm'), '-o', str(binp)], check=True)
        return binp.read_bytes()


def _rom(pbin, wsv, s):
    orig = rom.player_bin
    rom.player_bin = lambda n: pbin
    try:
        return rom.build_nile_rom(wsv, s)
    finally:
        rom.player_bin = orig


def main():
    video = sys.argv[1]
    out = Path(sys.argv[2])
    names = sys.argv[3:] or list(VARIANTS)
    out.mkdir(parents=True, exist_ok=True)
    pbin = stats_player()
    for name in names:
        if (name == 'P30W') != (E.BAND_KIND == '8x8'):
            sys.exit(f'{name}: WSVIDEO_BAND は P30W のときだけ 8x8 にする (今は {E.BAND_KIND})')
        if name.endswith('K') and E.COLOR_SPACE != 'oklab':
            sys.exit(f'{name}: WSVIDEO_SPACE を oklab にする (今は {E.COLOR_SPACE})')
        k, hz60, units, desc = VARIANTS[name][:4]
        # 5 つ目があれば、その動画だけ 1コマの予算の倍率を変える (なければ環境変数 WSVIDEO_NILEBUDGET のまま)
        E.NILE_BUDGET_SCALE = VARIANTS[name][4] if len(VARIANTS[name]) > 4 and VARIANTS[name][4] else BUDGET0
        target = 'rom' if name.startswith('R') else 'nile'
        sd = int(os.environ.get('HWTEST_SD', '350'))     # 試す用: 読み込みの上限を変える
        if len(VARIANTS[name]) > 5:
            sd = VARIANTS[name][5]
        # 7 つ目 = 映像の方式 (Settings.video_mode: 'raw' / 'rawpal' / 'raster'。なければ 'delta')
        vmode = VARIANTS[name][6] if len(VARIANTS[name]) > 6 else 'delta'
        am = VARIANTS[name][7] if len(VARIANTS[name]) > 7 else 'mono'
        s = E.Settings(k=k, target=target, hz60=hz60, sd_kbps=sd, dither=1.0, saturation=1.2, max_rom_mb=16, audio_mode=am,
                       video_mode=vmode)
        if target == 'rom':
            if video == '--rom-only':
                continue
            # ROM に入れる版: 普通のプレイヤーのまま (表は出ない)。激しい場面 1 つだけ
            t0 = time.time()
            st, ln, title = SCENES[0]
            r = E.convert([E.Item(video, st, ln, title)], s, str(out / f'{name}.wsc'))
            print(f'{name}: {desc}  {s.fps:.2f}fps  {r.rom_mb}MB  平均 {r.avg_tiles:.0f} タイル  {time.time() - t0:.0f} 秒')
            continue
        if video == '--rom-only':
            # プレイヤーだけ直したとき: 変換はせず、ROM だけ作り直す
            path = out / f'{name}.wsc'
            path.write_bytes(_rom(pbin, E.wsv_name(str(path)), s))
            print(name, 'ROM だけ作り直した')
            continue
        old = E.MAX_UNITS.get(k)
        if units:
            E.MAX_UNITS[k] = units
        td = VARIANTS[name][8] if len(VARIANTS[name]) > 8 else 0
        E.TDITHER = td                                  # 並列の解析の子プロセスには環境変数で渡す
        os.environ['WSVIDEO_TDITHER'] = str(td)
        t0 = time.time()
        try:
            items = [E.Item(video, st, ln, title) for st, ln, title in SCENES]
            path = out / f'{name}.wsc'
            r = E.convert(items, s, str(path))
        finally:
            E.MAX_UNITS[k] = old
            E.TDITHER = 0
            os.environ.pop('WSVIDEO_TDITHER', None)
        path.write_bytes(_rom(pbin, Path(r.wsv_path).name, s))
        print(f'{name}: {desc}  {s.fps:.2f}fps  {Path(r.wsv_path).name} {r.wsv_size / 1048576:.1f}MB  '
              f'平均 {r.avg_tiles:.0f} タイル  上限で削ったコマ {r.limited}  {time.time() - t0:.0f} 秒')


if __name__ == '__main__':
    main()
