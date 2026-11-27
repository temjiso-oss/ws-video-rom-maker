; ===========================================================================
; WS Video Player - WonderSwan Color / SwanCrystal 動画再生プレイヤー
; V30MZ (80186互換) / 75.47 Hz / 224x144
;
; 2種類をこの1ファイルから作る (build_player.py):
;   player.bin       通常版。動画データは ROM の中 (最大16MB)。
;   player_nile.bin  nileswan 版 (-DNILE)。動画データは microSD 上の .WSV ファイルから
;                    読みながら PSRAM のリングバッファに入れて再生する (長さの制限なし)。
;
; このコードはROMの最終バンク(セグメント0xF000)に置かれる。
; 変換アプリ(wsvideo/rom.py)が config 表とヘッダーを書き換える。
;
; 映像: ダブルバッファ。
;   バッファA: タイル0-503 (0x4000-)、マップ0x1000、パレット0-7 (0xFE00-)
;   バッファB: タイル512-1015 (0x8000-)、マップ0x1800、パレット8-15 (0xFF00-)
;   隠れている側に次のフレームを書き、VBlankでポート0x07を切り替える。
; 音声: チャンネル2のボイスモード(8bit PCM)。R ライン毎(12000/R Hz)に1バイト。
;   R=1〜3 (12k/6k/4kHz) は音の DMA (SDMA) で鳴らす: 本体の RAM のリング (RING、2KB) を
;   くり返し読ませ、VBlank ごとに一般 DMA (GDMA) で音声データを足す (atopup)。
;   それ以外の R と -DNOSDMA のときは、HBlank タイマー割り込みでポート0x89に書く (isr_hbl)。
; ストリームの読み出し:
;   通常版   映像 = セグメント0x2000(ポートC2)、音声 = 0x3000(ポートC3)
;   nileswan 映像 = セグメント0x3000(ポートD4)、音声 = 0x2000(ポートC2)
;            (SPI受信バッファも 0x3000 経由で読むため。音声の割り込みと干渉しない)
;
; 映像フレームの形式:
;   u8 hdr  bit7=このバンクの残りは空き(次のバンクの先頭へ) / bit0=パレットあり / bit1=16パレット
;           bit2=全面描き直し (1.5.0〜)
;   [パレットあり] 8パレット x 16色 x 2バイト (16パレットなら 16 x 16 x 2)
;   [全面描き直し] (偶数の番地にそろえる 0〜1 バイト) マップ 504 語 (28 x 18、行の順) + タイル 504 x 32 バイト。
;                  一般 DMA で写す (gdma_vseg)。下の「繰り返し」はない。
;   コマは偶数の番地から始まる。間の詰め物は 0x40 (読み飛ばす)。
;   count の bit7 = まとめ書き (1.4.0 の後): n = count & 0x7F 個のマップ値 (u16)、偶数にそろえる 0〜1 バイト、
;                  n x 32 バイトのタイル (普通のタイルだけ)。タイルは一般 DMA で写す。
;   繰り返し(タイル番号 < 504 の間): u8 skip, u8 count, count x (u16 マップ値, 32バイトのタイル)
;   マップ値の bit15 が立っていれば1色タイル: 32バイトの代わりに1バイト(全バイト共通の値)
; ===========================================================================
bits 16
cpu 186
org 0

IVT_BASE    equ 0x20
STACK_TOP   equ 0x0800
MAP2        equ 0x0800      ; スクリーン2 (時間表示) のマップ
MAPA        equ 0x1000
MAPB        equ 0x1800
TILEA       equ 0x4000
TILEB       equ 0x8000
NTILES      equ 504
PALSTAGE    equ 0xFC00      ; 次のコマの16パレットの置き場 (画面を切り替えるときに 0xFE00 へ写す)
MAPA_SEL    equ (MAPA>>11)|((MAP2>>11)<<4)
MAPB_SEL    equ (MAPB>>11)|((MAP2>>11)<<4)
; 時間表示: 映像が使わないタイル 504-511 (A側) と 1016-1023 (B側) に文字を置き、
; パレット7 の色14(黒)・色15(白) で描く (変換側がこの2色を固定している)
OSD_PAL     equ 7
OSD_ROW     equ 16
OSD_BLANK   equ (504+5)|0x2000          ; 透明なタイル
OSD_SHOW    equ 150                     ; 表示しておく VBlank 数 (約2秒)

%ifdef NILE
VSEG        equ 0x3000
ASEG        equ 0x2000
VR0         equ 16          ; 映像リング: PSRAM バンク 16-111
VR_END      equ 112
VRN         equ VR_END-VR0
AR0         equ 112         ; 音声リング: PSRAM バンク 112-127
AR_END      equ 128
ARN         equ AR_END-AR0
RXBANK      equ 510         ; SPI 受信バッファ (ROM バンク)
TXBANK      equ 15          ; SPI 送信バッファ (RAM バンク)
EXTTAB      equ 0x2000      ; ファイルの連続区間の表 (8バイト x 192。0x2600 からは 8 ラインの帯のパレット A、0x3800 からは音のリング)
EXTMAX      equ 192         ; (2026-11-26 深夜に 768 → 192。ふつうに写したファイルは 1〜数区間)
SECBUF      equ 0xC000      ; FAT 用 512 バイト
HDR         equ 0xC200      ; .WSV のヘッダー (最大 31 セクタ)
HDRMAX      equ 20          ; 0xC200 から 20 セクタ (0xEA00 から帯のパレット、0xFC00 からはパレットの置き場)
                            ; (2026-11-26 に 29 → 20。.WSV は約 4.9GB まで)
BANDA       equ 0xEA00      ; ラスター: バッファ A のコマの帯のパレット (9 帯 x 8 パレット x 32 バイト = 2304)
BANDB       equ 0xF300      ; 同 バッファ B
BAND8A      equ 0x2600      ; 8 ラインの帯 (18 帯 x 8 パレット x 32 バイト = 4608): バッファ A (0x2600-0x37FF)
BAND8B      equ 0xEA00      ; 同 バッファ B (0xEA00-0xFBFF、16 ラインの帯の BANDA・BANDB と同じ所)
HDR_ITEMS   equ HDR+0x10
HDR_BITMAP  equ HDR+0x210
PREBUF_V    equ 6           ; 再生開始前に読んでおく映像チャンク数
SPI_READ    equ 0x0200
SPI_WREAD   equ 0x0600
SPI_TF      equ 0x1000
SPI_CART    equ 0x0800
%else
VSEG        equ 0x2000
ASEG        equ 0x3000
%endif
RING        equ 0x3800      ; 音の DMA が読むリング (本体の RAM)
RINGSZ      equ 2048
AFILL       equ 1024        ; VBlank ごとに、リングの中身をこの量まで足す

; 映像バンクを AL にする
%macro SET_VBANK 0
%ifdef NILE
    xor ah,ah
    out 0xD4,ax
%else
    out 0xC2,al
%endif
%endmacro
; 音声バンクを AL にする
%macro SET_ABANK 0
%ifdef NILE
    out 0xC2,al
%else
    out 0xC3,al
%endif
%endmacro

