# 同梱しているソフトウェアとライセンス / Third-party notices

WS Video ROM Maker の本体（`src/` のプログラムとビルド用スクリプト）は MIT ライセンスです（`LICENSE`）。
Windows 版の配布物には、次のソフトウェアが含まれています。各ライセンスの全文は `licenses/` フォルダにあります。

| ソフトウェア | 用途 | ライセンス | 全文 |
|---|---|---|---|
| FFmpeg 9.0.2（gyan.dev "essentials" ビルド、imageio-ffmpeg 0.8.1 経由で入手） | 動画・音声の読み込み、プレビュー動画の作成 | **GNU GPL v3**（`--enable-gpl --enable-version3` でビルドされたもの） | `licenses/GPL-3.0.txt` |
| Python 3.12 | 実行環境 | PSF License | `licenses/Python-LICENSE.txt` |
| NumPy 2.5 | 計算 | BSD-3-Clause ほか | `licenses/numpy-LICENSE.txt` |
| Pillow 13.0 | 画像処理 | MIT-CMU (HPND) | `licenses/Pillow-LICENSE.txt` |
| Tcl/Tk 8.6 (tkinter) | 画面 | Tcl/Tk License (BSD 系) | `licenses/Tcl-Tk-license.terms` |
| tkinterdnd2 0.7 / tkdnd | ドラッグ＆ドロップ | MIT（tkinterdnd2）/ BSD 系（tkdnd） | `licenses/tkinterdnd2-LICENSE.txt` |
| imageio-ffmpeg 0.8 | FFmpeg の入手 | BSD-2-Clause | `licenses/imageio-ffmpeg-LICENSE.txt` |
| PyInstaller 6.22（ブートローダー） | exe 化 | GPL v2 以降 + 配布を認める例外条項 | `licenses/PyInstaller-COPYING.txt` |
| pywebview 6.2（1.5.0〜） | 画面（Windows の WebView2 で表示） | BSD-3-Clause | `licenses/pywebview-LICENSE.txt` |
| bottle 0.13 | pywebview が使う小さな Web サーバー | MIT | `licenses/bottle-LICENSE.txt` |
| pythonnet 3.2 / clr_loader 0.3 | pywebview が Windows の部品を呼ぶため | MIT | `licenses/pythonnet-LICENSE.txt`・`licenses/clr_loader-LICENSE.txt` |
| proxy_tools 0.1 | pywebview が使う | MIT | （パッケージに全文の添付なし） |

GPU 版（`WSVideoRomMaker-x.x.x-GPU.zip`）には、さらに次のものが含まれています。

| ソフトウェア | 用途 | ライセンス | 全文 |
|---|---|---|---|
| CuPy 14.2（cupy-cuda12x） | GPU での計算 | MIT | `licenses/cupy_cuda12x-LICENSE.txt` |
| cuda-pathfinder 1.9 | CUDA の部品を探す | Apache-2.0 | `licenses/cuda_pathfinder-LICENSE.txt` |
| NVIDIA CUDA の実行用の部品 12.9（cuBLAS・cuFFT・cuRAND・cuSOLVER・cuSPARSE・NVRTC・nvJitLink・CUDA Runtime。PyPI の nvidia-*-cu12） | GPU での計算 | NVIDIA の使用許諾（再配布を認める部品） | `licenses/NVIDIA-CUDA-License.txt` |

## FFmpeg について（GPL v3）

- `_internal/ffmpeg.exe` は FFmpeg プロジェクト（https://ffmpeg.org/ ）の成果物で、改変していません。
  このアプリは ffmpeg.exe を別のプログラムとして起動して使っています。
- ビルド: FFmpeg 9.0.2 "essentials_build"（https://www.gyan.dev/ffmpeg/builds/ ）。
- ソースコードの入手先: FFmpeg 9.0.2 のソース（`ffmpeg-9.0.2.tar.xz`）は、このアプリの GitHub の Releases（各版の添付ファイル）に置いています。
  公式の配布場所は https://ffmpeg.org/download.html （リリース版 9.0.2、https://ffmpeg.org/releases/ ）、
  ビルドに使われた外部ライブラリとビルド設定は上記 gyan.dev のページで公開されています。
  入手できない場合は、配布ページ（GitHub の Issues など）でお知らせください。
- FFmpeg は GPL v3 の条件のもとで、誰でも自由に再配布・改変できます。

## 参考にしたもの

- **libnile**（Adrian "asie" Siekierka ほか、zlib ライセンス、https://github.com/49bitcat/libnile ）:
  nileswan の microSD を読む手順（SPI のコマンドの順番など）を参考にしました。コードはアセンブリで書き直しています。
  ライセンス全文: `licenses/libnile-LICENSE.txt`
- **WSdev Wiki**（https://ws.nesdev.org/ ）: ワンダースワンのハードウェアの資料。
- **nileswan**（49bitcat、https://github.com/49bitcat/nileswan ）: カートリッジの開発者向け資料。

## 商標について

「ワンダースワン」「WonderSwan」「ワンダースワンカラー」「スワンクリスタル」は株式会社バンダイの商標です。
このアプリは非公式のもので、株式会社バンダイおよび nileswan の開発者とは関係ありません。
