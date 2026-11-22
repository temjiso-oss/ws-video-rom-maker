"""WS Video ROM Maker の画面 (tkinter)。"""
import copy
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import __version__, encoder, media

APP = 'WS Video ROM Maker'
VIDEO_TYPES = [('動画ファイル', '*.mp4 *.m4v *.mkv *.avi *.mov *.wmv *.webm *.flv *.mpg *.mpeg *.ts *.m2ts '
                                '*.3gp *.ogv *.vob *.gif *.asf *.divx *.rm *.rmvb'), ('すべてのファイル', '*.*')]

FPS_KS = [2, 3, 4, 5, 6, 8, 10]


def fps_choices(lines):
    out = []
    for k in FPS_KS:
        fps = 12000 / (lines * k)
        note = ''
        if k == 2:
            note = '（なめらか）'
        elif abs(fps - 15) < 0.6:
            note = '（標準）'
        elif k == 10:
            note = '（データ少なめ）'
        out.append((k, f'{fps:.1f} fps{note}'))
    return out


FPS_CHOICES = fps_choices(159)
AUDIO_CHOICES = [(1, '12 kHz（高音質）'), (2, '6 kHz'), (3, '4 kHz'), (0, 'なし')]
ROM_CHOICES = [(16, '16 MB'), (8, '8 MB'), (4, '4 MB'), (2, '2 MB'), (1, '1 MB')]
QUALITY_CHOICES = [('auto', '自動（ROM容量に合わせて最高画質）'), ('best', '最高'), ('high', '高'),
                   ('normal', '標準'), ('low', '低（容量節約）')]
MODE_CHOICES = [('fit', '全体を表示（黒帯あり）'), ('fill', '画面いっぱい（はみ出しはカット）'),
                ('stretch', '引き伸ばし')]
SPEAKER_CHOICES = [(1, '標準（50%）'), (2, '小（25%）'), (3, '最小（12.5%）'), (0, '最大（100%・音割れします）')]
PAL_CHOICES = [(True, '241色（16パレット・標準）'), (False, '121色（8パレット・変換が速い）')]
DITHER_CHOICES = [(0.0, 'なし'), (0.5, '弱'), (1.0, '標準'), (1.6, '強')]
TARGET_CHOICES = [('rom', 'ROMに入れる（どのカートでも可・最大16MB）'),
                  ('nile', 'nileswan の microSD から再生（長さ無制限）')]
SDKBPS_CHOICES = [(250, '250 KB/秒（標準・実機で確認済み）'), (400, '400 KB/秒（試験的）'), (160, '160 KB/秒（控えめ）'),
                  (100, '100 KB/秒')]

# 使い方ごとのおすすめ設定 (ここにない項目は DEFAULTS のまま)
DEFAULTS = dict(target='rom', k=5, hz60=False, audio=1, rom=16, sd=250, speaker=1, quality='auto', mode='fit',
                dither=1.0, bright=0.0, contrast=1.0, sat=1.2, norm=True, deint=False, preview=False, pal16=True)
PRESETS = {
    'nile60': dict(target='nile', hz60=True, k=3, sd=250),
    'nile': dict(target='nile', hz60=False, k=4, sd=250),
    'rom': dict(target='rom', hz60=False, k=5),
}
PRESET_CHOICES = [('nile60', 'nileswan で再生・60Hz で映す本体（IPS 液晶化・TV スワンなど）'),
                  ('nile', 'nileswan で再生・純正の液晶'),
                  ('rom', 'ROM に入れる（ほかのフラッシュカート。最大16MB = 数分）'),
                  ('custom', 'カスタム（詳細設定で自分で決める）')]


def settings_path():
    base = os.environ.get('APPDATA') or str(Path.home())
    return Path(base) / 'WSVideoRomMaker' / 'settings.json'

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
except Exception:          # ドラッグ＆ドロップのライブラリがなくても動くように
    TkinterDnD = None


def fmt_time(sec):
    if sec is None:
        return '?'
    sec = int(round(sec))
    return f'{sec // 60}:{sec % 60:02d}'


def parse_time(s):
    s = s.strip()
    if not s:
        return None
    parts = s.split(':')
    v = 0.0
    for p in parts:
        v = v * 60 + float(p)
    return v


class Combo(ttk.Combobox):
    def __init__(self, master, choices, default, width=30):
        super().__init__(master, values=[c[1] for c in choices], state='readonly', width=width)
        self.choices = choices
        self.set_value(default)

    def value(self):
        return self.choices[self.current()][0]

    def set_choices(self, choices, value):
        self.choices = choices
        self.configure(values=[c[1] for c in choices])
        self.set_value(value)

    def set_value(self, v):
        for i, c in enumerate(self.choices):
            if c[0] == v:
                self.current(i)
                return


