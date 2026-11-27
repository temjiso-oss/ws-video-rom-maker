"""WS Video ROM Maker の画面 (1.5.0〜、HTML + pywebview)。

画面は web/ フォルダの index.html・app.css・app.js。ここ (Python) は、画面から呼ばれる窓口 (Api) と、
変換を別スレッドで動かして進み具合を覚えておく役。画面は poll() を 0.3 秒ごとに呼んで表示を更新する。
pywebview が使えないときは、前の画面 (gui.py、Tkinter) を使う。
"""
import base64
import copy
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import __version__, emuimg, encoder, media
from .gui import (AUDIO_CHOICES, DITHER_CHOICES, MODE_CHOICES, PAL_CHOICES, QUALITY_CHOICES, ROM_CHOICES,
                  SPEAKER_CHOICES, fmt_time, parse_time, settings_path, short_stem, split_outputs)

APP = 'WS Video ROM Maker'
VIDEO_EXTS = ('mp4', 'm4v', 'mkv', 'avi', 'mov', 'wmv', 'webm', 'flv', 'mpg', 'mpeg', 'ts', 'm2ts', '3gp', 'ogv',
              'vob', 'gif', 'asf', 'divx', 'rm', 'rmvb')

# 標準の設定。使い方 (プリセット) ごとの値は PRESETS
DEFAULTS = dict(target='nile', k=3, hz60=True, audio=1, rom=16, sd=350, speaker=1, quality='auto', mode='fit',
                dither=1.0, bright=0.0, contrast=1.0, sat=1.2, norm=True, deint=False, preview=False, pal16=True,
                vmode='delta', amode='mono', emu=False)   # emu = エミュレーター用の microSD のイメージも作る (1.6.0〜)
PRESETS = {
    # 20.1fps・350KB/秒 (2026-11-25、TV スワン・IPS 改造の実機で変身シーンを確認)
    'nile60': dict(target='nile', hz60=True, k=3, sd=350, vmode='delta'),
    # 30fps・パラパラ漫画の方式・コマごとのパレット・ラスター (2026-11-26、mesem-mod で確認。2026-11-27 に TV スワンの実機で遅れ 0)
    'nile60hq': dict(target='nile', hz60=True, k=2, sd=900, vmode='raster'),
    # 18.9fps (75Hz。1コマの時間は 60Hz・K3 より長い。実機は未確認)
    'nile': dict(target='nile', hz60=False, k=4, sd=350, vmode='delta'),
    'rom': dict(target='rom', hz60=False, k=5, vmode='delta'),
}
PRESET_INFO = [
    dict(id='nile60', title='nileswan・60Hz の本体', sub='IPS 液晶化・TV スワンなど（20fps）', badge='おすすめ'),
    # 「試験的」は外した (2026-11-27、TV スワン＋nileswan の実機で 30fps が遅れ 0 だったため)
    dict(id='nile60hq', title='nileswan・60Hz・高画質', sub='30fps・毎コマ描き直し・ラスター（データが多め）'),
    dict(id='nile', title='nileswan・純正の液晶', sub='改造していない本体'),
    dict(id='rom', title='ROM に入れる', sub='ほかのフラッシュカート（最大 16MB）'),
]
SD_CHOICES = [(350, '350 KB/秒（標準・実機で確認済み）'), (250, '250 KB/秒（控えめ）'), (160, '160 KB/秒'),
              (450, '450 KB/秒（試験的）'), (900, '900 KB/秒（高画質 30fps 用）')]
# 映像の方式 (nileswan 版)。Settings.video_mode
VMODE_CHOICES = [('delta', '差分（標準）'), ('raw', 'パラパラ'), ('rawpal', 'パラパラ＋色'),
                 ('raster', 'パラパラ＋色＋ラスター（最高画質）')]