; ---- RAM ------------------------------------------------------------------
absolute 0x0400
TICK:       resw 1      ; VBlankの回数
NEXTT:      resw 1      ; 次に画面を切り替える TICK
PENDING:    resb 1      ; 0以外: VBlankでポート0x07へ書く値
BUF:        resb 1      ; 0=A 1=B (次に書く側)
VBANK:      resb 1
ABANK:      resb 1
VPTR:       resw 1
APTR:       resw 1
FRAMEI:     resw 1
FRAMEN:     resw 1
ITEM:       resb 1
STARTED:    resb 1
KEYS:       resb 1
PREVK:      resb 1
NEWK:       resb 1
VOL:        resb 1
TBASE:      resw 1
MBASE:      resw 1
LATE:       resw 1      ; 間に合わなかったフレーム数(テスト用)
HBLCNT:     resw 1      ; HBlank割り込みの回数(テスト用)
PAUSED:     resb 1
CFGK:       resb 1      ; 映像1コマあたりの VBlank 数
CFGR:       resb 1      ; 音声1サンプルあたりのライン数 (0=なし)
CFGCOUNT:   resb 1      ; 動画の本数
KFW:        resw 1      ; ジャンプ先の間隔 (コマ数、0=ジャンプなし)
KFI:        resw 1      ; ジャンプ先の番号
NEWKY:      resb 1      ; 押された瞬間の Y ボタン (bit0-3 = Y1-Y4)
PREVKY:     resb 1
OVL:        resb 1      ; 時間表示 0=なし 1=時間が来たら消す 2=出しっぱなし
OVLT:       resw 1
SMPLHI:     resw 1      ; 鳴らしたサンプル数の上位 (下位は HBLCNT)
F0:         resw 1      ; 音声を鳴らし始めたときのコマ番号
AVFIX:      resw 1      ; 音ズレを直した回数 (診断用)
LINESW:     resw 1      ; 1画面のライン数 (159 または 200)
PALPEND:    resb 1      ; 1 = 次の切り替えで PALSTAGE のパレットを書く / 2 = 帯のパレット (ラスター) に切り替える
AHOLD:      resb 1      ; 1 = 音声の読み込み待ち (無音)
XLUT:       resb 32     ; 色の少ないタイルの展開に使う表 (2色: 16語 / 4色: 16バイト)
ASDMA:      resb 1      ; 0 = 割り込みで鳴らす / それ以外 = 音の DMA のポート0x52 の値
ARUN:       resb 1      ; 1 = 音の DMA が動いている (VBlank で足す)
ARESET:     resb 1      ; 1 = 次の audio_start でリングを作り直す
AWP:        resw 1      ; リングの書き込み位置
ACOPY:      resd 1      ; リングに写した本物のサンプル数 (鳴らした数 = これ - リングの残り)
%ifdef NILE
SPICFG:     resw 1      ; SPI_CNT の装置・クロックのビット
CARDBLK:    resb 1      ; 1 = ブロック単位のアドレス (SDHC/SDXC)
OPEN:       resb 1      ; 1 = 複数ブロック読み出し(CMD18)の途中
NEXTLBA:    resd 1
CMDARG:     resd 1
PART:       resd 1
FATLBA:     resd 1
DATALBA:    resd 1
SPC:        resb 1      ; 1クラスタのセクタ数
SPCSH:      resb 1
CLUS:       resd 1
RUNST:      resd 1
RUNLEN:     resd 1
FATCACHE:   resd 1
NEXT:       resw 1      ; 区間の数
CUREXT:     resw 1
CURBASE:    resd 1
FC:         resd 1      ; 次に読むチャンク (ファイル内の番号)
SECIN:      resb 1      ; チャンク内の次のセクタ (0-127)
LTYPE:      resb 1      ; 読み込み中のチャンクの種類 0=映像 1=音声
ITEMEND:       resd 1      ; この動画の終わりのチャンク
VL:         resw 1      ; 読み込み済みの映像チャンク数 (この動画の中で)
VCI:        resw 1      ; 再生中の映像チャンク
ALD:        resw 1      ; 読み込み済みの音声チャンク数
ACI:        resw 1      ; 再生中の音声チャンク (割り込みで進む)
VLSLOT:     resb 1
ALSLOT:     resb 1
DSTBANK:    resb 1
DSTPS:      resb 1      ; 1 = PSRAM へ、0 = 内蔵RAM へ
DSTOFF:     resw 1
ERRCODE:    resb 1
RETRY:      resb 1
NSECT:      resw 1      ; 読んだセクタ数 (テスト用)
STALL:      resw 1      ; 映像の読み込み待ちが起きた回数 (テスト用)
TOUT:       resw 1
STAGE:      resb 1      ; 進み具合 (エラー画面に出す)
VORD:       resw 1      ; 次に出会う映像チャンクの番号 (この動画の中で)
AORD:       resw 1
VSKIP:      resw 1      ; これより前の番号の映像チャンクは読み飛ばす (ジャンプ用)
ASKIP:      resw 1
PIPE:       resb 1      ; 1 = セクタの後半を次の読み込みと並行して写す (stream_step の中)
PEND:       resb 1      ; 1 = 受信バッファに写していない後半 (368バイト) がある
PENDPS:     resb 1
PENDBANK:   resb 1
PENDOFF:    resw 1
TOKF:       resb 1      ; 1 = trick の読み方の途中 (カードは前のセクタの CRC を送る前で止まっている。OLDPIPE では「次の印を待つ受信を出してある」)
FPEND:      resb 1      ; 1 = 受信バッファにまだ PSRAM へ写していないセクタがある (trick の読み方)
FPBANK:     resb 1
FPOFF:      resw 1
; ラスター (2026-11-26)。テストの道具が決まった番地を読むので、新しい変数は後ろに足す
RASTER:     resb 1      ; 1 = 表示中のコマは帯のパレット (VBlank とラインの割り込みでパレットを書き換える)
RK:         resb 1      ; ラスター: 次に写す帯の番号 (2〜8。-DBAND8 は組の番号 4〜17)
NEXTBAND:   resw 1      ; 次に表示するコマの帯のパレット (BANDA / BANDB)
DISPBAND:   resw 1      ; 表示中のコマの帯のパレット
; 音 (2026-11-26)
AFLG:       resb 1      ; .WSV の音の設定: bit7 = 1 回 2 バイト (音の DMA 24kHz)、bit6 = ステレオ (左右交互)
AHV:        resb 1      ; 1 = ハイパーボイスで鳴らしている (ステレオ・ヘッドホン)
%ifdef BENCH
BS0:        resw 1      ; 速さの測定用 (tests/bench: -DBENCH)
BT0:        resw 1
BS1:        resw 1
BT1:        resw 1
BF2:        resw 1
BT2:        resw 1
BRES:       resw 32     ; 動画ごとの結果 (最大 8 本 x BS1・BT1・BF2・BT2)
%endif
%ifdef STATS
SRES:       resw 32     ; 実機の試し用 (-DSTATS): 動画ごとの コマ数・遅れ・読み込み待ち・音ズレ補正
%endif
%endif

section .text
; ---- 先頭: エントリと設定表 ---------------------------------------------
    jmp start
    times 16-($-$$) db 0
config:
    db 'WSVP'
cfg_k:      db 5        ; 映像1フレームあたりの VBlank 数 (nileswan 版は .WSV から)
cfg_r:      db 1        ; 音声1サンプルあたりのライン数 (0=音声なし)
cfg_flags:  db 0
cfg_count:  db 1        ; 動画の本数
cfg_out:    db 0x09     ; ポート0x91 のシフト以外 (スピーカー有効・ヘッドホン有効)
cfg_vvol:   db 0x05     ; ポート0x94 (左右100%)
cfg_vol:    db 1        ; 内蔵スピーカーのシフト初期値 (0=100% 1=50% 2=25% 3=12.5%)
cfg_minsh:  db 1        ; 上ボタンで上げられる上限 (シフトの最小値)。0 は音割れするので普通は 1
cfg_kf:     dw 0        ; ジャンプ先の間隔 (コマ数)。通常版のみ (nileswan 版は .WSV から)
cfg_vtot:   db 0x9E     ; ポート0x16 (1画面のライン数-1)。158=75.47Hz、199=60Hz (実験的)。通常版のみ
    times 32-($-$$) db 0
%ifdef NILE
; nileswan 版: microSD のルートにある動画データファイルの 8.3 名 (変換時に書き込む)
cfg_name:   db 'VIDEO   WSV'
    times 64-($-$$) db 0
%else
; items: 1本 8バイト x 64本
;   +0 映像バンク, +1 映像オフセット(w), +3 音声バンク, +4 音声オフセット(w), +6 フレーム数(w)
items:
    times 64*8 db 0
; 動画ごとのジャンプ表の始まり (u16 x 64)。ジャンプ表そのものは 0x8000 から 8バイトずつ:
;   +0 映像バンク, +1 映像オフセット(w), +3 音声バンク, +4 音声オフセット(w)
seekidx:
    times 64*2 db 0
SEEKTAB     equ 0x8000
%endif

maptab:
%assign t 0
%rep NTILES
    dw ((t / 28) * 32 + (t - (t / 28) * 28)) * 2
%assign t t+1
%endrep

; ---- 起動 -----------------------------------------------------------------
start:
    cli
    cld
    xor ax,ax
    mov ss,ax
    mov sp,STACK_TOP
    mov ds,ax
    mov es,ax
    out 0x00,al             ; 画面表示オフ
    out 0xB2,al             ; 割り込みオフ
    out 0xA2,al             ; タイマー停止
    mov al,0xE0             ; カラー・4bpp・パックド
    out 0x60,al
    xor di,di
    xor ax,ax
    mov cx,32768
    rep stosw               ; RAM全消去(タイル・マップ・パレットも)
    mov byte [ARESET],1
    ; 割り込みベクタ
    mov bx,IVT_BASE*4
    mov cx,8
.vec:
    mov word [bx],isr_dummy
    mov [bx+2],cs
    add bx,4
    loop .vec
    mov word [(IVT_BASE+6)*4],isr_vbl
    mov word [(IVT_BASE+7)*4],isr_hbl
%ifdef NILE
    mov word [(IVT_BASE+4)*4],isr_line
%endif
    mov al,IVT_BASE
    out 0xB0,al
    ; 画面
    xor al,al
    out 0x01,al             ; 背景色 = パレット0の色0 (黒)
    out 0x10,al
    out 0x11,al
    out 0x12,al
    out 0x13,al
    mov al,MAPB_SEL
    out 0x07,al             ; B(空)を表示し、最初のフレームはAに書く
    mov byte [BUF],0
    mov al,0x9E
%ifndef NILE
    mov al,[cs:cfg_vtot]
%endif
    call set_vtotal         ; 1フレームのライン数 (nileswan 版は .WSV を読んでから決め直す)
    mov al,1
    out 0x14,al             ; LCDオン
    call osd_init
    ; 音
    mov al,[cs:cfg_vol]
    mov [VOL],al
    call set_speaker
    mov al,3
    out 0x9E,al             ; 本体の主音量は最大のまま (音量はシフトで変える)
    mov al,[cs:cfg_vvol]
    out 0x94,al
    mov al,0x22             ; ch2 ボイスモード
    out 0x90,al
    mov al,0x80
    out 0x89,al
    ; 割り込み
    mov al,0xFF
    out 0xB6,al
    mov al,0x40             ; VBlank
    out 0xB2,al
    sti
    mov al,1
    out 0x00,al             ; スクリーン1表示
    mov byte [ITEM],0
%ifdef NILE
    call nile_boot          ; microSD を準備し .WSV のヘッダーを読む (CFGK など)
