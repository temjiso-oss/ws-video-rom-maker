"""mesem 用の microSD イメージを作り直す: 指定以外の .WSV を消し、指定の .WSV を入れ、MENU.WS を差し替える。
    python -X utf8 mkimg.py イメージ ROM WSV...
"""
import sys
from pathlib import Path

sys.path.insert(0, r'J:\01_AI-Projects\projects\ws-video-rom-maker\tests')
import fat32img as F  # noqa: E402

img = F.Fat32(sys.argv[1])
rom = Path(sys.argv[2])
want = {F.name83(Path(p).name): Path(p) for p in sys.argv[3:]}
names = []
for c, i, e in img.dir_entries():
    if e[0] == 0:
        break
    if e[0] != 0xE5 and e[11] != 0x0F:
        names.append(bytes(e[:11]))
# 2026-11-25: 前は同じ名前の .WSV を残していたので、設定だけ変えた (名前が同じ) 新しい .WSV が入らず、
# 古い方を再生していた (calib.ps1 の予算 1.0〜4.5 の比べが無効になった)。今は全部消してから入れ直す。
for nm in names:
    if nm.endswith(b'WSV'):
        img.delete(nm)
for nm, p in want.items():
    img.put(p.read_bytes(), nm)
dcl = img.find(F.name83('NILESWAN'))
img.put(rom.read_bytes(), F.name83('MENU.WS'), dclus=dcl)
img.ls()