# 音の形式 (nileswan 版)。Settings.audio_mode
AMODE_CHOICES = [('mono', 'モノラル 12kHz（標準）'), ('hi', 'モノラル 24kHz'), ('stereo', 'ステレオ 12kHz')]
# 1 コマのデータの大きさ (全部描き直しのとき、バイト): 先頭 + パレット + マップ・タイル
RAW_FRAME_BYTES = {'raw': 1 + 512 + 17136, 'rawpal': 1 + 512 + 17136, 'raster': 1 + 2304 + 17136}
# 変換の重さ (「変わった所だけ」を 1 とした目安。コマごとにパレットを作り直すと重い。2026-11-26 に測った)
VMODE_COST = {'delta': 1.0, 'raw': 0.7, 'rawpal': 2.0, 'raster': 3.0}
FPS_KS = [2, 3, 4, 5, 6, 8, 10]
STAGES = ['動画を読む', '色と動きを調べる', 'データを作る', '書き出す']

# 変換の速さの見込み (1 コマあたりの秒)。変換するたびに実際の値で更新して覚えておく
SPEED_DEFAULT = {'gpu': 0.0075, 'cpu': 0.042}
JOB_OVERHEAD = 6.0          # 1 本ごとの準備 (秒)


def _choices(lst):
    return [dict(value=v, label=t) for v, t in lst]


def fps_of(k, hz60):
    return 12000 / ((199 if hz60 else 159) * k)