%else
    mov al,[cs:cfg_k]
    mov [CFGK],al
    mov al,[cs:cfg_r]
    mov [CFGR],al
    mov al,[cs:cfg_count]
    mov [CFGCOUNT],al
    mov ax,[cs:cfg_kf]
    mov [KFW],ax
%endif

; ---- 1本の再生 -------------------------------------------------------------
play_item:
    call audio_stop
    call osd_hide
%ifdef NILE
    call nile_item_start    ; 読み込み開始・先読み (画面Aに LOADING を出す)
    ; 読み込み中の文字を出していたAを隠し、Bを表示してから始める
    mov al,MAPB_SEL
    out 0x07,al
    mov byte [BUF],0
    mov al,VR0
    mov [VBANK],al
    mov word [VPTR],0
    mov al,AR0
    mov [ABANK],al
    mov word [APTR],0
    mov bl,[ITEM]
    xor bh,bh
    shl bx,3
    mov ax,[HDR_ITEMS+bx+4]
    mov [FRAMEN],ax
%ifdef BENCH
    jmp bench
%endif
%ifdef STATS
    mov word [LATE],0
    mov word [STALL],0
    mov word [AVFIX],0
%endif
%else
    ; どの動画も最初のフレームはバッファAに書く(変換側と同じ約束)。
    call ensure_buf_a
    mov bl,[ITEM]
    xor bh,bh
    shl bx,3
    add bx,items
    mov al,[cs:bx]
    mov [VBANK],al
    mov ax,[cs:bx+1]
    mov [VPTR],ax
    mov al,[cs:bx+3]
    mov [ABANK],al
    mov ax,[cs:bx+4]
    mov [APTR],ax
    mov ax,[cs:bx+6]
    mov [FRAMEN],ax
%endif
    mov word [FRAMEI],0
    mov byte [STARTED],0
frame_loop:
    mov ax,[FRAMEI]
    cmp ax,[FRAMEN]
    jae item_end
    call decode_frame
    cmp byte [STARTED],0
    jne .req
    cli
    mov word [TICK],0
    mov word [NEXTT],1
    mov word [HBLCNT],0
    mov word [SMPLHI],0
    mov word [ACOPY],0
    mov word [ACOPY+2],0
    mov byte [ARESET],1
    mov ax,[FRAMEI]
    mov [F0],ax
    mov word [OVLT],OSD_SHOW    ; TICK を 0 に戻したので、時間表示を消す時刻も数え直す
    sti
    call audio_start
    mov byte [STARTED],1
.req:
    cli
    mov ax,[TICK]
    sub ax,[NEXTT]
    js .ontime
    inc word [LATE]
.ontime:
    mov al,MAPA_SEL
    cmp byte [BUF],0
    je .seta
    mov al,MAPB_SEL
.seta:
    mov [PENDING],al
    sti
.wait:
%ifdef NILE
    call stream_step        ; 待ち時間に microSD から読む
    or al,al
    jnz .chk
%endif
    hlt
.chk:
    cmp byte [PENDING],0
    jne .wait
    xor byte [BUF],1
    inc word [FRAMEI]
    call av_sync
    call handle_keys
    jmp frame_loop

; ---------------------------------------------------------------------------
; 音ズレ補正: 映像の位置から「今鳴っているはずのサンプル数」を出し、
; 実際に鳴らした数 (割り込みで数える) より 3000 サンプル以上遅れていたら、
; 音声の読み出し位置をその分だけ進める。
; (実機で HBlank 割り込みが取りこぼされると音声だけが遅れていくため)
; ---------------------------------------------------------------------------
av_sync:
    cmp byte [CFGR],0
    je .ret
    ; 期待値 E = (FRAMEI - F0) * K * 159 / R  (DI:SI, 32bit)
    mov ax,[FRAMEI]
    sub ax,[F0]
    jbe .ret
    mov cl,[CFGK]
    xor ch,ch
    mul cx                  ; DX:AX = コマ x K
    mov bx,dx
    mov cx,[LINESW]
    mul cx
    mov si,ax
    mov di,dx
    mov ax,bx
    mul cx
    add di,ax               ; DI:SI = x ライン数
    mov cl,[CFGR]
    xor ch,ch
    cmp cx,1
    je .div
    mov ax,di
    xor dx,dx
    div cx
    mov bx,ax
    mov ax,si
    div cx
    mov si,ax
    mov di,bx
.div:
%ifdef NILE
    test byte [AFLG],0x80   ; 1 回 2 バイト (24kHz・ステレオ): 期待値も 2 倍のバイト数
    jz .one
    shl si,1
    rcl di,1
.one:
%endif
    cli
    mov ax,si
    mov dx,di
    sub ax,[HBLCNT]
    sbb dx,[SMPLHI]
    js .back                ; 音声の方が先 (映像が遅れている) → 何もしない
    jnz .fix
%ifdef NILE
    test byte [AFLG],0x80   ; 1 回 2 バイトの音は、同じ時間 (0.25 秒) にするため 6000 バイト
    jz .th1
    cmp ax,6000
    jb .back
    jmp .fix
.th1:
%endif
    cmp ax,3000
    jb .back
.fix:
    ; DX:AX サンプル進める (音の DMA の一般 DMA は偶数の番地しか読めないので偶数にする)
    and ax,0xFFFE
%ifdef NILE
    ; 進めた先のチャンクがまだ読み込まれていなければ、今回は直さない
    mov bx,[APTR]
    add bx,ax
    mov cx,dx
    adc cx,0
    add cx,[ACI]
    cmp cx,[ALD]
    jae .back
    cmp byte [AHOLD],0
    jne .back
%endif
    add [HBLCNT],ax
    adc [SMPLHI],dx
    add [ACOPY],ax          ; 音の DMA のとき: HBLCNT は VBlank ごとに ACOPY から計算し直す
    adc [ACOPY+2],dx
    add [APTR],ax
    adc dx,0                ; DX = 進めるバンク数
    inc word [AVFIX]
%ifdef NILE
    add [ACI],dx
    mov ax,[ACI]
    and al,ARN-1
    add al,AR0
    mov [ABANK],al
%else
    add [ABANK],dl
    mov al,[ABANK]
%endif
    SET_ABANK
.back:
    sti
.ret:
    ret

; AL = ポート0x16 の値 (ライン数-1)。LINESW も合わせる
set_vtotal:
    out 0x16,al
    push ax
    sub al,3                ; 垂直同期 (バックポーチ) の位置も合わせる (WSC のみ。既定 155 = 158-3)
    out 0x17,al             ; IPS 化液晶や TV スワンはこの位置を画面の始まりとして扱う
    pop ax
    xor ah,ah
    inc ax
    mov [LINESW],ax
    ret

; 次に書くバッファを A にする (Aを表示中なら Bに切り替えてから)
ensure_buf_a:
    cmp byte [BUF],0
    je .ok
    cli
    mov ax,[TICK]
    mov [NEXTT],ax
    mov byte [PENDING],MAPB_SEL
    sti
.flip:
    hlt
    cmp byte [PENDING],0
    jne .flip
    mov byte [BUF],0
.ok:
    ret

item_end:
%ifdef NILE
    call raster_off         ; 動画の切れ目 (結果の表・次の動画の読み込み) ではラスターを止める
%endif
%ifdef STATS
    jmp stats_item_end
%endif
    mov al,[ITEM]
    inc al
    cmp al,[CFGCOUNT]
    jb .ok
    xor al,al
.ok:
    mov [ITEM],al
    jmp play_item

; ---- ボタン ---------------------------------------------------------------
; KEYS: bit4-7 = X1-X4, bit1 START, bit2 A, bit3 B。NEWKY: bit0-3 = Y1-Y4
handle_keys:
    call take_keys
    mov dx,ax               ; DL = ボタン・X, DH = Y
    test al,0x02|0x04       ; START / A : 一時停止
    jz .nopause
    call do_pause           ; AH = 一時停止中に押された Y
    mov dh,ah
    xor dl,dl
.nopause:
    ; Y ボタン: ジャンプ (Y2/Y4 = 10秒進む/戻る、Y1/Y3 = 1分進む/戻る)
    xor bx,bx
    test dh,0x02
    jz .y4
    mov bx,1
.y4:
    test dh,0x08
    jz .y1
    mov bx,-1
.y1:
    test dh,0x01
    jz .y3
    mov bx,6
.y3:
    test dh,0x04
    jz .yok
    mov bx,-6
.yok:
    or bx,bx
    jz .noseek
    add sp,2
    jmp seek_rel
.noseek:
    mov al,dl
    test al,0x08            ; B : 最初から
    jz .nob
    add sp,2
    jmp play_item
.nob:
    test al,0x20            ; X2(右) : 次の動画
    jz .nonext
    add sp,2
    jmp item_end
.nonext:
    test al,0x80            ; X4(左) : 前の動画
    jz .noprev
    add sp,2
    mov al,[ITEM]
    or al,al
    jnz .dec
    mov al,[CFGCOUNT]
.dec:
    dec al
    mov [ITEM],al
    jmp play_item
.noprev:
    ; 内蔵スピーカーの音量 = ポート0x91 のシフト (VOL、小さいほど大きい音)
    test al,0x10            ; X1(上) : 音量+
    jz .noup
    mov al,[VOL]
    cmp al,[cs:cfg_minsh]
    jbe .noup
    dec byte [VOL]
    call set_speaker