class App:
    def __init__(self, root):
        self.root = root
        root.title(f'{APP} {__version__} — 動画をワンダースワンカラー用ROMに変換')
        root.minsize(760, 640)
        self.items = []         # dict(path, title, duration, desc, start, length)
        self.q = queue.Queue()
        self.worker = None
        self.cancel_flag = False
        self.cache = encoder.AnalysisCache()    # やり直しのときに解析を使い回す
        self._build()
        self.load_settings()            # 前回の設定を戻す
        self._after = root.after(100, self._poll)
        root.protocol('WM_DELETE_WINDOW', self.on_close)

    def on_close(self):
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno(APP, '変換中です。中止して終了しますか？'):
                return
            self.cancel_flag = True
            self.worker.join(10)
        self.save_settings()
        try:
            self.root.after_cancel(self._after)
        except Exception:
            pass
        try:
            self.cache.close()
        except Exception:
            pass
        self.root.destroy()

    # ---- 画面 ----------------------------------------------------------
    def _build(self):
        pad = dict(padx=6, pady=4)
        import tkinter.font as tkfont
        line = tkfont.nametofont('TkDefaultFont').metrics('linespace')
        ttk.Style().configure('Treeview', rowheight=int(line * 1.5))
        main = ttk.Frame(self.root, padding=8)
        main.pack(fill='both', expand=True)

        lf = ttk.LabelFrame(main, text='① 動画ファイル（上から順に再生。本体の左右ボタンで切り替え）')
        lf.pack(fill='both', expand=True, **pad)
        cols = ('title', 'desc', 'dur', 'start', 'len')
        self.tree = ttk.Treeview(lf, columns=cols, show='headings', height=7, selectmode='browse')
        for c, t, w in zip(cols, ('ファイル', '形式', '長さ', '開始', '切り出す長さ'), (300, 140, 60, 60, 90)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor='w' if c in ('title', 'desc') else 'center')
        self.tree.grid(row=0, column=0, rowspan=5, sticky='nsew', padx=4, pady=4)
        sb = ttk.Scrollbar(lf, orient='vertical', command=self.tree.yview)
        sb.grid(row=0, column=1, rowspan=5, sticky='ns')
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.bind('<<TreeviewSelect>>', self._on_select)
        bf = ttk.Frame(lf)
        bf.grid(row=0, column=2, sticky='n', padx=4)
        for t, cmd in (('追加…', self.add_files), ('削除', self.remove), ('↑ 上へ', lambda: self.move(-1)),
                       ('↓ 下へ', lambda: self.move(1))):
            ttk.Button(bf, text=t, command=cmd, width=10).pack(pady=2)
        tf = ttk.Frame(lf)
        tf.grid(row=5, column=0, columnspan=3, sticky='w', padx=4, pady=(0, 4))
        ttk.Label(tf, text='選んだ動画の 開始位置').pack(side='left')
        self.e_start = ttk.Entry(tf, width=8)
        self.e_start.pack(side='left', padx=4)
        ttk.Label(tf, text='長さ').pack(side='left')
        self.e_len = ttk.Entry(tf, width=8)
        self.e_len.pack(side='left', padx=4)
        ttk.Button(tf, text='反映', command=self.apply_trim).pack(side='left', padx=4)
        ttk.Label(tf, text='（例 1:30 / 90、長さが空なら最後まで）', foreground='#666').pack(side='left')
        self.total_lbl = ttk.Label(lf, text='', foreground='#333')
        self.total_lbl.grid(row=6, column=0, columnspan=3, sticky='w', padx=6)
        lf.columnconfigure(0, weight=1)
        lf.rowconfigure(4, weight=1)

        pf = ttk.LabelFrame(main, text='② 使い方を選ぶ')
        pf.pack(fill='x', **pad)
        self.c_preset = Combo(pf, PRESET_CHOICES, 'rom', width=52)
        self.c_preset.grid(row=0, column=0, sticky='w', padx=6, pady=4)
        self.c_preset.bind('<<ComboboxSelected>>', lambda e: self.apply_preset(self.c_preset.value()))
        self.b_detail = ttk.Button(pf, text='詳細設定を表示 ▼', command=self.toggle_detail)
        self.b_detail.grid(row=0, column=1, sticky='w', padx=6)
        self.summary = ttk.Label(pf, text='', foreground='#333')
        self.summary.grid(row=1, column=0, columnspan=2, sticky='w', padx=8, pady=(0, 4))

        sf = ttk.LabelFrame(main, text='詳細設定（形式・fps・読み込み速度・60Hz を変えると「カスタム」になります。明るさなどの好みは使い方を変えても残ります）')
        self.detail_frame = sf
        self.detail_pack = dict(fill='x', after=pf, **pad)
        self.c_fps = Combo(sf, FPS_CHOICES, 5)
        self.c_audio = Combo(sf, AUDIO_CHOICES, 1)
        self.c_rom = Combo(sf, ROM_CHOICES, 16)
        self.c_quality = Combo(sf, QUALITY_CHOICES, 'auto')
        self.c_mode = Combo(sf, MODE_CHOICES, 'fit')
        self.c_dither = Combo(sf, DITHER_CHOICES, 1.0)
        self.c_speaker = Combo(sf, SPEAKER_CHOICES, 1)
        self.c_target = Combo(sf, TARGET_CHOICES, 'rom', width=38)
        self.c_sd = Combo(sf, SDKBPS_CHOICES, 250)
        self.c_pal = Combo(sf, PAL_CHOICES, True)
        ttk.Label(sf, text='出力の形式').grid(row=0, column=0, sticky='e', padx=4, pady=3)
        self.c_target.grid(row=0, column=1, columnspan=3, sticky='w', padx=4, pady=3)
        rows = [('フレームレート', self.c_fps), ('音声', self.c_audio), ('ROMサイズの上限', self.c_rom),
                ('microSD の読み込み速度', self.c_sd), ('本体スピーカーの音量', self.c_speaker),
                ('画質', self.c_quality), ('画面の合わせ方', self.c_mode), ('ディザ（ざらつきで色を補う）', self.c_dither),
                ('1コマの色数', self.c_pal)]
        for i, (t, w) in enumerate(rows):
            r, c = 1 + i % 4, (i // 4) * 2
            ttk.Label(sf, text=t).grid(row=r, column=c, sticky='e', padx=4, pady=3)
            w.grid(row=r, column=c + 1, sticky='w', padx=4, pady=3)
        self.v_bright = tk.DoubleVar(value=0.0)
        self.v_contrast = tk.DoubleVar(value=1.0)
        self.v_sat = tk.DoubleVar(value=1.2)
        sl = ttk.Frame(sf)
        sl.grid(row=5, column=0, columnspan=4, sticky='w', padx=4, pady=3)
        for t, v, lo, hi in (('明るさ', self.v_bright, -0.3, 0.3), ('コントラスト', self.v_contrast, 0.5, 1.8),
                             ('彩度', self.v_sat, 0.0, 2.0)):
            ttk.Label(sl, text=t).pack(side='left', padx=(8, 2))
            ttk.Scale(sl, from_=lo, to=hi, variable=v, length=110).pack(side='left')
            lbl = ttk.Label(sl, width=5)
            lbl.pack(side='left')
            v.trace_add('write', lambda *a, v=v, lbl=lbl: lbl.configure(text=f'{v.get():.2f}'))
            lbl.configure(text=f'{v.get():.2f}')
        ttk.Button(sl, text='標準に戻す', command=self._reset_eq).pack(side='left', padx=8)
        ck = ttk.Frame(sf)
        ck.grid(row=6, column=0, columnspan=4, sticky='w', padx=4, pady=3)
        self.v_norm = tk.BooleanVar(value=True)
        self.v_deint = tk.BooleanVar(value=False)
        self.v_preview = tk.BooleanVar(value=True)
        self.v_hz60 = tk.BooleanVar(value=False)
        ttk.Checkbutton(ck, text='音量を自動でそろえる', variable=self.v_norm).pack(side='left', padx=8)
        ttk.Checkbutton(ck, text='インターレース解除（TV録画など）', variable=self.v_deint).pack(side='left', padx=8)
        ttk.Checkbutton(ck, text='確認用のプレビュー動画（.mp4）も作る', variable=self.v_preview).pack(side='left', padx=8)
        ck2 = ttk.Frame(sf)
        ck2.grid(row=7, column=0, columnspan=4, sticky='w', padx=4, pady=3)
        ttk.Checkbutton(ck2, text='60Hz モード（60Hzで映す本体向け。純正の液晶では映らないことがあります）',
                        variable=self.v_hz60, command=self._on_hz).pack(side='left', padx=8)
        ttk.Button(ck2, text='すべて最初の設定に戻す', command=self.reset_all).pack(side='left', padx=16)
        # 詳細設定を触ったら「カスタム」にする
        for w in (self.c_fps, self.c_audio, self.c_rom, self.c_target, self.c_sd, self.c_speaker, self.c_quality,
                  self.c_mode, self.c_dither, self.c_pal):
            w.bind('<<ComboboxSelected>>', lambda e: self._changed(), add='+')
        for v in (self.v_bright, self.v_contrast, self.v_sat, self.v_norm, self.v_deint, self.v_preview,
                  self.v_hz60):
            v.trace_add('write', lambda *a: self._changed())

        of = ttk.LabelFrame(main, text='③ 保存先（.wsc。nileswan のときは同じフォルダに .WSV もできます）')
        of.pack(fill='x', **pad)
        self.v_out = tk.StringVar()
        ttk.Entry(of, textvariable=self.v_out).pack(side='left', fill='x', expand=True, padx=4, pady=4)
        ttk.Button(of, text='参照…', command=self.choose_out).pack(side='left', padx=4)
        self.v_split = tk.BooleanVar(value=False)
        ttk.Checkbutton(main, variable=self.v_split,
                        text='1本ずつ別の ROM にする（保存先と同じフォルダに、動画の名前で 1 本ずつ作ります）'
                        ).pack(fill='x', padx=10)

        rf = ttk.Frame(main)
        rf.pack(fill='x', **pad)
        self.b_run = ttk.Button(rf, text='▶ 変換開始', command=self.start)
        self.b_run.pack(side='left')
        self.b_cancel = ttk.Button(rf, text='中止', command=self.cancel, state='disabled')
        self.b_cancel.pack(side='left', padx=6)
        self.b_open = ttk.Button(rf, text='出力フォルダを開く', command=self.open_folder, state='disabled')
        self.b_open.pack(side='left', padx=6)
        ttk.Button(rf, text='このアプリについて', command=self.about).pack(side='right', padx=6)
        self.pb = ttk.Progressbar(rf, maximum=1000)
        self.pb.pack(side='left', fill='x', expand=True, padx=6)
        self.status = ttk.Label(main, text='動画を追加してください。')
        self.status.pack(fill='x', padx=8)
        self.log = tk.Text(main, height=8, wrap='word', state='disabled', font=('Meiryo UI', 9))
        self.log.pack(fill='both', expand=False, padx=6, pady=4)
        self._say('本体の操作: START/A=一時停止  B=最初から  Y2(右)/Y4(左)=10秒進む/戻る  Y1(上)/Y3(下)=1分進む/戻る\n'
                  '            X2(右)/X4(左)=次/前の動画  X1(上)/X3(下)=スピーカーの音量')
        self._update_total()

    # ---- 設定のまとめ・保存 ---------------------------------------------
    def get_state(self):
        return dict(target=self.c_target.value(), k=self.c_fps.value(), hz60=bool(self.v_hz60.get()),
                    audio=self.c_audio.value(), rom=self.c_rom.value(), sd=self.c_sd.value(),
                    speaker=self.c_speaker.value(), quality=self.c_quality.value(), mode=self.c_mode.value(),
                    dither=self.c_dither.value(), bright=round(self.v_bright.get(), 2),
                    contrast=round(self.v_contrast.get(), 2), sat=round(self.v_sat.get(), 2),
                    norm=bool(self.v_norm.get()), deint=bool(self.v_deint.get()),
                    preview=bool(self.v_preview.get()), pal16=bool(self.c_pal.value()))

    def set_state(self, st):
        d = dict(DEFAULTS)
        d.update({k: v for k, v in st.items() if k in DEFAULTS})
        self._applying = True
        try:
            self.v_hz60.set(d['hz60'])
            self.c_fps.set_choices(fps_choices(self._lines()), d['k'])
            self.c_target.set_value(d['target'])
            self.c_audio.set_value(d['audio'])
            self.c_rom.set_value(d['rom'])
            self.c_sd.set_value(d['sd'])
            self.c_speaker.set_value(d['speaker'])
            self.c_quality.set_value(d['quality'])
            self.c_mode.set_value(d['mode'])
            self.c_dither.set_value(d['dither'])
            self.v_bright.set(d['bright'])
            self.v_contrast.set(d['contrast'])
            self.v_sat.set(d['sat'])
            self.v_norm.set(d['norm'])
            self.v_deint.set(d['deint'])
            self.v_preview.set(d['preview'])
            self.c_pal.set_value(bool(d['pal16']))
        finally:
            self._applying = False
        self._changed()

    def apply_preset(self, name):
        if name == 'custom':
            self.show_detail(True)
            return
        st = self.get_state()
        st.update(DEFAULTS)
        # 画面の明るさ・プレビューなど使い方に関係ない好みは残す
        for k in ('bright', 'contrast', 'sat', 'norm', 'deint', 'preview', 'speaker', 'mode', 'dither', 'audio', 'pal16'):
            st[k] = self.get_state()[k]
        st.update(PRESETS[name])
        self.set_state(st)

    def _changed(self):
        if getattr(self, '_applying', False) or not hasattr(self, 'c_preset'):
            return
        st = self.get_state()
        name = 'custom'
        for pn, pv in PRESETS.items():
            want = dict(st)
            want.update(pv)
            if want == st:
                name = pn
                break
        self.c_preset.set_value(name)
        fps = 12000 / (self._lines() * st['k'])
        if st['target'] == 'nile':
            what = f'nileswan（microSD）/ 読み込み {st["sd"]} KB/秒'
        else:
            what = f'ROM（最大 {st["rom"]}MB）'
        hz = '60Hz' if st['hz60'] else '75Hz（本来の速さ）'
        audio = 'なし' if not st['audio'] else f'{12 // st["audio"]}kHz'
        self.summary.configure(text=f'今の設定: {what} / {fps:.1f} fps / {hz} / 音声 {audio}')
        self._update_total()

    def toggle_detail(self):
        self.show_detail(not self.detail_frame.winfo_ismapped())

    def show_detail(self, on):
        if on and not self.detail_frame.winfo_ismapped():
            self.detail_frame.pack(**self.detail_pack)
            self.b_detail.configure(text='詳細設定を隠す ▲')
        elif not on and self.detail_frame.winfo_ismapped():
            self.detail_frame.pack_forget()
            self.b_detail.configure(text='詳細設定を表示 ▼')

    def reset_all(self):
        if messagebox.askyesno(APP, 'すべての設定を最初の状態に戻しますか？'):
            self.set_state(dict(DEFAULTS))

    def load_settings(self):
        self.last_outdir = ''
        try:
            import json
            d = json.loads(settings_path().read_text(encoding='utf-8'))
        except Exception:
            self.set_state(dict(DEFAULTS))
            return
        self.last_outdir = d.get('outdir', '') or ''
        self.set_state(d.get('state', {}))
        if d.get('detail'):
            self.show_detail(True)

    def save_settings(self):
        try:
            import json
            p = settings_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(dict(state=self.get_state(), outdir=self.last_outdir,
                                         detail=bool(self.detail_frame.winfo_ismapped())),
                                    ensure_ascii=False, indent=1), encoding='utf-8')
        except Exception:
            pass

    def _reset_eq(self):
        self.v_bright.set(0.0)
        self.v_contrast.set(1.0)
        self.v_sat.set(1.2)

    # ---- ファイル一覧 ----------------------------------------------------
    def add_files(self):
        self.add_paths(filedialog.askopenfilenames(title='動画を選択', filetypes=VIDEO_TYPES))

    def add_paths(self, paths):
        bad = []
        for p in paths:
            try:
                inf = media.probe(p)
            except Exception as ex:
                bad.append(f'{os.path.basename(p)}: {ex}')
                continue
            if not inf.has_video:
                bad.append(f'{os.path.basename(p)}: 映像がありません')
                continue
            desc = inf.desc + ('' if inf.has_audio else '（音声なし）')
            self.items.append(dict(path=p, title=os.path.basename(p), duration=inf.duration, desc=desc,
                                   start=0.0, length=None))
        if bad:
            messagebox.showwarning(APP, '読み込めなかったファイル:\n' + '\n'.join(bad))
        self._refresh()

    def _auto_out(self):
        """保存先の名前を、いちばん上の動画の名前から付ける。
        空のとき、または前に自動で付けた名前のままのときだけ変える (自分で決めた名前は変えない)。
        ROM の名前から microSD の .WSV の名前が決まるので、動画ごとに別の名前になるようにする。"""
        if not self.items:
            return
        cur = self.v_out.get().strip()
        if cur and cur != getattr(self, '_auto_name', None):
            return
        p = Path(self.items[0]['path'])
        if cur:
            d = Path(cur).parent                    # 自動で付けた名前のフォルダはそのまま
        else:
            d = Path(self.last_outdir) if self.last_outdir and Path(self.last_outdir).is_dir() else p.parent
        self._auto_name = str(d / (short_stem(p.stem) + '.wsc'))
        self.v_out.set(self._auto_name)

    def _refresh(self, select=None):
        self._auto_out()
        self.tree.delete(*self.tree.get_children())
        for i, it in enumerate(self.items):
            self.tree.insert('', 'end', iid=str(i), values=(
                it['title'], it['desc'], fmt_time(it['duration']), fmt_time(it['start']),
                fmt_time(it['length']) if it['length'] else '最後まで'))
        if select is not None and 0 <= select < len(self.items):
            self.tree.selection_set(str(select))
        self.status.configure(text=f'{len(self.items)} 本の動画' if self.items else '動画を追加してください。')
        self._update_total()

    def _sel(self):
        s = self.tree.selection()
        return int(s[0]) if s else None

    def _on_select(self, _e=None):
        i = self._sel()
        if i is None:
            return
        it = self.items[i]
        self.e_start.delete(0, 'end')
        self.e_start.insert(0, fmt_time(it['start']))
        self.e_len.delete(0, 'end')
        if it['length']:
            self.e_len.insert(0, fmt_time(it['length']))

    def apply_trim(self):
        i = self._sel()
        if i is None:
            return
        try:
            self.items[i]['start'] = parse_time(self.e_start.get()) or 0.0
            self.items[i]['length'] = parse_time(self.e_len.get())
        except ValueError:
            messagebox.showerror(APP, '時間は 90 や 1:30 のように入力してください。')
            return
        self._refresh(i)

    def remove(self):
        i = self._sel()
        if i is not None:
            del self.items[i]
            self._refresh(min(i, len(self.items) - 1))

    def move(self, d):
        i = self._sel()
        if i is None or not 0 <= i + d < len(self.items):
            return
        self.items[i], self.items[i + d] = self.items[i + d], self.items[i]
        self._refresh(i + d)

    def _item_seconds(self, it):
        d = max(0.0, (it['duration'] or 0) - it['start'])
        return min(d, it['length']) if it['length'] else d

    def _lines(self):
        return 199 if self.v_hz60.get() else 159

    def _on_hz(self):
        # フレームレートの選択肢を作り直し、今に近い fps を選び直す
        old = 12000 / ((358 - self._lines()) * self.c_fps.value())
        ch = fps_choices(self._lines())
        best = min(ch, key=lambda c: abs(12000 / (self._lines() * c[0]) - old))[0]
        self.c_fps.set_choices(ch, best)
        self._update_total()

    def _update_total(self):
        nile = self.c_target.value() == 'nile'
        self.c_rom.configure(state='disabled' if nile else 'readonly')
        self.c_sd.configure(state='readonly' if nile else 'disabled')
        if not self.items:
            self.total_lbl.configure(text='ここに動画ファイルをドラッグ＆ドロップできます'
                                     if TkinterDnD else '')
            return
        tot = sum(self._item_seconds(it) for it in self.items)
        k = self.c_fps.value()
        frames = tot * 12000 / (self._lines() * k)
        ab = tot * (12000 / self.c_audio.value()) if self.c_audio.value() else 0
        if nile:
            mb = tot * self.c_sd.value() / 1024
            self.total_lbl.configure(text=f'合計 {fmt_time(tot)}（{frames:.0f}コマ）  '
                                          f'microSD に置くデータ: 最大 約{mb:.0f}MB')
            return
        cap = (self.c_rom.value() * 16 - 1) * 65536
        # 動きの多い動画で 1コマ ≒ 6KB、少ない動画で ≒ 2KB として目安を出す
        hi = (frames * 6000 + ab) / cap
        lo = (frames * 2000 + ab) / cap
        msg = f'合計 {fmt_time(tot)}（{frames:.0f}コマ）  ROM使用量の目安: {lo * 100:.0f}〜{hi * 100:.0f}%'
        if ab > cap:
            msg += '  ※音声だけで容量を超えます'
        elif lo > 1:
            msg += '  ※入りきらない可能性が高いです（短くする・fpsを下げる・ROMを大きく）'
        self.total_lbl.configure(text=msg)

    def choose_out(self):
        p = filedialog.asksaveasfilename(title='保存先', defaultextension='.wsc',
                                         filetypes=[('WonderSwan Color ROM', '*.wsc')],
                                         initialfile=Path(self.v_out.get() or (short_stem(Path(self.items[0]['path']).stem) + '.wsc' if self.items else 'video.wsc')).name)
        if p:
            self.v_out.set(p)

    def about(self):
        messagebox.showinfo(APP, f'{APP} {__version__}\n作者: BW\nライセンス: MIT\n\n'
                            '同梱の FFmpeg は GNU GPL v3 です。そのほか Python、NumPy、Pillow、Tcl/Tk、'
                            'tkinterdnd2、PyInstaller を含みます。詳しくはアプリのフォルダの '
                            'THIRD_PARTY_NOTICES.md と licenses フォルダを見てください。\n\n'
                            '非公式のツールです。株式会社バンダイ、nileswan の開発者とは関係ありません。\n'
                            '「ワンダースワン」「スワンクリスタル」は株式会社バンダイの商標です。')

    def open_folder(self):
        p = Path(self.v_out.get())
        if p.exists():
            subprocess.run(['explorer', '/select,', str(p)])
        elif p.parent.is_dir():                 # 1本ずつ作ったときは、欄の名前のファイルがないことがある
            subprocess.run(['explorer', str(p.parent)])

    # ---- 変換 ------------------------------------------------------------
    def settings(self):
        out = self.v_out.get()
        return encoder.Settings(
            k=self.c_fps.value(), audio_div=self.c_audio.value(), max_rom_mb=self.c_rom.value(),
            quality=self.c_quality.value(), mode=self.c_mode.value(), dither=self.c_dither.value(),
            brightness=round(self.v_bright.get(), 3), contrast=round(self.v_contrast.get(), 3),
            saturation=round(self.v_sat.get(), 3), normalize=self.v_norm.get(), deinterlace=self.v_deint.get(),
            speaker_shift=self.c_speaker.value(), target=self.c_target.value(), sd_kbps=self.c_sd.value(),
            hz60=self.v_hz60.get(), pal16=bool(self.c_pal.value()),
            preview_path=str(Path(out).with_name(Path(out).stem + '_preview.mp4')) if self.v_preview.get() else None)

    def start(self):
        if not self.items:
            messagebox.showinfo(APP, '先に動画を追加してください。')
            return
        out = self.v_out.get().strip()
        if not out:
            self.choose_out()
            out = self.v_out.get().strip()
            if not out:
                return
        if not out.lower().endswith('.wsc'):
            out += '.wsc'
            self.v_out.set(out)
        s = self.settings()
        if s.target == 'rom':
            secs = [self._item_seconds(it) for it in self.items]
            if self.v_split.get() and len(secs) > 1:     # 1本ずつなら、1本ごとに入るかを見る
                msg = next((x for x in (encoder.rom_fit_precheck([v], s) for v in secs) if x), None)
            else:
                msg = encoder.rom_fit_precheck(secs, s)
            if msg:
                ans = messagebox.askyesnocancel(
                    APP, msg.split('\n')[0] + '\n\n「nileswan の microSD から再生」に切り替えて変換しますか？\n'
                    '（いいえ = ROM のまま変換しない）')
                if not ans:
                    return
                self.c_target.set_value('nile')
                self._update_total()
                s = self.settings()
        items = [encoder.Item(it['path'], it['start'], it['length'], it['title']) for it in self.items]
        split = bool(self.v_split.get()) and len(items) > 1
        if split:
            jobs = [([it], o) for it, o in zip(items, split_outputs([it['path'] for it in self.items], Path(out).parent))]
            exist = [Path(o).name for _, o in jobs if Path(o).exists()]
            if exist and not messagebox.askokcancel(
                    APP, '次のファイルは、すでにあります。上書きしますか？\n' + '\n'.join(exist[:8])):
                return
        else:
            jobs = [(items, out)]
        self.cancel_flag = False
        self.b_run.configure(state='disabled')
        self.b_cancel.configure(state='normal')
        self.b_open.configure(state='disabled')
        self.pb['value'] = 0
        self._say('—— 変換開始 ——')
        self.last_outdir = str(Path(out).parent)
        self.save_settings()
        self.t0 = time.time()

        def work():
            try:
                n = len(jobs)
                for i, (its, o) in enumerate(jobs):
                    sj = s
                    if n > 1:
                        sj = copy.copy(s)       # プレビューの名前は ROM ごとに
                        if s.preview_path:
                            sj.preview_path = str(Path(o).with_name(Path(o).stem + '_preview.mp4'))
                        self.q.put(('log', f'—— {i + 1} / {n} 本目: {Path(o).name} ——'))
                    r = encoder.convert(its, sj, o,
                                        lambda f, m, i=i: self.q.put(('prog', (i + f) / n,
                                                                      (f'[{i + 1}/{n}] ' if n > 1 else '') + m)),
                                        lambda m: self.q.put(('log', m)), lambda: self.cancel_flag, cache=self.cache)
                    self.q.put(('done', r, o, sj, i == n - 1))
            except encoder.Cancelled:
                self.q.put(('cancelled',))
            except Exception as ex:
                self.q.put(('error', str(ex)))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def cancel(self):
        self.cancel_flag = True
        self.status.configure(text='中止しています…')

    def _say(self, msg):
        self.log.configure(state='normal')
        self.log.insert('end', msg + '\n')
        self.log.see('end')
        self.log.configure(state='disabled')

    def _finish(self):
        self.b_run.configure(state='normal')
        self.b_cancel.configure(state='disabled')

    def _poll(self):
        try:
            while True:
                m = self.q.get_nowait()
                if m[0] == 'prog':
                    self.pb['value'] = m[1] * 1000
                    el = time.time() - self.t0
                    eta = f'  残り約{fmt_time(el / m[1] - el)}' if 0.05 < m[1] < 0.85 else ''
                    self.status.configure(text=f'{m[2]}{eta}')
                elif m[0] == 'log':
                    self._say(m[1])
                elif m[0] == 'done':
                    r, out, s, last = m[1], m[2], m[3], m[4]
                    if last:
                        self._finish()
                        self.b_open.configure(state='normal')
                    msg = (f'完了: {Path(out).name}（ROM {r.rom_mb}MB）\n'
                           f'  再生時間 {fmt_time(r.seconds)} / {r.frames}コマ / {s.fps:.1f}fps / '
                           f'1コマ平均 {r.avg_tiles:.0f}タイル書き換え\n'
                           f'  映像 {r.video_bytes / 1048576:.2f}MB + 音声 {r.audio_bytes / 1048576:.2f}MB  '
                           f'（画質パラメータ {r.tau:.0f}、小さいほど高画質）  所要 {fmt_time(time.time() - self.t0)}')
                    if s.preview_path:
                        msg += f'\n  プレビュー: {Path(s.preview_path).name}'
                    if r.wsv_path:
                        wn = Path(r.wsv_path).name
                        msg += (f'\n  データファイル: {wn}（{r.wsv_size / 1048576:.0f}MB）\n'
                                f'  ★ microSD の「いちばん上のフォルダ（ルート）」に {wn} を、'
                                f'好きなフォルダに {Path(out).name} をコピーしてください。')
                        if r.limited:
                            msg += (f'\n  ※ 読み込み速度の上限のため {r.limited} コマで書き換えを減らしました'
                                    f'（実機で余裕があれば速度を上げて変換し直すと画質が上がります）')
                    self._say(msg)
                    if last:
                        self.status.configure(text='完了しました。')
                elif m[0] == 'cancelled':
                    self._finish()
                    self._say('中止しました。')
                    self.status.configure(text='中止しました。')
                elif m[0] == 'error':
                    self._finish()
                    self._say('エラー: ' + m[1])
                    self.status.configure(text='エラーが発生しました。')
                    messagebox.showerror(APP, m[1])
        except queue.Empty:
            pass
        self._after = self.root.after(100, self._poll)


