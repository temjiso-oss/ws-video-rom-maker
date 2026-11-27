"""GUI 起動用 (PyInstaller の入口)。

WSVideoRomMaker.exe --cli 入力 ... -o 出力.wsc  とするとコマンドライン版として動く
(exe はウィンドウアプリなので、表示は WSV_LOG で指定したファイル、なければ出力先の .log に書く)。"""
import os
import sys


def run_cli(args):
    from wsvideo import cli
    if sys.stdout is None:
        log = os.environ.get('WSV_LOG')
        if not log:
            out = args[args.index('-o') + 1] if '-o' in args else 'wsvideo'
            log = os.path.splitext(out)[0] + '.log'
        sys.stdout = sys.stderr = open(log, 'w', encoding='utf-8')
    try:
        cli.main(args)
    except Exception as ex:
        print('エラー:', ex)
        sys.exit(1)


if __name__ == '__main__':
    import multiprocessing
    multiprocessing.freeze_support()    # exe 版で並列解析の子プロセスを動かすため
    if len(sys.argv) > 1 and sys.argv[1] == '--cli':
        run_cli(sys.argv[2:])
    elif len(sys.argv) > 1 and sys.argv[1] == '--classic':
        # 前の画面 (Tkinter)
        sys.argv.pop(1)
        from wsvideo.gui import main
        main()
    else:
        # 1.5.0〜 の画面 (HTML + pywebview)。使えない PC では前の画面にする
        try:
            import webview  # noqa: F401
            from wsvideo.webgui import main
        except Exception:
            from wsvideo.gui import main
        main()