.noup:
    mov al,dl
    test al,0x40            ; X3(下) : 音量-
    jz .nodn
    cmp byte [VOL],3
    jae .nodn
    inc byte [VOL]
    call set_speaker
.nodn:
    ret

; ポート0x91 = cfg_out | (VOL << 1)
set_speaker:
    mov al,[VOL]
    and al,3
    shl al,1
    or al,[cs:cfg_out]
    out 0x91,al
%ifdef NILE
    cmp byte [AHV],0
    jne hv_setup
%endif
    ret

%ifdef NILE
; ハイパーボイス (2026-11-26): ステレオをヘッドホンで鳴らすときだけ有効にする。
; 符号なし (0x80 が無音)。音量はシフト = VOL (0 は 1 にする。シフト 0 は 16 ビットをはみ出して割れる)
hv_setup:
    cmp byte [AHV],0
    je .off
    mov al,[VOL]
    and al,3
    jnz .s
    mov al,1
.s:
    or al,0x80              ; 有効・符号なし・分周 0
    out 0x6A,al
    xor al,al               ; ステレオ (左右交互)
    out 0x6B,al
    ret
.off:
    xor al,al
    out 0x6A,al
    ret
%endif

; 一時停止。時間を出しっぱなしにする。START/A で再開、Y を押したら再開してジャンプ。
; 戻り値 AH = 押された Y ボタン
do_pause:
    call audio_stop
    mov byte [PAUSED],1
    mov ax,[FRAMEI]
    dec ax
    jns .t
    xor ax,ax
.t:
    mov byte [OVL],2
    call osd_time
    call osd_guide          ; 「A:サイセイ B:メニュー」
.lp:
%ifdef NILE
    call osd_buf            ; 読み込み状況 (一時停止中も読み込みは続く)
    call stream_step
    or al,al
    jnz .chk
%endif
    hlt
.chk:
    call take_keys
%ifdef NILE
    test al,0x08            ; 一時停止中の B : nileswan のメニューへ戻る
    jz .nomenu
    jmp menu_return
.nomenu:
%endif
    or ah,ah
    jnz .go
    test al,0x02|0x04
    jz .lp
.go:
    push ax
    call osd_guide_off      ; 借りたタイルを元に戻す (次のコマを展開する前に)
    mov byte [PAUSED],0
    cli
    mov ax,[TICK]
    add al,[CFGK]
    adc ah,0
    mov [NEXTT],ax
    add ax,OSD_SHOW/2
    mov [OVLT],ax
    mov byte [OVL],1
    sti
    call audio_start
    pop ax
    ret

; ---- ジャンプ -----------------------------------------------------------------
; BX = 何個先のジャンプ先へ行くか (負なら戻る)。最後は frame_loop へ。
seek_rel:
    mov cx,[KFW]
    jcxz .none
    mov ax,[FRAMEI]
    xor dx,dx
    div cx                  ; AX = 今いるジャンプ区間
    add ax,bx
    jns .pos
    xor ax,ax
.pos:
    push ax
    mul cx
    or dx,dx
    jnz .past
    cmp ax,[FRAMEN]
    jae .past
    pop ax
    jmp seek_to
.past:
    pop ax                  ; 終わりより先には飛ばない
.none:
    jmp frame_loop

; AX = ジャンプ先の番号。その位置から再生し直す
seek_to:
    mov [KFI],ax
    call audio_stop
    mov ax,[KFI]
    mul word [KFW]
    mov [FRAMEI],ax
    mov byte [OVL],2        ; 読み込み中も時間を出しておく
    call osd_time
%ifdef NILE
    call nile_seek
%else
    mov bl,[ITEM]
    xor bh,bh
    shl bx,1
    mov bx,[cs:seekidx+bx]
    add bx,[KFI]
    shl bx,3
    add bx,SEEKTAB
    mov al,[cs:bx]
    mov [VBANK],al
    mov ax,[cs:bx+1]
    mov [VPTR],ax
    mov al,[cs:bx+3]
    mov [ABANK],al
    mov ax,[cs:bx+4]
    mov [APTR],ax
%endif
    call ensure_buf_a       ; ジャンプ先のコマは偶数番 = バッファA
    cli
    mov ax,[TICK]
    add ax,OSD_SHOW
    mov [OVLT],ax
    mov byte [OVL],1
    sti
    mov byte [STARTED],0
    jmp frame_loop

; VBlank割り込みから呼ぶ。押された瞬間のボタンを NEWK / NEWKY に貯める。
input:
    mov al,0x20
    out 0xB5,al
    nop
    nop
    nop
    in al,0xB5
    and al,15
    mov ah,al
    shl ah,4
    mov al,0x40
    out 0xB5,al
    nop
    nop
    nop
    in al,0xB5
    and al,14
    or al,ah
    mov ah,[ss:PREVK]
    mov [ss:PREVK],al
    mov [ss:KEYS],al
    not ah
    and al,ah
    or [ss:NEWK],al
    mov al,0x10             ; Y ボタン
    out 0xB5,al
    nop
    nop
    nop
    in al,0xB5
    and al,15
    mov ah,[ss:PREVKY]
    mov [ss:PREVKY],al
    not ah
    and al,ah
    or [ss:NEWKY],al
    ret

; AL = 貯まったボタン, AH = 貯まった Y ボタン (どちらも 0 に戻す)
take_keys:
    cli
    mov al,[NEWK]
    mov ah,[NEWKY]
    mov byte [NEWK],0
    mov byte [NEWKY],0
    sti
    ret

; ---- 時間表示 (スクリーン2) -------------------------------------------------
osd_init:
    pusha
    push ds
    push es
    xor ax,ax
    mov es,ax
    push cs
    pop ds
    mov si,osdfont          ; 0-7 → タイル 504-511、8 9 : / 空白 透明 → 1016-1021
    mov di,TILEA+504*32
    mov cx,8*16
    rep movsw
    mov di,TILEB+504*32
    mov cx,6*16
    rep movsw
    pop es
    pop ds
    mov di,MAP2
    mov ax,OSD_BLANK
    mov cx,1024
    rep stosw
    popa
    ret

osd_hide:
    mov byte [OVL],0
    mov al,1
    out 0x00,al
    ret

; AX = コマ番号。「 MM:SS/MM:SS 」(今 / 全体) を表示する
osd_time:
    pusha
    push ax                 ; AX = コマ番号 (あとで使う)
    mov di,MAP2+((OSD_ROW+1)*32+1)*2   ; 2行目 (読み込み状況) を消す
    mov ax,OSD_BLANK
    mov cx,14
    push es
    push ds
    pop es
    rep stosw
    pop es
    pop ax
    mov di,MAP2+(OSD_ROW*32+1)*2
    mov dx,12               ; 空白
    call .cell
    call .mmss
    mov dx,11               ; /
    call .cell
    mov ax,[FRAMEN]
    call .mmss
    mov dx,12
    call .cell
    mov al,3
    out 0x00,al             ; スクリーン2 も表示
    popa
    ret
; AX = コマ番号 → 分:秒 を書く
.mmss:
    push ax
    mov al,[CFGK]           ; 秒 = コマ x K x ライン数 / 12000
    xor ah,ah
    mul word [LINESW]
    mov cx,ax
    pop ax
    mul cx
    mov cx,12000
    div cx
    xor dx,dx
    mov cx,60
    div cx                  ; AX = 分, DX = 秒
    push dx
    cmp ax,99
    jbe .m
    mov ax,99
.m:
    mov cl,10
    div cl                  ; AL = 十の位, AH = 一の位
    push ax
    mov dl,al
    xor dh,dh
    call .cell
    pop ax
    mov dl,ah
    xor dh,dh
    call .cell
    mov dx,10               ; :
    call .cell
    pop ax
    mov cl,10
    div cl
    push ax
    mov dl,al
    xor dh,dh
    call .cell
    pop ax
    mov dl,ah
    xor dh,dh
    jmp .cell
; DX = 文字 (0-9, 10=':', 11='/', 12=空白) を DI に書いて進む
.cell:
    push ax
    mov ax,dx
    cmp ax,8
    jb .lo
    add ax,504-8
    or ax,0x2000
    jmp .w
.lo:
    add ax,504
.w:
    or ax,OSD_PAL<<9
    mov [di],ax
    add di,2
    pop ax
    ret

%ifdef NILE
; 一時停止中の2行目: 先に読み込めている 映像チャンク数 / 音声チャンク数 (1チャンク = 64KB)
osd_buf:
    pusha
    mov di,MAP2+((OSD_ROW+1)*32+1)*2
    mov dx,12
    call osd_time.cell
    mov ax,[VL]
    sub ax,[VCI]
    call .two
    mov dx,11
    call osd_time.cell
    mov ax,[ALD]
    sub ax,[ACI]
    call .two
    mov dx,12
    call osd_time.cell
    popa
    ret
.two:
    cmp ax,0x8000
    jb .p
    xor ax,ax
.p:
    cmp ax,99
    jbe .t
    mov ax,99
.t:
    mov cl,10
    div cl
    push ax
    mov dl,al
    xor dh,dh
    call osd_time.cell
    pop ax
    mov dl,ah
    xor dh,dh
    jmp osd_time.cell
%endif