class Api:
    def __init__(self):
        self._window = None
        self._lock = threading.RLock()
        self.items = []             # dict(id, path, title, duration, desc, start, length, status, frac, result, error)
        self.next_id = 1
        self.thumbs = {}
        self.state = dict(DEFAULTS)
        self.combine = False        # True = 全部を 1 つの ROM にまとめる (標準は動画ごとに別の ROM)
        self.outdir = ''
        self.speed = dict(SPEED_DEFAULT)
        self._cache = encoder.AnalysisCache()
        self.job = None             # 変換中の情報
        self.log = []
        self.load_settings()

    # ---- 設定 ----------------------------------------------------------
    def load_settings(self):
        try:
            d = json.loads(settings_path().read_text(encoding='utf-8'))
        except Exception:
            return
        st = d.get('web_state') or {}
        self.state.update({k: v for k, v in st.items() if k in DEFAULTS})
        self.outdir = d.get('outdir', '') or ''
        self.combine = bool(d.get('combine', False))
        sp = d.get('speed') or {}
        for k in SPEED_DEFAULT:
            if isinstance(sp.get(k), (int, float)) and sp[k] > 0:
                self.speed[k] = float(sp[k])

    def save_settings(self):
        try:
            p = settings_path()
            try:
                d = json.loads(p.read_text(encoding='utf-8'))
            except Exception:
                d = {}
            d.update(web_state=self.state, outdir=self.outdir, combine=self.combine, speed=self.speed)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding='utf-8')
        except Exception:
            pass

    def _vmode(self):
        """今の映像の方式 (ROM に入れる版は差分だけ)"""
        return self.state['vmode'] if self.state['target'] == 'nile' else 'delta'

    def preset_name(self):
        for name, pv in PRESETS.items():
            if all(self.state.get(k) == v for k, v in pv.items()):
                return name
        return 'custom'

    # ---- 画面に渡す情報 ---------------------------------------------------
    def init(self):
        hz60 = self.state['hz60']
        return dict(
            version=__version__, gpu=encoder.gpu_name(), presets=PRESET_INFO, stages=STAGES,
            choices=dict(
                fps=[dict(value=k, label=f'{fps_of(k, False):.1f} fps', label60=f'{fps_of(k, True):.1f} fps')
                     for k in FPS_KS],
                audio=_choices(AUDIO_CHOICES), rom=_choices(ROM_CHOICES), quality=_choices(QUALITY_CHOICES),
                mode=_choices(MODE_CHOICES), speaker=_choices(SPEAKER_CHOICES),
                pal16=[dict(value=bool(v), label=t) for v, t in PAL_CHOICES], dither=_choices(DITHER_CHOICES),
                sd=_choices(SD_CHOICES), vmode=_choices(VMODE_CHOICES), amode=_choices(AMODE_CHOICES)),
            view=self.view(), hz60=hz60)

    def view(self):
        """画面の今の状態 (動画の一覧・設定・見込み)"""
        with self._lock:
            return dict(items=[self._item_view(it) for it in self.items], state=dict(self.state),
                        preset=self.preset_name(), combine=self.combine, outdir=self.outdir,
                        estimate=self.estimate(), running=bool(self.job and self.job.get('running')))

    def _item_view(self, it):
        secs = self._item_seconds(it)
        return dict(id=it['id'], title=it['title'], desc=it['desc'], duration=fmt_time(it['duration']),
                    start=fmt_time(it['start']) if it['start'] else '',
                    length=fmt_time(it['length']) if it['length'] else '',
                    trim=self._trim_text(it), seconds=fmt_time(secs), status=it['status'],
                    frac=round(it.get('frac', 0.0), 4), error=it.get('error'), result=it.get('result'),
                    thumb=self.thumbs.get(it['id']))

    def _trim_text(self, it):
        if not it['start'] and not it['length']:
            return '全部'
        a = fmt_time(it['start'])
        b = fmt_time(it['start'] + it['length']) if it['length'] else '最後'
        return f'{a} 〜 {b}'

    def _item_seconds(self, it):
        d = max(0.0, (it['duration'] or 0) - it['start'])
        return min(d, it['length']) if it['length'] else d

    def estimate(self):
        st = self.state
        tot = sum(self._item_seconds(it) for it in self.items)
        fps = fps_of(st['k'], st['hz60'])
        frames = tot * fps
        dev = 'gpu' if encoder.gpu_name() else 'cpu'
        njobs = 1 if (self.combine or not self.items) else len(self.items)
        vmode = self._vmode()
        eta = frames * self.speed[dev] * VMODE_COST.get(vmode, 1.0) + JOB_OVERHEAD * njobs if self.items else 0
        if st['target'] == 'rom':
            eta *= 1.6          # ROM は容量に合わせて画質を探すので長い
        bpl = 2 if (st['target'] == 'nile' and st['amode'] in ('hi', 'stereo')) else 1
        audio_b = tot * (12000 * bpl / st['audio']) if st['audio'] else 0
        if st['target'] == 'nile':
            if vmode in RAW_FRAME_BYTES:
                # 全部描き直し: 1 コマの大きさは決まっている
                mb = (frames * RAW_FRAME_BYTES[vmode] + audio_b) / 1048576
                note = '毎コマ全部描き直すので、ほぼこの量'
            else:
                # 激しいアニメで読み込みの上限の 7 割ほど (変身シーン 20fps で約 257KB/秒) を見込む
                mb = (tot * st['sd'] * 1024 * 0.7 + audio_b) / 1048576
                note = '激しい動画での目安'
            data = dict(label='microSD に置く量', value=f'約 {mb:,.0f} MB' if mb >= 1 else '1 MB 未満', note=note)
        else:
            cap = (st['rom'] * 16 - 1) * 65536
            hi = (frames * 6000 + audio_b) / cap
            lo = (frames * 2000 + audio_b) / cap
            data = dict(label='ROM の使用量', value=f'{lo * 100:.0f}〜{hi * 100:.0f}%',
                        note='入りきらない見込み' if lo > 1 else f'上限 {st["rom"]} MB', warn=lo > 1)
        return dict(seconds=fmt_time(tot), frames=int(frames), fps=f'{fps:.1f}', hz='60Hz' if st['hz60'] else '75Hz',
                    data=data, eta=fmt_dur(eta), device=encoder.gpu_name() or 'CPU', count=len(self.items))

    # ---- 動画の一覧 -----------------------------------------------------
    def pick_files(self):
        import webview
        types = ('動画ファイル (' + ';'.join('*.' + e for e in VIDEO_EXTS) + ')', 'すべてのファイル (*.*)')
        r = self._window.create_file_dialog(webview.FileDialog.OPEN, allow_multiple=True, file_types=types)
        if not r:
            return dict(view=self.view(), bad=[])
        return self.add_paths(list(r))

    def add_paths(self, paths):
        bad = []
        added = []
        for p in paths:
            if not p or not os.path.isfile(p):
                continue
            try:
                inf = media.probe(p)
            except Exception as ex:
                bad.append(f'{os.path.basename(p)}: {ex}')
                continue
            if not inf.has_video:
                bad.append(f'{os.path.basename(p)}: 映像がありません')
                continue
            with self._lock:
                it = dict(id=self.next_id, path=p, title=os.path.basename(p), duration=inf.duration,
                          desc=inf.desc + ('' if inf.has_audio else '（音声なし）'), start=0.0, length=None,
                          status='wait', frac=0.0)
                self.next_id += 1
                self.items.append(it)
                added.append(it)
            if not self.outdir:
                self.outdir = str(Path(p).parent)
        for it in added:
            threading.Thread(target=self._make_thumb, args=(it,), daemon=True).start()
        return dict(view=self.view(), bad=bad)

    def _make_thumb(self, it):
        try:
            t = max(0.0, it['start'] + (self._item_seconds(it) or 0) * 0.3)
            cmd = [media.ffmpeg_exe(), '-v', 'error', '-ss', f'{t:.2f}', '-i', it['path'], '-frames:v', '1',
                   '-vf', 'scale=160:-2', '-f', 'image2', '-c:v', 'mjpeg', '-q:v', '5', '-']
            kw = dict(capture_output=True, timeout=30)
            if os.name == 'nt':
                kw['creationflags'] = 0x08000000      # 黒い窓を出さない
            d = subprocess.run(cmd, **kw).stdout
            if d:
                self.thumbs[it['id']] = 'data:image/jpeg;base64,' + base64.b64encode(d).decode()
                self._push()
        except Exception:
            pass

    def _push(self):
        """画面に「一覧を読み直して」と伝える"""
        try:
            if self._window:
                self._window.evaluate_js('window.app && app.refresh()')
        except Exception:
            pass

    def _find(self, iid):
        for i, it in enumerate(self.items):
            if it['id'] == iid:
                return i
        return None

    def remove(self, iid):
        with self._lock:
            if self._busy():
                return self.view()
            i = self._find(iid)
            if i is not None:
                del self.items[i]
        return self.view()

    def clear(self):
        with self._lock:
            if not self._busy():
                self.items = []
        return self.view()

    def move(self, iid, d):
        with self._lock:
            i = self._find(iid)
            if i is not None and 0 <= i + d < len(self.items) and not self._busy():
                self.items[i], self.items[i + d] = self.items[i + d], self.items[i]
        return self.view()

    def set_trim(self, iid, start, end):
        """start / end は '1:30' や '90'。end が空なら最後まで"""
        try:
            s = parse_time(start or '') or 0.0
            e = parse_time(end or '')
        except ValueError:
            return dict(error='時間は 90 や 1:30 のように入力してください。', view=self.view())
        with self._lock:
            i = self._find(iid)
            if i is None:
                return dict(view=self.view())
            it = self.items[i]
            dur = it['duration'] or 0
            if dur and s >= dur:
                return dict(error=f'始まりが動画の長さ（{fmt_time(dur)}）を超えています。', view=self.view())
            if e is not None and e <= s:
                return dict(error='終わりは始まりより後にしてください。', view=self.view())
            it['start'] = s
            it['length'] = (e - s) if e is not None else None
        return dict(view=self.view())

    # ---- 設定 -------------------------------------------------------------
    def set_preset(self, name):
        with self._lock:
            if name in PRESETS:
                keep = {k: self.state[k] for k in ('bright', 'contrast', 'sat', 'norm', 'deint', 'preview', 'emu', 'speaker',
                                                   'mode', 'dither', 'audio', 'pal16', 'amode')}
                self.state = dict(DEFAULTS)
                self.state.update(keep)
                self.state.update(PRESETS[name])
                self.save_settings()
        return self.view()

    def set_state(self, changes):
        with self._lock:
            for k, v in (changes or {}).items():
                if k in DEFAULTS:
                    self.state[k] = type(DEFAULTS[k])(v) if not isinstance(DEFAULTS[k], bool) else bool(v)
            self.save_settings()
        return self.view()

    def reset_state(self):
        with self._lock:
            self.state = dict(DEFAULTS)
            self.save_settings()
        return self.view()

    def set_combine(self, on):
        self.combine = bool(on)
        self.save_settings()
        return self.view()

    def choose_outdir(self):
        import webview
        r = self._window.create_file_dialog(webview.FileDialog.FOLDER, directory=self.outdir or '')
        if r:
            self.outdir = r[0] if isinstance(r, (list, tuple)) else r
            self.save_settings()
        return self.view()

    def open_folder(self):
        d = self.outdir
        if d and Path(d).is_dir():
            subprocess.Popen(['explorer', str(Path(d))])

    def open_file_location(self, path):
        if path and Path(path).exists():
            subprocess.Popen(['explorer', '/select,', str(Path(path))])

    def about(self):
        return dict(app=APP, version=__version__, gpu=encoder.gpu_name())

    # ---- 変換 ---------------------------------------------------------------
    def _busy(self):
        return bool(self.job and self.job.get('running'))

    def _settings(self, preview_path=None):
        st = self.state
        return encoder.Settings(
            k=st['k'], audio_div=st['audio'], max_rom_mb=st['rom'], quality=st['quality'], mode=st['mode'],
            dither=st['dither'], brightness=round(st['bright'], 3), contrast=round(st['contrast'], 3),
            saturation=round(st['sat'], 3), normalize=st['norm'], deinterlace=st['deint'],
            speaker_shift=st['speaker'], target=st['target'], sd_kbps=st['sd'], hz60=st['hz60'],
            pal16=bool(st['pal16']), preview_path=preview_path, video_mode=st['vmode'], audio_mode=st['amode'])

    def plan(self):
        """変換する組 (動画たち, 保存先) の一覧"""
        outdir = Path(self.outdir or (Path(self.items[0]['path']).parent if self.items else '.'))
        if self.combine or len(self.items) == 1:
            name = short_stem(Path(self.items[0]['path']).stem) + '.wsc'
            return [([it['id'] for it in self.items], str(outdir / name))]
        outs = split_outputs([it['path'] for it in self.items], outdir)
        return [([it['id']], o) for it, o in zip(self.items, outs)]

    def check_start(self):
        """始める前の確認。戻り値: dict(ok, error, confirm=[上書きするファイル], rom_problem)"""
        with self._lock:
            if not self.items:
                return dict(ok=False, error='動画を追加してください。')
            if self._busy():
                return dict(ok=False, error='変換中です。')
            if not self.outdir or not Path(self.outdir).is_dir():
                return dict(ok=False, error='保存先のフォルダを選んでください。')
            jobs = self.plan()
            exist = [Path(o).name for _, o in jobs if Path(o).exists()]
            rom_problem = None
            if self.state['target'] == 'rom':
                s = self._settings()
                for ids, _o in jobs:
                    secs = [self._item_seconds(self.items[self._find(i)]) for i in ids]
                    msg = encoder.rom_fit_precheck(secs, s)
                    if msg:
                        rom_problem = msg.split('\n')[0]
                        break
            return dict(ok=True, confirm=exist, rom_problem=rom_problem)

    def start(self, switch_to_nile=False):
        with self._lock:
            if not self.items or self._busy():
                return dict(ok=False)
            if switch_to_nile:
                self.state.update(PRESETS['nile60'])
            jobs = self.plan()
            for it in self.items:
                it.update(status='wait', frac=0.0, result=None, error=None)
            frames = []
            for ids, _o in jobs:
                frames.append(sum(self._item_seconds(self.items[self._find(i)]) for i in ids)
                              * fps_of(self.state['k'], self.state['hz60']))
            self.job = dict(running=True, cancel=False, t0=time.time(), jobs=jobs, index=0, frac=0.0, msg='準備中…',
                            stage=0, frames=frames, done_frames=0.0, finished=False, cancelled=False, error=None,
                            results=[])
            self.log = []
            self.save_settings()
        threading.Thread(target=self._work, daemon=True).start()
        return dict(ok=True)

    def cancel(self):
        if self.job:
            self.job['cancel'] = True
            self.job['msg'] = '中止しています…'

    def _say(self, m):
        with self._lock:
            self.log.append(m)

    def _work(self):
        job = self.job
        dev = 'gpu' if encoder.gpu_name() else 'cpu'
        try:
            n = len(job['jobs'])
            for j, (ids, out) in enumerate(job['jobs']):
                with self._lock:
                    job['index'] = j
                    job['stage'] = 0
                    job['frac_job'] = 0.0
                    its = [self.items[self._find(i)] for i in ids]
                    for it in its:
                        it['status'] = 'run'
                preview = str(Path(out).with_name(Path(out).stem + '_preview.mp4')) if self.state['preview'] else None
                s = self._settings(preview)
                items = [encoder.Item(it['path'], it['start'], it['length'], it['title']) for it in its]
                self._say(f'—— {j + 1} / {n}: {Path(out).name} ——')
                t1 = time.time()

                def prog(f, m, its=its):
                    with self._lock:
                        job['frac_job'] = f
                        job['msg'] = m
                        if '解析' in m:
                            job['stage'] = 1
                        elif 'データを作成' in m or '容量' in m or '画質を調整' in m:
                            job['stage'] = 2
                        elif '書き出し' in m or '仕上げ' in m or m == '完了':
                            job['stage'] = 3
                        for it in its:
                            it['frac'] = f

                r = encoder.convert(items, s, out, prog, self._say, lambda: job['cancel'], cache=self._cache)
                el = time.time() - t1
                if r.frames >= 1500:           # 短い動画は準備の時間が大きく、速さの見込みが狂うので覚えない
                    # 覚えるのは「変わった所だけ」の方式に換算した速さ (方式ごとの重さ VMODE_COST で割る)
                    sp = max(0.0005, (el - JOB_OVERHEAD) / r.frames / VMODE_COST.get(self._vmode(), 1.0))
                    self.speed[dev] = self.speed[dev] * 0.5 + sp * 0.5
                res = dict(out=out, name=Path(out).name, wsv=Path(r.wsv_path).name if r.wsv_path else None,
                           wsv_path=r.wsv_path, wsv_mb=round((r.wsv_size or 0) / 1048576),
                           rom_mb=r.rom_mb, frames=r.frames, seconds=fmt_time(r.seconds), fps=f'{s.fps:.1f}',
                           tiles=round(r.avg_tiles), limited=r.limited, elapsed=fmt_dur(el), preview=preview)
                if self.state.get('emu') and r.wsv_path:
                    # エミュレーター用の microSD のイメージ。作れなくても変換そのものは成功にする
                    with self._lock:
                        job['msg'] = 'エミュレーター用のイメージを作っています…'
                    try:
                        folder = emuimg.make_for(out, r.wsv_path)
                        res.update(emu=str(folder), emu_name=folder.name)
                        self._say(f'エミュレーター用: {folder}')
                    except Exception as ex:
                        res['emu_error'] = str(ex)
                        self._say('エミュレーター用のイメージは作れませんでした: ' + str(ex))
                with self._lock:
                    for it in its:
                        it.update(status='done', frac=1.0, result=res)
                    job['results'].append(res)
                    job['done_frames'] += job['frames'][j]
                    job['frac_job'] = 0.0
        except encoder.Cancelled:
            with self._lock:
                job['cancelled'] = True
                for it in self.items:
                    if it['status'] == 'run':
                        it['status'] = 'cancel'
        except Exception as ex:
            with self._lock:
                job['error'] = str(ex)
                for it in self.items:
                    if it['status'] == 'run':
                        it.update(status='error', error=str(ex))
            self._say('エラー: ' + str(ex))
        finally:
            with self._lock:
                job['running'] = False
                job['finished'] = True
                job['elapsed_total'] = time.time() - job['t0']
            self.save_settings()
            self._push()

    def poll(self, log_from=0):
        """変換中の様子"""
        with self._lock:
            job = self.job
            items = [dict(id=it['id'], status=it['status'], frac=round(it.get('frac', 0.0), 4)) for it in self.items]
            if not job:
                return dict(job=None, items=items, log=self.log[log_from:], log_len=len(self.log))
            tot = sum(job['frames']) or 1
            j = job['index']
            cur_frames = job['frames'][j] if j < len(job['frames']) else 0
            frac = min(1.0, (job['done_frames'] + cur_frames * job.get('frac_job', 0.0)) / tot)
            el = (job.get('elapsed_total') if job['finished'] else time.time() - job['t0'])
            dev = 'gpu' if encoder.gpu_name() else 'cpu'
            remain_frames = tot - job['done_frames'] - cur_frames * job.get('frac_job', 0.0)
            est_left = (remain_frames * self.speed[dev] * VMODE_COST.get(self._vmode(), 1.0)
                        + JOB_OVERHEAD * max(0, len(job['jobs']) - j - 1))
            if frac > 0.08:
                left = el / frac - el
                left = 0.6 * left + 0.4 * est_left
            else:
                left = est_left
            cur_title = ''
            if j < len(job['jobs']):
                ids = job['jobs'][j][0]
                k = self._find(ids[0])
                if k is not None:
                    cur_title = self.items[k]['title'] + (f' ほか {len(ids) - 1} 本' if len(ids) > 1 else '')
            return dict(job=dict(running=job['running'], finished=job['finished'], cancelled=job['cancelled'],
                                 error=job['error'], frac=frac, stage=job['stage'], msg=job['msg'],
                                 index=j + 1, count=len(job['jobs']), title=cur_title, elapsed=fmt_dur(el),
                                 left=fmt_dur(left) if job['running'] else '', results=job['results']),
                        items=items, log=self.log[log_from:], log_len=len(self.log))

    def close(self):
        if self.job and self.job.get('running'):
            self.job['cancel'] = True
        self.save_settings()
        try:
            self._cache.close()
        except Exception:
            pass


