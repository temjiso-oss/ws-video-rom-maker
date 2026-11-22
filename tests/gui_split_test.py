"""「1本ずつ別の ROM にする」を、画面を自動で動かして確かめる。

    python tests/gui_split_test.py <作業フォルダ>

短いテスト用の動画を 2 本作り、(1) まとめて 1 つ、(2) 1 本ずつ、の両方で変換して、できたファイルを調べる。
ふだんの設定ファイルには書かない (save_settings を止める)。
"""
import subprocess
import sys
import time
import tkinter as tk
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from wsvideo import encoder, gui, media  # noqa: E402

work = Path(sys.argv[1])
work.mkdir(parents=True, exist_ok=True)
ff = media.ffmpeg_exe() if hasattr(media, 'ffmpeg_exe') else 'ffmpeg'
vids = []
for name, src in (('テスト動画その1', 'testsrc=size=448x288:rate=30'), ('テスト動画その2', 'testsrc2=size=448x288:rate=30')):
    p = work / (name + '.mp4')
    subprocess.run([ff, '-y', '-loglevel', 'error', '-f', 'lavfi', '-i', src, '-f', 'lavfi', '-i', 'sine=frequency=440',
                    '-t', '3', '-pix_fmt', 'yuv420p', str(p)], check=True)
    vids.append(str(p))

msgs = []
gui.messagebox.showinfo = lambda *a, **k: msgs.append(a[1])
gui.messagebox.showwarning = lambda *a, **k: msgs.append(a[1])
gui.messagebox.showerror = lambda *a, **k: msgs.append('ERR ' + a[1])
gui.messagebox.askokcancel = lambda *a, **k: True
gui.messagebox.askyesnocancel = lambda *a, **k: True

root = tk.Tk()
app = gui.App(root)
app.save_settings = lambda: None
app.last_outdir = str(work)
app.c_target.set_value('nile')
app.v_preview.set(False)
app.v_out.set('')
app.add_paths(vids)


def run():
    app.start()
    t0 = time.time()
    while app.worker.is_alive() or str(app.b_run['state']) == 'disabled':
        root.update()
        time.sleep(0.05)
        assert time.time() - t0 < 600, '時間切れ'
    root.update()


def files():
    return sorted(p.name for p in work.iterdir() if p.suffix.lower() in ('.wsc', '.wsv'))


auto = Path(app.v_out.get()).name
assert auto == 'テスト動画その1.wsc', auto
run()
one = files()
print('まとめて:', one)
assert len(one) == 2, one
for p in work.iterdir():
    if p.suffix.lower() in ('.wsc', '.wsv'):
        p.unlink()

app.v_split.set(True)
run()
two = files()
print('1本ずつ:', two)
assert len(two) == 4, two
wsc = [work / n for n in two if n.endswith('.wsc')]
for w in wsc:                                   # ROM の中の名前と、できた .WSV が合っているか
    want = encoder.wsv_name(str(w))
    assert (work / want).exists(), want
    rom = w.read_bytes()
    base, _, ext = want.partition('.')
    assert (base.ljust(8) + ext.ljust(3)).encode('ascii') in rom, want
assert len({encoder.wsv_name(str(w)) for w in wsc}) == 2
assert not [m for m in msgs if m.startswith('ERR')], msgs
log = app.log.get('1.0', 'end')
assert '1 / 2 本目' in log and '2 / 2 本目' in log
print('status:', app.status.cget('text'))
print('OK')
root.destroy()