; ---- 一時停止中の操作の案内 (時間の1行上) -----------------------------------
; 文字のタイルは空きがないので、案内を出すマスの映像のタイル (A・B 両方の同じ番号) を一時的に借りる。
; そのマスはスクリーン2 の不透明な文字で覆うので、どちらのバッファを表示していても文字だけが見える。
; 元のタイルは MAP2 の見えない行 (18行目以降) に退避し、再開時に戻す。
GUIDE_ROW   equ OSD_ROW-1
GUIDESAVE   equ MAP2+18*64          ; 退避先 (1文字 64 バイト、最大 14 文字 = 896 バイト)
osd_guide:
    pusha
    push es
    xor ax,ax
    mov es,ax
    mov si,guide_cells
    mov di,MAP2+(GUIDE_ROW*32+1)*2
    mov bx,GUIDE_ROW*28+1   ; そのマスの映像のタイル番号
    mov bp,GUIDESAVE
.lp:
    xor ax,ax
    mov al,[cs:si]
    inc si
    cmp al,0xFF
    je .done
    test al,0x80
    jnz .glyph
    mov dx,ax               ; ':' と空白は時間表示の文字を使う
    call osd_time.cell
    jmp .next
.glyph:
    and al,0x7F
    push si
    push di
    mov si,bx
    shl si,5
    add si,TILEA
    mov di,bp
    mov cx,16
    rep movsw               ; A のタイルを退避
    add si,TILEB-TILEA-32
    mov cx,16
    rep movsw               ; B のタイルを退避
    mov bp,di
    mov di,bx
    shl di,5
    add di,TILEA
    shl ax,5
    add ax,guidefont
    push ds
    push cs
    pop ds
    mov si,ax
    mov cx,16
    rep movsw               ; A に文字を書く
    add di,TILEB-TILEA-32
    mov si,ax
    mov cx,16
    rep movsw               ; B にも同じ文字
    pop ds
    pop di
    pop si
    mov ax,bx
    or ax,OSD_PAL<<9
    mov [di],ax
    add di,2
.next:
    inc bx
    jmp .lp
.done:
    pop es
    popa
    ret

osd_guide_off:
    pusha
    push es
    xor ax,ax
    mov es,ax
    mov si,guide_cells
    mov di,MAP2+(GUIDE_ROW*32+1)*2
    mov bx,GUIDE_ROW*28+1
    mov bp,GUIDESAVE
.lp:
    mov al,[cs:si]
    inc si
    cmp al,0xFF
    je .done
    test al,0x80
    jz .blank
    push si
    push di
    mov si,bp
    mov di,bx
    shl di,5
    add di,TILEA
    mov cx,16
    rep movsw
    add di,TILEB-TILEA-32
    mov cx,16
    rep movsw
    mov bp,si
    pop di
    pop si
.blank:
    mov word [di],OSD_BLANK
    add di,2
    inc bx
    jmp .lp
.done:
    pop es
    popa
    ret

; 案内のマス: 0x80+n = guidefont の n 番目、10 = ':'、12 = 空白、0xFF = 終わり
guide_cells:
    db 12, 0x80, 10, 0x81, 0x82, 0x83, 0x84
%ifdef NILE
    db 12, 0x85, 10, 0x86, 0x87, 0x88, 0x89
%endif
    db 12, 0xFF
guidefont:
    incbin "guidefont.bin"

osdfont:
    incbin "osdfont.bin"
; ---- 音声 -----------------------------------------------------------------
audio_start:
    cmp byte [CFGR],0
    je .ret
%ifndef NOSDMA
    ; R=1/2/3 → 音の DMA 12k/6k/4kHz (ポート0x52 = 動かす | くり返し | 頻度)
    mov byte [ASDMA],0
    mov al,[CFGR]
    cmp al,3
    ja .irq
    mov ah,0x8B
    sub ah,al
%ifdef NILE
    ; 1 回 2 バイト: 音の DMA を 1 段速く (R=1 → 24kHz)。ステレオでヘッドホン (TV スワンも) なら、ハイパーボイスへ送る
    mov byte [AHV],0
    test byte [AFLG],0x80
    jz .sd1
    inc ah
    test byte [AFLG],0x40
    jz .sd1
    in al,0x91
    test al,0x80            ; ヘッドホンがつながっている
    jz .sd1
    or ah,0x10              ; 音の DMA の行き先 = ハイパーボイス
    mov byte [AHV],1
.sd1:
%endif
    mov [ASDMA],ah
    cli
%ifdef NILE
    mov al,[ABANK]
    xor ah,ah
    out 0xD2,ax
%else
    mov al,[ABANK]
    SET_ABANK
%endif
    cmp byte [ARESET],0
    je .resume
    ; リングを作り直す: 音の DMA の読み出しをリングの先頭に置き、先に AFILL 分を入れる
    mov byte [ARESET],0
    and word [APTR],0xFFFE
    mov word [AWP],0
    mov ax,RING
    out 0x4A,ax
    xor al,al
    out 0x4C,al
    mov ax,RINGSZ
    out 0x4E,ax
    xor al,al
    out 0x50,al
    call atopup
    ; 鳴らし始め (再生の最初・ジャンプ) の 128 サンプル (12kHz で約 10ms) を無音から立ち上げる。
    ; 波形の途中からいきなり鳴らすと「プツッ」と聞こえるため (2026-11-25 実機)
    push bx
    push dx
    xor bx,bx
.fade:
    mov al,[RING+bx]
    sub al,0x80
    cbw
    imul bx                 ; DX:AX = サンプル x i
    sar ax,7
    add al,0x80
    mov [RING+bx],al
    inc bx
    cmp bx,128
    jb .fade
    pop dx
    pop bx
%ifdef NILE
    mov al,0x10             ; ハイパーボイス: ステレオ、左右の順を戻す (リングの先頭 = 左)
    out 0x6B,al
%endif
.resume:
%ifdef NILE
    call hv_setup
%endif
    mov al,[ASDMA]
    out 0x52,al
    mov byte [ARUN],1
    sti
    ret
.irq:
%endif
    cli
%ifdef NILE
    mov al,[ABANK]
    xor ah,ah
    out 0xD2,ax             ; 上位ビットも0にする (以後は割り込みが C2 だけ書く)
%else
    mov al,[ABANK]
    SET_ABANK
%endif
    mov al,[CFGR]
    xor ah,ah
    out 0xA4,ax
    mov al,0x03             ; HBlankタイマー 有効・自動リロード
    out 0xA2,al
    mov al,0xC0             ; VBlank + HBlankタイマー
    out 0xB2,al
    sti
.ret:
    ret

audio_stop:
    cli
%ifndef NOSDMA
    xor al,al
    out 0x52,al             ; 音の DMA を止める (読み出し位置とリングはそのまま。再開で続きから)
    mov [ARUN],al
%endif
    mov al,0x40
    out 0xB2,al
    xor al,al
    out 0xA2,al
    mov al,0x80
    out 0x89,al
    out 0xB6,al
    sti
    ret

%ifndef NOSDMA
; ---------------------------------------------------------------------------
; 音のリングを足す (VBlank 割り込みと audio_start から、割り込み禁止で呼ぶ)。
; 音の DMA の今の読み出し位置 rp から見て、リングの中身が AFILL になるまで、
; 音声データ (ASEG の窓の APTR) を一般 DMA で写す。読み込み待ち (AHOLD) の間は無音を入れる。
; 最後に「鳴らした本物のサンプル数」(HBLCNT:SMPLHI) を計算し直す (音ズレ補正 av_sync が使う)。
; ---------------------------------------------------------------------------
atopup:
    pusha
    push ds
    push es
    xor ax,ax
    mov ds,ax
    mov es,ax
.rd:                        ; 読み出し位置 (動いている途中なので、2回読んで同じなら確か)
    in ax,0x4A
    mov bx,ax
    in ax,0x4A
    cmp ax,bx
    jne .rd
    sub ax,RING
    and ax,RINGSZ-1
    mov bp,ax               ; BP = rp
    mov ax,[AWP]
    sub ax,bp
    and ax,RINGSZ-1         ; AX = リングに入っている量
    mov cx,AFILL
    sub cx,ax
    jbe .count
    and cx,0xFFFE           ; CX = 足す量 (偶数)
.lp:
    jcxz .count
    mov dx,cx               ; DX = 今回写す量: リングの終わりまで
    mov ax,RINGSZ
    sub ax,[AWP]
    cmp dx,ax
    jbe .w1
    mov dx,ax
.w1:
    mov di,[AWP]
    add di,RING
    cmp byte [AHOLD],0
    jne .silence
    mov ax,[APTR]           ; 64KB のチャンクの終わりまで (APTR=0 なら制限なし)
    neg ax
    jz .w2
    cmp dx,ax
    jbe .w2
    mov dx,ax
.w2:
    mov ax,[APTR]           ; 一般 DMA: 写し元 = ASEG の窓 + APTR、写し先 = リング
    out 0x40,ax
    mov al,ASEG>>12
    out 0x42,al
    mov ax,di
    out 0x44,ax
    mov ax,dx
    out 0x46,ax
    mov al,0x80
    out 0x48,al
    add [ACOPY],dx
    adc word [ACOPY+2],0
    add [APTR],dx
    jnz .next
    call anext_chunk        ; チャンクの終わり → 次のチャンク
    jmp .next
.silence:
    push cx
    mov cx,dx
    shr cx,1
    mov ax,0x8080
    rep stosw
    pop cx
.next:
    mov ax,[AWP]
    add ax,dx
    and ax,RINGSZ-1
    mov [AWP],ax
    sub cx,dx
    jmp .lp