NAME_MAX = 40       # 自動で付ける ROM の名前の長さ (文字数)。長すぎる名前は nileswan の一覧で「?」になり、選べない


def short_stem(stem, n=NAME_MAX):
    """自動で付ける名前を n 文字までにする (切った末尾の空白や記号は落とす)。"""
    if len(stem) <= n:
        return stem
    return stem[:n].rstrip(' ._-~　、。・') or stem[:n]


def split_outputs(paths, outdir):
    """「1本ずつ別の ROM にする」ときの保存先。動画の名前 + .wsc。
    ROM の名前も、そこから決まる microSD の .WSV の名前も、重ならないように番号を足す。"""
    outs, stems, wsvs = [], set(), set()
    for p in paths:
        base, n = short_stem(Path(p).stem), 1
        while True:
            stem = base if n == 1 else f'{base}_{n}'
            w = encoder.wsv_name(stem + '.wsc')
            if stem.lower() not in stems and w not in wsvs:
                break
            n += 1
        stems.add(stem.lower())
        wsvs.add(w)
        outs.append(str(Path(outdir) / (stem + '.wsc')))
    return outs


def main():
    if os.name == 'nt':
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = None
    if TkinterDnD:
        try:
            root = TkinterDnD.Tk()
        except Exception:
            root = None
    dnd = root is not None
    if root is None:
        root = tk.Tk()
    try:
        ttk.Style().theme_use('vista')
    except tk.TclError:
        pass
    app = App(root)
    if dnd:
        # ウィンドウのどこに落としても動画を追加する
        def on_drop(event):
            app.add_paths([p for p in root.tk.splitlist(event.data) if os.path.isfile(p)])
            return event.action

        for w in (root, app.tree):
            w.drop_target_register(DND_FILES)
            w.dnd_bind('<<Drop>>', on_drop)
    if len(sys.argv) > 1:          # exe のアイコンに動画をドラッグ＆ドロップしたとき
        app.add_paths(sys.argv[1:])
    root.mainloop()


if __name__ == '__main__':
    main()