def fmt_dur(sec):
    sec = int(round(max(0, sec or 0)))
    if sec < 60:
        return f'{sec} 秒'
    if sec < 3600:
        return f'{sec // 60} 分 {sec % 60:02d} 秒'
    return f'{sec // 3600} 時間 {sec % 3600 // 60:02d} 分'


def web_dir():
    base = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent.parent))
    for p in (base / 'wsvideo' / 'web', Path(__file__).resolve().parent / 'web'):
        if (p / 'index.html').exists():
            return p
    raise FileNotFoundError('web/index.html が見つかりません')


def main():
    import webview
    from webview.dom import DOMEventHandler
    api = Api()
    win = webview.create_window(f'{APP} {__version__}', url=str(web_dir() / 'index.html'), js_api=api,
                                width=1180, height=820, min_size=(980, 700), background_color='#F7F7F5')
    api._window = win

    def on_drop(e):
        files = (e.get('dataTransfer') or {}).get('files') or []
        paths = [f.get('pywebviewFullPath') for f in files if f.get('pywebviewFullPath')]
        exts = tuple('.' + x for x in VIDEO_EXTS)
        if paths:
            r = api.add_paths([p for p in paths if p.lower().endswith(exts)] or paths)
            if r.get('bad'):
                try:
                    win.evaluate_js('app.toast(%s)' % json.dumps('読み込めなかったファイル: ' + ' / '.join(r['bad'])))
                except Exception:
                    pass
            api._push()

    def on_loaded():
        win.dom.document.events.dragover += DOMEventHandler(lambda e: None, True, True)
        win.dom.document.events.drop += DOMEventHandler(on_drop, True, True)
        if len(sys.argv) > 1:              # exe のアイコンに動画をドラッグ＆ドロップしたとき
            api.add_paths(sys.argv[1:])
            api._push()

    win.events.loaded += on_loaded
    win.events.closing += lambda: api.close()
    webview.start(debug=os.environ.get('WSVIDEO_DEBUG') == '1')


if __name__ == '__main__':
    main()