.count:
    mov ax,[AWP]
    sub ax,bp
    and ax,RINGSZ-1
    mov bx,[ACOPY]
    mov dx,[ACOPY+2]
    sub bx,ax
    sbb dx,0
    mov [HBLCNT],bx
    mov [SMPLHI],dx
    pop es
    pop ds
    popa
    ret

; 音声の次のチャンクへ (isr_hbl のチャンクの終わりと同じ動き)
anext_chunk:
%ifdef NILE
    inc word [ACI]
    mov al,[ABANK]
    inc al
    cmp al,AR_END
    jb .ab
    mov al,AR0
.ab:
    mov [ABANK],al
    SET_ABANK
    mov ax,[ALD]            ; 読み込み済みでなければ、読み込まれるまで無音にする
    cmp ax,[ACI]
    ja .r
    mov byte [AHOLD],1
.r:
%else
    inc byte [ABANK]
    mov al,[ABANK]
    SET_ABANK
%endif
    ret
%endif

; ---- 映像1フレームの展開 ---------------------------------------------------
decode_frame:
%ifdef NILE
    call ensure_video       ; 今のチャンクが読み込み済みになるまで待つ
%endif
    push ds
    mov al,[VBANK]
    SET_VBANK
    mov ax,TILEA
    mov bx,MAPA
    mov di,0xFE00
    cmp byte [BUF],0
    je .bufok
    mov ax,TILEB
    mov bx,MAPB
    mov di,0xFF00
.bufok:
    mov [TBASE],ax
    mov [MBASE],bx
    mov ax,VSEG
    mov ds,ax
    mov si,[ss:VPTR]
.hdr:
    lodsb
    test al,0x40            ; 詰め物 (コマを偶数の番地から始めるため) → 読み飛ばす
    jnz .hdr
    test al,0x80
    jz .hdrok
%ifdef NILE
    inc word [ss:VCI]       ; 次のチャンクへ (リングを一周したら先頭へ)
    mov al,[ss:VBANK]
    inc al
    cmp al,VR_END
    jb .vb
    mov al,VR0
.vb:
    mov [ss:VBANK],al
    push di
    push ds
    xor ax,ax
    mov ds,ax
    call ensure_video
    pop ds
    pop di
    mov al,[ss:VBANK]
    SET_VBANK
%else
    inc byte [ss:VBANK]     ; 次のバンクへ
    mov al,[ss:VBANK]
    SET_VBANK
%endif
    xor si,si
    jmp .hdr
.hdrok:
    push ax                 ; 先頭バイト (bit2 = 全面描き直し。パレットのあとで見る)
%ifdef NILE
    test al,8
    jnz .bands
%endif
    test al,2
    jnz .pal16
    test al,1               ; 8パレット (古い形式): 書く側のバッファの半分へすぐ書く
    jz .nopal
    mov bx,8
.pal:
%rep 16
    movsw
%endrep
    dec bx
    jnz .pal
    jmp .nopal
.pal16:                     ; 16パレット: 画面を切り替えるときに書く (いったん PALSTAGE へ)
    mov di,PALSTAGE
    mov bx,16
.p16:
%rep 16
    movsw
%endrep
    dec bx
    jnz .p16
    mov byte [ss:PALPEND],1
%ifdef NILE
    jmp .nopal
.bands:                     ; 帯のパレット (ラスター、2026-11-26): 72 パレットを書く側の BANDA / BANDB へ
    mov di,BANDA
    cmp byte [ss:BUF],0
    je .bda
    mov di,BANDB
.bda:
    mov [ss:NEXTBAND],di
    test al,0x10
    jnz .b144
    mov bx,72
.bl:
%rep 16
    movsw
%endrep
    dec bx
    jnz .bl
    mov byte [ss:PALPEND],2
    jmp .nopal
.b144:                      ; 8 ラインの帯 (bit4、2026-11-26 深夜): 詰め物 1 バイト、144 パレット (4608 バイト) を一般 DMA で
    inc si
    mov bx,BAND8A
    cmp byte [ss:BUF],0
    je .b8a
    mov bx,BAND8B
.b8a:
    mov [ss:NEXTBAND],bx
    mov cx,9
.bg:
    mov dx,512
    call gdma_vseg
    add bx,512
    loop .bg
    mov byte [ss:PALPEND],3
%endif
.nopal:
    pop ax
    test al,4
    jz .delta
    ; ---- 全面描き直し: マップ 18 行 x 28 語、タイル 504 枚 x 32 バイトが続けて並ぶ。一般 DMA で写す ----
    test si,1               ; 一般 DMA は偶数の番地から (変換側が 1 バイト空けている)
    jz .ev
    inc si
.ev:
    mov bp,2048             ; タイルを分けて写す大きさ (分けるのは、割り込みを長く待たせないため)
    test al,0x10
    jz .ch
    mov bp,512              ; 8 ラインの帯: ラインの割り込みを 2 ラインより待たせない
.ch:
    mov bx,[ss:MBASE]
    mov cx,18
.mrow:
    mov dx,56
    call gdma_vseg          ; マップ 1 行 (28 語) → MBASE + 行 x 64
    add bx,64
    loop .mrow
    mov bx,[ss:TBASE]
    mov cx,NTILES*32        ; タイル 16128 バイト
.tch:
    mov dx,bp
    cmp cx,dx
    jae .tc
    mov dx,cx
.tc:
    call gdma_vseg
    add bx,dx
    sub cx,dx
    jnz .tch
    jmp .done
.delta:
    xor dx,dx
.run:
    cmp dx,NTILES
    jae .done
    lodsb
    xor ah,ah
    add dx,ax
    lodsb
    or al,al
    jz .run
    test al,0x80
    jnz .bulk
    mov bp,ax
    mov di,dx
    shl di,5
    add di,[ss:TBASE]
.tile:
    mov bx,dx
    shl bx,1
    mov bx,[cs:maptab+bx]
    add bx,[ss:MBASE]
    lodsw
    test ah,0xC0
    jnz .special
    mov [es:bx],ax
%rep 16
    movsw                   ; REP を使わない (割り込みを待たせないため)
%endrep
.next:
    inc dx
    dec bp
    jnz .tile
    jmp .run
.special:
    test ah,0x80
    jz .copy
    test ah,0x40
    jz .solid
    ; bit15 と bit14 の両方: 色の少ないタイル (次の1バイト: 1 = 2色, 2 = 4色)
    and ah,0x3F
    mov [es:bx],ax
    lodsb
    cmp al,1
    jne .c4
    call tile_c2
    jmp .next
.c4:
    call tile_c4
    jmp .next
.copy:
    ; コピー: 表示中のもう一方のバッファの (sx, sy) から 8x8 を持ってくる
    and ah,0xBF
    mov [es:bx],ax
    lodsw                   ; AL = sx (偶数), AH = sy
    call tile_copy
    jmp .next
.solid:                     ; 1色だけのタイル: 1バイトで全体を塗る
    and ah,0x7F
    mov [es:bx],ax
    lodsb
    mov ah,al
%rep 16
    stosw
%endrep
    jmp .next
.bulk:
    ; まとめ書き: n 個のマップの値、(偶数にそろえる 0〜1 バイト)、n 枚のタイル (n x 32 バイト) を一般 DMA で
    and ax,0x7F
    mov cx,ax               ; CX = n
    mov bp,ax
    push dx
.bm:
    mov bx,dx
    shl bx,1
    mov bx,[cs:maptab+bx]
    add bx,[ss:MBASE]
    lodsw
    mov [es:bx],ax
    inc dx
    dec bp
    jnz .bm
    pop bx                  ; BX = 始まりのタイル番号
    test si,1
    jz .be
    inc si
.be:
    shl bx,5
    add bx,[ss:TBASE]
    push dx
    mov dx,cx
    shl dx,5
    call gdma_vseg
    pop dx
    jmp .run
.done:
    mov [ss:VPTR],si
    pop ds
    ret

; 一般 DMA: 映像の窓 (VSEG:SI) から本体の RAM の BX へ DX バイト (偶数)。SI は DX 進む。AX を壊す
gdma_vseg:
    ; 2026-11-26: ポートを書く間は割り込みを止める。VBlank の割り込みの音の補充 (atopup) やラスターも
    ; 一般 DMA を使うので、途中で入られると写し元・写し先が書き換わる (まれに 1 コマの絵が乱れる不具合だった)。
    ; 転送そのものは CPU を止めて一度に終わるので、止めるのはポートを書く数命令だけ。
    pushf
    cli
    mov ax,si
    out 0x40,ax
    mov al,VSEG>>12
    out 0x42,al
    mov ax,bx
    out 0x44,ax
    mov ax,dx
    out 0x46,ax
    mov al,0x80
    out 0x48,al
    popf
    add si,dx
    ret

; ---------------------------------------------------------------------------
; 2色のタイル: u8 色 (上位=色0, 下位=色1)、8 バイト (1行 1バイト、bit7 が左端、1 = 色1)
; 4色のタイル: u8 色0色1, u8 色2色3、16 バイト (1行 2バイト、1ドット 2ビット、上位ビットが左)
; DS:SI = データ、ES:DI = 書くタイル (32 進む)。2ドット分の値を表 XLUT から引く (xlat)。
; ---------------------------------------------------------------------------
tile_c2:
%ifndef OLDC2
    ; 2026-11-26: 固定の表 c2exp (1バイト = 8ドット → 4バイト、1 のドットは 0xF) を引き、
    ; 出力 = 色0 x 0x1111 XOR (表 AND (色0 XOR 色1) x 0x1111)。タイルごとの表づくりがいらない。
    push bx
    push dx
    lodsb                   ; 上位 = 色0, 下位 = 色1
    mov ah,al
    shr ah,4                ; AH = 色0
    and al,0x0F             ; AL = 色1
    xor al,ah               ; AL = 色0 XOR 色1
    mov dl,al
    shl al,4
    or dl,al
    mov dh,dl               ; DX = (色0 XOR 色1) x 0x1111
    mov cl,ah
    shl ah,4
    or cl,ah
    mov ch,cl               ; CX = 色0 x 0x1111
    xor bh,bh
%rep 8
    lodsb
    mov bl,al
    shl bx,1
    shl bx,1
    mov ax,[cs:c2exp+bx]
    and ax,dx
    xor ax,cx
    stosw
    mov ax,[cs:c2exp+bx+2]
    and ax,dx
    xor ax,cx
    stosw
    xor bh,bh
%endrep
    pop dx
    pop bx
    ret
%else
    push bx
    push dx
    lodsb                   ; 色0 色1
    mov ah,al
    and ah,0x0F             ; AH = 色1
    shr al,4                ; AL = 色0
    mov cl,al
    shl cl,4
    mov ch,ah
    shl ch,4
    mov dl,cl
    or dl,ah                ; DL = 色0色1 (2ドットのビット 01)
    mov dh,ch
    or dh,al                ; DH = 色1色0 (10)
    or cl,al                ; CL = 色0色0 (00)
    or ch,ah                ; CH = 色1色1 (11)
    ; XLUT の語 [4ドットのビット] = 左2ドット | 右2ドット << 8
    mov al,cl
    mov ah,cl
    mov [ss:XLUT+0],ax
    mov al,cl
    mov ah,dl
    mov [ss:XLUT+2],ax
    mov al,cl
    mov ah,dh
    mov [ss:XLUT+4],ax
    mov al,cl
    mov ah,ch
    mov [ss:XLUT+6],ax
    mov al,dl
    mov ah,cl
    mov [ss:XLUT+8],ax
    mov al,dl
    mov ah,dl
    mov [ss:XLUT+10],ax
    mov al,dl
    mov ah,dh
    mov [ss:XLUT+12],ax
    mov al,dl
    mov ah,ch
    mov [ss:XLUT+14],ax
    mov al,dh
    mov ah,cl
    mov [ss:XLUT+16],ax
    mov al,dh
    mov ah,dl
    mov [ss:XLUT+18],ax
    mov al,dh
    mov ah,dh
    mov [ss:XLUT+20],ax
    mov al,dh
    mov ah,ch
    mov [ss:XLUT+22],ax
    mov al,ch
    mov ah,cl
    mov [ss:XLUT+24],ax
    mov al,ch
    mov ah,dl
    mov [ss:XLUT+26],ax
    mov al,ch
    mov ah,dh
    mov [ss:XLUT+28],ax
    mov al,ch
    mov ah,ch
    mov [ss:XLUT+30],ax
    xor bx,bx
%rep 8
    lodsb
    mov dl,al
    mov bl,al
    shr bl,3
    and bl,0x1E
    mov ax,[ss:XLUT+bx]
    stosw
    mov bl,dl
    shl bl,1
    and bl,0x1E
    mov ax,[ss:XLUT+bx]
    stosw
%endrep
    pop dx
    pop bx
    ret
%endif

%ifndef OLDC2
; 2色のタイルの表: c2exp[b] = 8 ドット (bit7 が左端) の 1 を 0xF にした 4 バイト。
; 1 バイト = 左のドットが上位 4 ビット (タイルの 1 行 = 4 バイトと同じ並び)。
c2exp:
%assign b 0
%rep 256
    db ((b>>7)&1)*0xF0 | ((b>>6)&1)*0x0F, ((b>>5)&1)*0xF0 | ((b>>4)&1)*0x0F
    db ((b>>3)&1)*0xF0 | ((b>>2)&1)*0x0F, ((b>>1)&1)*0xF0 | (b&1)*0x0F
%assign b b+1
%endrep
%endif

tile_c4:
    push bx
    push dx
    lodsw                   ; AL = 色0色1, AH = 色2色3
    mov cl,al
    shr cl,4                ; CL = 色0
    mov ch,al
    and ch,0x0F             ; CH = 色1
    mov dl,ah
    shr dl,4                ; DL = 色2
    mov dh,ah
    and dh,0x0F             ; DH = 色3
    ; XLUT[i] = (色[i>>2] << 4) | 色[i&3]  (i = 2ドット分の4ビット)
    mov al,cl
    shl al,4
    mov bl,al
    or bl,cl
    mov bh,al
    or bh,ch
    mov [ss:XLUT+0],bx
    mov bl,al
    or bl,dl
    mov bh,al
    or bh,dh
    mov [ss:XLUT+2],bx
    mov al,ch
    shl al,4
    mov bl,al
    or bl,cl
    mov bh,al
    or bh,ch
    mov [ss:XLUT+4],bx
    mov bl,al
    or bl,dl
    mov bh,al
    or bh,dh
    mov [ss:XLUT+6],bx
    mov al,dl
    shl al,4
    mov bl,al
    or bl,cl
    mov bh,al
    or bh,ch
    mov [ss:XLUT+8],bx
    mov bl,al
    or bl,dl
    mov bh,al
    or bh,dh
    mov [ss:XLUT+10],bx
    mov al,dh
    shl al,4
    mov bl,al
    or bl,cl
    mov bh,al
    or bh,ch
    mov [ss:XLUT+12],bx
    mov bl,al
    or bl,dl
    mov bh,al
    or bh,dh
    mov [ss:XLUT+14],bx
    mov bx,XLUT
%rep 16
    lodsb
    mov ah,al
    shr al,4
    ss xlatb
    stosb
    mov al,ah
    and al,0x0F
    ss xlatb
    stosb
%endrep
    pop dx
    pop bx
    ret

; ---------------------------------------------------------------------------
; タイルのコピー。表示中のバッファ (今書いていない方) の画面上の位置 (sx, sy) から
; 8x8 ドットを ES:DI のタイルへ写す (DI は 32 進む)。
; AL = sx (0-216, 偶数), AH = sy (0-136)。sx・sy が 8 の倍数でなければ
; 隣・下のタイルにまたがって読む (横のずれは 2ドット = 1バイト単位)。
; ---------------------------------------------------------------------------
tile_copy:
    push si
    push ds
    push bx
    push cx
    push dx
    push bp
    mov dx,ax               ; DL = sx, DH = sy
    mov bx,TILEB            ; 読む側 = 書いていない方のバッファ
    cmp byte [ss:BUF],0
    je .b
    mov bx,TILEA
.b:
    mov al,dh
    shr al,3
    mov ah,28
    mul ah                  ; AX = 行 x 28
    mov cl,dl
    shr cl,3
    xor ch,ch
    add ax,cx
    shl ax,5
    add bx,ax               ; BX = 左上のタイル
    push es
    pop ds                  ; 読むのも内蔵RAM
    mov cl,dl
    and cl,7
    shr cl,1
    xor ch,ch
    mov bp,cx               ; BP = 横のずれ (バイト 0-3)
    mov al,dh
    and al,7                ; AL = 縦のずれ (行 0-7)
    xor ah,ah
    mov si,ax
    shl si,2
    add si,bx               ; SI = 上のタイルの (ずれ) 行目
    mov dh,8
    sub dh,al               ; DH = 上のタイルから取る行数
    mov dl,8                ; DL = 残りの行数
    or bp,bp
    jnz .row
    or al,al
    jnz .row
%rep 16                     ; ずれなし: そのまま 32 バイト
    movsw
%endrep
    jmp .done
.row:
    or bp,bp
    jnz .split
%ifndef OLDCOPY
.v:
    movsw                   ; 横のずれなし: 1行 4 バイト
    movsw
    dec dh
    jnz .vn
    lea si,[bx+28*32]       ; 下のタイルの 0 行目へ
.vn:
    dec dl
    jnz .v
    jmp .done
%else
    movsw                   ; 横のずれなし: 1行 4 バイト
    movsw
    jmp .rnext
%endif
.split:
%ifndef OLDCOPY
    ; 2026-11-26: ずれ (1〜3 バイト) ごとの専用のコードで、1 行を語の読み書き 2 回で写す (前は 1 バイトずつ LOOP)
    cmp bp,2
    je .s2
    jb .s1
.s3:
    mov al,[si+3]
    mov ah,[si+32]
    stosw
    mov ax,[si+33]
    stosw
    add si,4
    dec dh
    jnz .s3n
    lea si,[bx+28*32]
.s3n:
    dec dl
    jnz .s3
    jmp .done
.s2:
    mov ax,[si+2]
    stosw
    mov ax,[si+32]
    stosw
    add si,4
    dec dh
    jnz .s2n
    lea si,[bx+28*32]
.s2n:
    dec dl
    jnz .s2
    jmp .done
.s1:
    mov ax,[si+1]
    stosw
    mov al,[si+3]
    mov ah,[si+32]
    stosw
    add si,4
    dec dh
    jnz .s1n
    lea si,[bx+28*32]
.s1n:
    dec dl
    jnz .s1
    jmp .done
%endif
    push si                 ; 左のタイルの [ずれ..3] + 右のタイルの [0..ずれ-1]
    add si,bp
    mov cx,4
    sub cx,bp
.l1:
    movsb
    loop .l1
    pop si
    push si
    add si,32
    mov cx,bp
.l2:
    movsb
    loop .l2
    pop si
    add si,4
.rnext:
    dec dh
    jnz .same
    lea si,[bx+28*32]       ; 下のタイルの 0 行目へ
.same:
    dec dl
    jnz .row
.done:
    pop bp
    pop dx
    pop cx
    pop bx
    pop ds
    pop si
    ret

; ---- 割り込み ---------------------------------------------------------------
isr_dummy:
    iret

isr_vbl:
    push ax
    inc word [ss:TICK]
    cmp byte [ss:PENDING],0
    je .done
    mov ax,[ss:TICK]
    sub ax,[ss:NEXTT]
    js .done
    mov al,[ss:PENDING]
    out 0x07,al
    mov byte [ss:PENDING],0
    mov al,[ss:CFGK]
    xor ah,ah
    add [ss:NEXTT],ax
    mov al,[ss:PALPEND]
    or al,al
    je .done
    mov byte [ss:PALPEND],0
%ifdef NILE
    cmp al,2
    jb .p16
    dec al                  ; PALPEND 2 → RASTER 1 (16 ラインの帯)、3 → 2 (8 ラインの帯)
    mov [ss:RASTER],al
    mov ax,[ss:NEXTBAND]    ; 帯のパレットのコマを表示した: ラスターを始める (下の band_vbl が毎 VBlank 書く)
    mov [ss:DISPBAND],ax
    jmp .done
.p16:
    mov byte [ss:RASTER],0
%endif
    call pal_apply
.done:
%ifdef NILE
    cmp byte [ss:RASTER],0
    je .noras
    call band_vbl
.noras:
%endif
    cmp byte [ss:OVL],1     ; 時間表示を消す時刻か
    jne .novl
    mov ax,[ss:TICK]
    sub ax,[ss:OVLT]
    js .novl
    mov byte [ss:OVL],0
    mov al,1
    out 0x00,al
.novl:
%ifndef NOSDMA
    cmp byte [ss:ARUN],0
    je .nosd
    call atopup             ; 音のリングを足す
.nosd:
%endif
    call input
    mov al,0x40
    out 0xB6,al
    pop ax
    iret

%ifdef NILE
; ---- ラスター (2026-11-26) ---------------------------------------------------
; 画面を 16 ラインずつ 9 本の帯に分け、帯 k は自分の 8 パレットを (k&1)*8 番から使う。
; VBlank で帯 0・1 (512 バイト) を書き、ライン 16k (帯 k の始まり) で帯 k+1 を、帯 k が使っていない方の
; 組 ((k+1)&1)*8 へ書く (k = 1〜7)。割り込みが一般 DMA (最大 2048 バイト = 8 ライン) で遅れても、帯は 16 ラインあるので間に合う。
; 写すのは一般 DMA (本体の RAM → パレット RAM)。メイン側の一般 DMA はポートを書く間 割り込みを止めている (gdma_vseg)。
; ライン一致の割り込みは、そのラインを描く前に起きる (mesem で、帯の最後のラインの色が乱れた) ので、
; RLAG ライン遅らせる。遅らせても、一般 DMA の待ち (最大 8 ライン) と写す時間 (1 ライン) を足して 16 ラインに収まる。
RLAG        equ 2
; ラスターを止める (メインから呼ぶ)。パレット RAM は帯 0・1 のままになる
raster_off:
    pushf
    cli
    mov byte [ss:RASTER],0
    mov byte [ss:PALPEND],0
    in al,0xB2
    and al,0xEF
    out 0xB2,al
    popf
    ret

; 割り込みの中から呼ぶ (割り込みは止まっている)
band_vbl:
    mov ax,[ss:DISPBAND]
    out 0x40,ax
    xor al,al
    out 0x42,al
    mov ax,0xFE00
    out 0x44,ax
    mov ax,512
    out 0x46,ax
    mov al,0x80
    out 0x48,al
    mov byte [ss:RK],2
    mov al,16+RLAG
    cmp byte [ss:RASTER],2
    jne .h16
    mov al,8+RLAG           ; 8 ラインの帯
.h16:
    out 0x03,al             ; 帯 1 の始まり (+RLAG) で割り込み
    mov al,0x10
    out 0xB6,al             ; 前のラインの割り込みの残りを消す
    in al,0xB2
    or al,0x10
    out 0xB2,al
    ret

; RASTER = 1: 16 ラインの帯 9 本 / 2: 8 ラインの帯 18 本 (2026-11-26 深夜。どちらも帯ごとに 8 パレット = 256 バイト)。
; 8 ラインの帯は、帯 k+1 を写せるのが帯 k の 8 ライン (−RLAG) だけなので、メインの一般 DMA を 512 バイト (2 ライン) に分けて、
; 割り込みの遅れを 2 ラインまでにしている (decode の全面描き直し)。遅れて次の帯の時刻を過ぎていたら、その分も続けて写す
; (帯 k を写す先の組は帯 k-2 の物で、もう描き終わっている)。
isr_line:
    push ax
    push bx
    mov bl,[ss:RK]
    mov bh,[ss:RASTER]
.one:
    mov ah,bl               ; 写し元 = DISPBAND + 帯 x 256
    xor al,al
    add ax,[ss:DISPBAND]
    out 0x40,ax
    xor al,al
    out 0x42,al
    mov ax,0xFE00           ; 写し先 = パレット RAM の (帯 & 1) x 256
    test bl,1
    jz .ev
    mov ah,0xFF
.ev:
    out 0x44,ax
    mov ax,256
    out 0x46,ax
    mov al,0x80
    out 0x48,al
    inc bl
    mov al,bl               ; 次: 帯 (RK-1) の始まりのライン (+RLAG)
    dec al
    shl al,1
    shl al,1
    shl al,1
    cmp bh,2
    je .h8
    cmp bl,9
    jae .last
    shl al,1                ; 16 ラインの帯
    jmp .nx
.h8:
    cmp bl,18
    jae .last
.nx:
    add al,RLAG
    mov ah,al
    in al,0x02              ; 今のライン。もう過ぎていたら続けて写す
    cmp al,ah
    jae .one
    mov [ss:RK],bl
    mov al,ah
    out 0x03,al
    jmp .ack
.last:
    mov [ss:RK],bl
    in al,0xB2              ; 最後の帯まで写した: この画面はもう割り込まない
    and al,0xEF
    out 0xB2,al
.ack:
    mov al,0x10
    out 0xB6,al
    pop bx
    pop ax
    iret
%endif

; 16パレットを PALSTAGE → パレットRAM (0xFE00) へ。VBlank 割り込みの中で、画面を切り替えた直後に呼ぶ。
; 先に VBlank を受け付け済みにして割り込みを許し、音声の割り込みを待たせない。
pal_apply:
    mov al,0x40
    out 0xB6,al
    push cx
    push si
    push di
    push ds
    push es
    xor ax,ax
    mov ds,ax
    mov es,ax
    cld
    mov si,PALSTAGE
    mov di,0xFE00
    sti
    mov cx,16
.l:
%rep 16
    movsw
%endrep
    loop .l
    cli
    pop es
    pop ds
    pop di
    pop si
    pop cx
    ret

isr_hbl:
    push ax
%ifdef NILE
    cmp byte [ss:AHOLD],0   ; 次の音声がまだ読み込まれていない → 無音で待つ
    jne .hold
%endif
    push si
    push ds
    mov ax,ASEG
    mov ds,ax
    mov si,[ss:APTR]
    lodsb
    out 0x89,al
    mov [ss:APTR],si
    add word [ss:HBLCNT],1  ; 鳴らしたサンプル数 (32bit、音ズレ補正に使う)
    adc word [ss:SMPLHI],0
    or si,si
    jnz .ok
%ifdef NILE
    inc word [ss:ACI]       ; 次の音声チャンク (リングを一周したら先頭へ)
    mov al,[ss:ABANK]
    inc al
    cmp al,AR_END
    jb .ab
    mov al,AR0
.ab:
    mov [ss:ABANK],al
    SET_ABANK
    mov ax,[ss:ALD]         ; そのチャンクが読み込み済みでなければ、読み込まれるまで無音にする
    cmp ax,[ss:ACI]         ; (読み込みが遅れたとき、古いデータを鳴らさないため)
    ja .ok
    mov byte [ss:AHOLD],1
%else
    inc byte [ss:ABANK]
    mov al,[ss:ABANK]
    SET_ABANK
%endif
.ok:
    mov al,0x80
    out 0xB6,al
    pop ds
    pop si
    pop ax
    iret
%ifdef NILE
.hold:
    mov al,0x80
    out 0x89,al
    out 0xB6,al
    pop ax
    iret
%endif

%ifdef NILE
%include "nile.inc"
%endif

CODE_END:
; ---- ROMヘッダー (セグメント最後の16バイト) ---------------------------------
    times 65520-($-$$) db 0xFF
    jmp 0xF000:start
    db 0                    ; maintenance
    db 0                    ; developer
    db 1                    ; カラー対応
    db 0                    ; cart id
    db 0                    ; version
    db 3                    ; ROMサイズ (変換時に書き換え)
    db 0                    ; セーブなし
    db 4                    ; flags (横画面・16bitバス)
    db 0                    ; mapper
    dw 0                    ; チェックサム (変換時に計算)
