; ===========================================================================
; nileswan 版: microSD から動画データを読みながら再生する部分
;
; 手順は libnile (zlib ライセンス, https://github.com/49bitcat/libnile) の
; storage/diskio.c と core/spi/*.s を参考にした。
;
;  1. I/O ロック解除 (POW_CNT=0x00DD) とバンクマスク解除 (BANK_MASK の bit9-11)。
;     ロック解除で TF カードの電源が切れるので、カードを初期化し直す。
;  2. FAT32 のルートから cfg_name のファイルを探し、クラスタの連続区間の表を作る。
;  3. ファイル先頭のヘッダー (.WSV) を内蔵RAMに読む。
;  4. 再生中は待ち時間に 64KB チャンク単位で PSRAM のリングバッファへ読み込む
;     (セルフフラッシュモード: ポート0xCE=1 で 0x10000 に PSRAM が見える)。
;     映像はバンク 16-111、音声は 112-127。デコーダーと音声割り込みはそこを読む。
;
; .WSV の形式 (チャンク = 64KB = 128 セクタ):
;   チャンク0 = ヘッダー
;     +0 'WSVD'  +4 版(1)  +5 K  +6 R  +7 動画の本数
;     +8 チャンク総数(u32)  +0x0C ヘッダーのセクタ数(u16)
;     +0x10 動画表 8バイト x 64 (開始チャンク u32, コマ数 u16, 予備 u16)
;     +0x210 チャンクの種類 1ビットずつ (0=映像 1=音声、LSBから)
;   チャンク1以降 = 映像ストリーム / 音声ストリームの 64KB 片を、必要になる順に並べたもの
; ===========================================================================

%define POS(r,c) ((r)*64+(c)*2)

; ---------------------------------------------------------------------------
; 起動時の準備
; ---------------------------------------------------------------------------
nile_boot:
    call text_screen
    mov si,msg_title
    mov di,POS(2,2)
    call print
    mov si,msg_card
    mov di,POS(8,2)
    call print
    ; I/O ロック解除。EMU_CNT=0 (フラッシュ書き込み保護なし・16bitバス)
    mov ax,0x00DD
    out 0xE2,ax
    ; バンクマスク解除 (ROM0 / ROM1 / RAM で生のバンク番号を使う)
    in ax,0xE4
    and ax,~0x0E00
    out 0xE4,ax
    xor al,al
    out 0xCE,al
    call sd_init
    mov si,msg_file
    mov di,POS(8,2)
    call print
    call fs_open
    call load_header
    ret

; ---------------------------------------------------------------------------
; 1本の再生の準備: 読み込み位置を動画の先頭にし、少し先読みする
; ---------------------------------------------------------------------------
nile_item_start:
    mov byte [STAGE],0x20
    call text_screen
    mov si,msg_loading
    mov di,POS(8,9)
    call print
    mov bl,[ITEM]
    xor bh,bh
    shl bx,3
    mov ax,[HDR_ITEMS+bx]
    mov dx,[HDR_ITEMS+bx+2]
    mov [FC],ax
    mov [FC+2],dx
    mov al,[ITEM]
    inc al
    cmp al,[CFGCOUNT]
    jb .nxt
    mov ax,[HDR+8]
    mov dx,[HDR+10]
    jmp .setend
.nxt:
    mov ax,[HDR_ITEMS+bx+8]
    mov dx,[HDR_ITEMS+bx+10]
.setend:
    mov [ITEMEND],ax
    mov [ITEMEND+2],dx
    xor ax,ax
    mov [VORD],ax
    mov [AORD],ax
    mov [VSKIP],ax
    mov [ASKIP],ax
    mov cx,PREBUF_V
    jmp ring_restart

; ---------------------------------------------------------------------------
; ジャンプ: ヘッダーのジャンプ表 (16バイト) から読み込み位置と再生位置を決めて先読みする
;   +0 読み始めるチャンク(u32) +4 そこまでの映像チャンク数 +6 音声チャンク数
;   +8 映像チャンク番号 +10 映像オフセット +12 音声チャンク番号 +14 音声オフセット
; ---------------------------------------------------------------------------
nile_seek:
    mov bl,[ITEM]
    xor bh,bh
    shl bx,3
    mov bx,[HDR_ITEMS+bx+6]
    mov ax,[KFI]
    shl ax,4
    add bx,ax
    add bx,HDR
    mov ax,[bx]
    mov [FC],ax
    mov ax,[bx+2]
    mov [FC+2],ax
    mov ax,[bx+4]
    mov [VORD],ax
    mov ax,[bx+6]
    mov [AORD],ax
    mov ax,[bx+8]
    mov [VSKIP],ax
    mov ax,[bx+10]
    mov [VPTR],ax
    mov ax,[bx+12]
    mov [ASKIP],ax
    mov ax,[bx+14]
    mov [APTR],ax
    mov al,VR0
    mov [VBANK],al
    mov al,AR0
    mov [ABANK],al
    mov cx,2                ; ジャンプのときは少しだけ先読みしてすぐ再開

; リングを空にして先読みする (CX = 先読みする映像チャンク数)
ring_restart:
    xor ax,ax
    mov [AHOLD],al
    mov [SECIN],al
    mov [VL],ax
    mov [VCI],ax
    mov [ALD],ax
    mov [ACI],ax
    mov byte [VLSLOT],VR0
    mov byte [ALSLOT],AR0
.pb:
    push cx
    call stream_step
    pop cx
    or al,al
    jz .done
    cmp [VL],cx
    jb .pb
    cmp byte [CFGR],0
    je .done
    cmp word [ALD],0
    je .pb
.done:
    ret

; ---------------------------------------------------------------------------
; 今の映像チャンク (VCI) が読み込み済みになるまで読む
; ---------------------------------------------------------------------------
ensure_video:
    pusha
    push es
    mov ax,[VL]
    cmp ax,[VCI]
    ja .ok
    inc word [STALL]
    ; 再生中に読み込みが追いつかなかったときは、音も止めて待ち、読めたら映像と音を一緒に再開する
    ; (止めずに待つと、読めたあとに映像が遅れを取り戻そうとして早回しになり、音も飛ぶ)
    mov bl,[STARTED]
    or bl,bl
    jz .l
    call audio_stop
.l:
    mov ax,[VL]
    cmp ax,[VCI]
    ja .ok0
    call stream_step
    or al,al
    jnz .l
    mov al,0x51             ; 読む物がないのに足りない = データが壊れている
    mov si,msg_bad
    jmp fatal
.ok0:
    or bl,bl
    jz .ok
    cli
    mov ax,[TICK]           ; 今表示しているコマを K 回分見せてから次へ (一時停止の再開と同じ)
    add al,[CFGK]
    adc ah,0
    mov [NEXTT],ax
    sti
    call audio_start
.ok:
    pop es
    popa
    ret

; ---------------------------------------------------------------------------
; 少しだけ読み込む (最大4セクタ)。AL=1 読んだ / 0 読む物がない(満杯か終わり)
; ---------------------------------------------------------------------------
stream_step:
%ifdef STALLTEST
    ; テスト用: 読み込みをわざと遅くして、読み込み待ちのときの動きを確かめる (64 VBlank 中 3 回だけ読む)
    test byte [TICK],0x3C
    jz .stt
    cmp byte [STARTED],0
    je .stt
    mov al,1
    ret
.stt:
%endif
    push bx
    push cx
    push dx
    push si
    push di
    push es
    mov ax,[FC]
    mov dx,[FC+2]
    cmp dx,[ITEMEND+2]
    jb .go
    ja .idle
    cmp ax,[ITEMEND]
    jae .idle
.go:
    cmp byte [SECIN],0
    jne .cont
    ; チャンクの種類 (ビットマップ)
    mov bx,ax
    shr bx,3
    mov cx,dx
    shl cx,13
    or bx,cx
    mov cl,al
    and cl,7
    mov al,[HDR_BITMAP+bx]
    shr al,cl
    and al,1
    mov [LTYPE],al
    ; ジャンプ直後: ジャンプ先より前のチャンクは読み飛ばす
    or al,al
    jnz .askip
    mov ax,[VORD]
    cmp ax,[VSKIP]
    jae .cont
    inc word [VORD]
    jmp .skip
.askip:
    mov ax,[AORD]
    cmp ax,[ASKIP]
    jae .cont
    inc word [AORD]
.skip:
    add word [FC],1
    adc word [FC+2],0
    jmp .did
.cont:
    cmp byte [LTYPE],0
    jne .aud
    mov ax,[VL]
    sub ax,[VCI]
    cmp ax,VRN
    jae .idle
    mov al,[VLSLOT]
    jmp .ld
.aud:
    mov ax,[ALD]
    sub ax,[ACI]
    cmp ax,ARN
    jae .idle
    mov al,[ALSLOT]
.ld:
    mov [DSTBANK],al
    mov byte [DSTPS],1
    mov byte [PIPE],1
    mov cx,4
.sec:
    push cx
    ; ファイル内のセクタ = FC*128 + SECIN
    mov ax,[FC]
    mov dx,[FC+2]
    mov cx,ax
    shr cx,9
    shl dx,7
    or dx,cx
    shl ax,7
    mov cl,[SECIN]
    xor ch,ch
    add ax,cx
    adc dx,0
    mov bl,[SECIN]
    xor bh,bh
    shl bx,9
    mov [DSTOFF],bx
    call map_fsec
    call read_lba
    pop cx
    inc byte [SECIN]
    cmp byte [SECIN],128
    jb .more
    ; チャンク完了
    mov byte [SECIN],0
    add word [FC],1
    adc word [FC+2],0
    cmp byte [LTYPE],0
    jne .adone
    inc word [VORD]
    inc word [VL]
    mov al,[VLSLOT]
    inc al
    cmp al,VR_END
    jb .v1
    mov al,VR0
.v1:
    mov [VLSLOT],al
    jmp .did
.adone:
    inc word [AORD]
    inc word [ALD]
    mov ax,[ALD]            ; 音声の読み込み待ちだったら再開
    cmp ax,[ACI]
    jbe .a0
    mov byte [AHOLD],0
.a0:
    mov al,[ALSLOT]
    inc al
    cmp al,AR_END
    jb .a1
    mov al,AR0
.a1:
    mov [ALSLOT],al
    jmp .did
.more:
    dec cx
    jz .did
    jmp .sec
.did:
    mov al,1
    jmp .ret
.idle:
    xor al,al
.ret:
    call pipe_flush
    mov byte [PIPE],0
    pop es
    pop di
    pop si
    pop dx
    pop cx
    pop bx
    ret

; ---------------------------------------------------------------------------
; ファイル内のセクタ番号 DX:AX → カードの LBA (DX:AX)。BX,CX,SI,DI を壊す
; ---------------------------------------------------------------------------
map_fsec:
    cmp dx,[CURBASE+2]
    ja .fwd
    jb .reset
    cmp ax,[CURBASE]
    jae .fwd
.reset:
    mov word [CUREXT],0
    mov word [CURBASE],0
    mov word [CURBASE+2],0
.fwd:
    mov bx,[CUREXT]
    cmp bx,[NEXT]
    jae .eof
    shl bx,3
    add bx,EXTTAB
    mov di,ax
    mov si,dx
    sub di,[CURBASE]
    sbb si,[CURBASE+2]
    cmp si,[bx+6]
    jb .in
    ja .next
    cmp di,[bx+4]
    jb .in
.next:
    mov cx,[bx+4]
    add [CURBASE],cx
    mov cx,[bx+6]
    adc [CURBASE+2],cx
    inc word [CUREXT]
    jmp .fwd
.in:
    add di,[bx]
    adc si,[bx+2]
    mov ax,di
    mov dx,si
    ret
.eof:
    mov al,0x52             ; ファイルの終わりより先を読もうとした
    mov si,msg_bad
    jmp fatal

; ---------------------------------------------------------------------------
; FAT32 からファイルを探し、連続区間の表を作る
; ---------------------------------------------------------------------------
fs_open:
    mov byte [STAGE],0x10
    mov byte [DSTPS],0
    mov word [DSTOFF],HDR
    xor ax,ax
    xor dx,dx
    call read_lba
    call is_fat32
    je .bpb
    ; パーティション表 (1番目)
    mov ax,[HDR+0x1C6]
    mov dx,[HDR+0x1C8]
    mov [PART],ax
    mov [PART+2],dx
    mov word [DSTOFF],HDR
    call read_lba
    call is_fat32
    je .bpb
    mov al,0x21
    mov si,msg_fat
    jmp fatal
.bpb:
    cmp word [HDR+0x0B],512
    je .bps
    mov al,0x22
    mov si,msg_fat
    jmp fatal
.bps:
    mov al,[HDR+0x0D]
    mov [SPC],al
    xor cl,cl
.sh:
    shr al,1
    jz .shd
    inc cl
    jmp .sh
.shd:
    mov [SPCSH],cl
    mov ax,[PART]
    mov dx,[PART+2]
    add ax,[HDR+0x0E]
    adc dx,0
    mov [FATLBA],ax
    mov [FATLBA+2],dx
    mov cl,[HDR+0x10]       ; FAT の数
    xor ch,ch
.fats:
    add ax,[HDR+0x24]
    adc dx,[HDR+0x26]
    loop .fats
    mov [DATALBA],ax
    mov [DATALBA+2],dx
    mov ax,[HDR+0x2C]
    mov dx,[HDR+0x2E]
    mov [CLUS],ax
    mov [CLUS+2],dx
    ; ルートディレクトリを探す
.dirclus:
    xor bx,bx
.dirsec:
    push bx
    mov ax,[CLUS]
    mov dx,[CLUS+2]
    call clus2lba
    add ax,bx
    adc dx,0
    mov word [DSTOFF],HDR
    call read_lba
    pop bx
    mov si,HDR
.ent:
    mov al,[si]
    or al,al
    jz .notfound
    cmp al,0xE5
    je .skip
    test byte [si+11],0x18  ; ディレクトリ・ボリューム名 (LFN 0x0F も含む)
    jnz .skip
    push si
    mov di,cfg_name
    mov cx,11
.cmp:
    mov al,[si]
    cmp al,[cs:di]
    jne .ne
    inc si
    inc di
    loop .cmp
    pop si
    jmp .found
.ne:
    pop si
.skip:
    add si,32
    cmp si,HDR+512
    jb .ent
    inc bx
    cmp bl,[SPC]
    jb .dirsec
    call fat_next
    mov [CLUS],ax
    mov [CLUS+2],dx
    call is_eoc
    jnc .dirclus
.notfound:
    call text_screen
    mov si,msg_nofile
    mov di,POS(7,2)
    call print
    mov di,POS(9,4)
    mov si,cfg_name
    mov cx,11
.nm:
    mov al,[cs:si]
    inc si
    sub al,32
    xor ah,ah
    mov [MAPA+di],ax
    add di,2
    loop .nm
    mov si,msg_root
    mov di,POS(11,2)
    call print
    jmp halt

.found:
    mov byte [STAGE],0x11
    mov ax,[si+0x1A]
    mov dx,[si+0x14]
    and dx,0x0FFF
    mov [CLUS],ax
    mov [CLUS+2],dx
    mov word [NEXT],0
    ; 連続区間の表を作る
.run:
    mov ax,[CLUS]
    mov dx,[CLUS+2]
    mov [RUNST],ax
    mov [RUNST+2],dx
    mov word [RUNLEN],1
    mov word [RUNLEN+2],0
.ext:
    call fat_next
    mov bx,[CLUS]
    mov cx,[CLUS+2]
    add bx,1
    adc cx,0
    cmp ax,bx
    jne .brk
    cmp dx,cx
    jne .brk
    mov [CLUS],ax
    mov [CLUS+2],dx
    add word [RUNLEN],1
    adc word [RUNLEN+2],0
    jmp .ext
.brk:
    push ax
    push dx
    call add_extent
    pop dx
    pop ax
    mov [CLUS],ax
    mov [CLUS+2],dx
    call is_eoc
    jnc .run
    ret

; ZF=1 なら HDR に FAT32 のブートセクタがある
is_fat32:
    cmp word [HDR+0x52],'FA'
    jne .r
    cmp word [HDR+0x54],'T3'
    jne .r
    cmp byte [HDR+0x56],'2'
.r:
    ret

; DX:AX がチェーンの終わり (または不正) なら CF=1
is_eoc:
    cmp dx,0x0FFF
    jb .chk
    cmp ax,0xFFF7
    jae .end
.chk:
    or dx,dx
    jnz .ok
    cmp ax,2
    jb .end
.ok:
    clc
    ret
.end:
    stc
    ret

; RUNST / RUNLEN (クラスタ) を区間表に足す
add_extent:
    mov bx,[NEXT]
    cmp bx,EXTMAX
    jb .ok
    mov al,0x31
    mov si,msg_frag
    jmp fatal
.ok:
    shl bx,3
    add bx,EXTTAB
    mov ax,[RUNST]
    mov dx,[RUNST+2]
    call clus2lba
    mov [bx],ax
    mov [bx+2],dx
    mov ax,[RUNLEN]
    mov dx,[RUNLEN+2]
    mov cl,[SPCSH]
.s:
    or cl,cl
    jz .d
    shl ax,1
    rcl dx,1
    dec cl
    jmp .s
.d:
    mov [bx+4],ax
    mov [bx+6],dx
    inc word [NEXT]
    ret

; クラスタ DX:AX → 先頭セクタの LBA (DX:AX)。CX を壊す
clus2lba:
    sub ax,2
    sbb dx,0
    mov cl,[SPCSH]
.s:
    or cl,cl
    jz .d
    shl ax,1
    rcl dx,1
    dec cl
    jmp .s
.d:
    add ax,[DATALBA]
    adc dx,[DATALBA+2]
    ret

; CLUS の次のクラスタ → DX:AX
fat_next:
    push bx
    push cx
    mov ax,[CLUS]
    mov dx,[CLUS+2]
    mov bx,ax
    and bx,127
    shl bx,2
    mov cx,dx
    shl cx,9
    shr ax,7
    or ax,cx
    shr dx,7
    add ax,[FATLBA]
    adc dx,[FATLBA+2]
    cmp ax,[FATCACHE]
    jne .rd
    cmp dx,[FATCACHE+2]
    je .hit
.rd:
    mov [FATCACHE],ax
    mov [FATCACHE+2],dx
    push bx
    mov byte [DSTPS],0
    mov word [DSTOFF],SECBUF
    call read_lba
    pop bx
.hit:
    mov ax,[SECBUF+bx]
    mov dx,[SECBUF+bx+2]
    and dx,0x0FFF
    pop cx
    pop bx
    ret

; .WSV のヘッダーを読む
load_header:
    mov byte [STAGE],0x12
    mov word [CUREXT],0
    mov word [CURBASE],0
    mov word [CURBASE+2],0
    mov byte [DSTPS],0
    xor bx,bx
.l:
    push bx
    mov ax,bx
    shl ax,9
    add ax,HDR
    mov [DSTOFF],ax
    mov ax,bx
    xor dx,dx
    call map_fsec
    call read_lba
    pop bx
    or bx,bx
    jnz .n
    cmp word [HDR],'WS'
    jne .bad
    cmp word [HDR+2],'VD'
    jne .bad
    mov al,[HDR+4]          ; 版 (bit7 = 60Hz モード)
    and al,0x7F
    cmp al,1
    je .verok
    cmp al,2
    jne .bad
.verok:
    mov ax,[HDR+0x0C]
    or ax,ax
    jz .bad
    cmp ax,HDRMAX
    ja .bad
.n:
    inc bx
    cmp bx,[HDR+0x0C]
    jb .l
    mov al,[HDR+5]
    mov [CFGK],al
    mov al,[HDR+6]          ; 音の R (bit7 = 1 回 2 バイト・音の DMA 24kHz、bit6 = ステレオ。2026-11-26)
    mov ah,al
    and ah,0xC0
    mov [AFLG],ah
    and al,0x3F
    mov [CFGR],al
    mov al,[HDR+7]
    or al,al
    jz .bad
    mov [CFGCOUNT],al
    mov ax,[HDR+0x0E]       ; ジャンプ先の間隔 (古い .WSV は 0 = ジャンプなし)
    mov [KFW],ax
    mov al,0x9E
    test byte [HDR+4],0x80
    jz .vt
    mov al,198              ; 60Hz モード: 版2 = 199 ライン (偶数値。SwanCrystal で奇数値は液晶に悪いとされる)
    cmp byte [HDR+4],0x82
    je .vt
    mov al,199              ; 版1 (2026-11-08 の最初の 60Hz 版) = 200 ライン
.vt:
    call set_vtotal
    ret
.bad:
    mov al,0x41
    mov si,msg_bad
    jmp fatal

; ---------------------------------------------------------------------------
; microSD カード (SPI モード)
; ---------------------------------------------------------------------------
sd_init:
    mov byte [STAGE],1
    mov word [SPICFG],SPI_CART
    call spi_wait
    mov ax,SPI_CART
    out 0xE0,ax
    call cs_high
    in al,0xE2
    or al,0x02              ; TF カードの電源
    out 0xE2,al
    mov ax,3
    call wait_ticks         ; 約 40ms
    mov ax,SPI_READ|9       ; CS=high のまま 80 クロック
    call spi_go
    call spi_wait
    ; CMD0 (リセット)
    mov byte [STAGE],2
    xor ax,ax
    mov [CMDARG],ax
    mov [CMDARG+2],ax
    mov ax,0x9540
    mov cl,2
    call tf_cmd
    test al,0xFE
    jz .c8
    mov ah,0x01
    jmp .err
.c8:
    ; CMD8 (電圧の確認。古いカードは非対応)
    mov byte [STAGE],3
    mov word [CMDARG],0x01AA
    mov ax,0x8748
    mov cl,6
    call tf_cmd
    test al,0xFE
    jnz .v1
    mov bx,3
    call rx_at
    and al,0x0F
    cmp al,1
    jne .e2
    mov bx,4
    call rx_at
    cmp al,0xAA
    je .hc
.e2:
    mov ah,0x02
    jmp .err
.hc:
    ; SDHC/SDXC: ACMD41(HCS) → 成功なら CMD58 でアドレス方式を調べる
    mov word [CMDARG],0
    mov word [CMDARG+2],0x4000
    mov bx,0x95E9
    call op_cond
    jc .v1                  ; 失敗したら古い方式を試す (libnile と同じ順)
    ; CMD58 (OCR): ブロック単位のアドレスか
    mov byte [STAGE],5
    xor ax,ax
    mov [CMDARG],ax
    mov [CMDARG+2],ax
    mov ax,0x957A
    mov cl,6
    call tf_cmd
    or al,al
    jnz .blklen
    mov bx,1
    call rx_at
    test al,0x40
    jz .blklen
    mov byte [CARDBLK],1
    jmp .blklen
.v1:
    ; SD v1: ACMD41(0)、だめなら MMC の CMD1
    xor ax,ax
    mov [CMDARG],ax
    mov [CMDARG+2],ax
    mov bx,0x95E9
    call op_cond
    jnc .blklen
    mov bx,0x9541
    call op_cond
    jnc .blklen
    mov ah,[ERRCODE]
    jmp .err
.blklen:
    ; CMD16 (ブロック長 512)
    mov byte [STAGE],6
    mov word [CMDARG],512
    mov word [CMDARG+2],0
    mov ax,0x9550
    mov cl,2
    call tf_cmd
    or al,al
    jz .ok
    mov ah,0x04
    jmp .err
.ok:
    mov word [SPICFG],0     ; 24MHz
    call cs_high
    mov byte [STAGE],7
    ret
.err:
    mov al,ah
    mov si,msg_sd
    jmp fatal

; BX = 初期化コマンド (ACMD41 / CMD1)。カードの準備ができるまで繰り返す (最大約1.5秒)
; 失敗すると CF=1、ERRCODE = 最後の応答 (時間切れは 0xEE)
op_cond:
    mov byte [STAGE],4
    mov ax,[TICK]
    add ax,115
    mov [TOUT],ax
.l:
    mov ax,bx
    mov cl,2
    push bx
    call tf_cmd
    pop bx
    test al,0xFE
    jnz .e
    or al,al
    jz .ok
    mov ax,[TICK]
    sub ax,[TOUT]
    js .l
    mov al,0xEE
.e:
    mov [ERRCODE],al
    stc
    ret
.ok:
    clc
    ret

; AL = コマンド (0x40|n、bit7 = ACMD), AH = CRC, CL = 応答の長さ, CMDARG = 引数
; 戻り値 AL = R1 (応答の続きは受信バッファに見えている)。BX を壊す
tf_cmd:
    test al,0x80
    jz .plain
    push ax
    push cx
    push word [CMDARG]
    push word [CMDARG+2]
    mov word [CMDARG],0
    mov word [CMDARG+2],0
    mov ax,0x9577           ; CMD55
    mov cl,1
    call tf_cmd
    pop word [CMDARG+2]
    pop word [CMDARG]
    pop cx
    mov bl,al
    pop ax
    test bl,0xFE
    jz .plain
    mov al,bl
    ret
.plain:
    push cx
    push ax
    call cs_high
    call cs_low
    pop ax
    push es
    push ax
    xor al,al
    out 0xCE,al
    mov ax,TXBANK
    out 0xD0,ax
    mov ax,0x1000
    mov es,ax
    pop ax
    and al,0x7F
    mov [es:0],al
    mov [es:5],ah
    mov al,[CMDARG+3]
    mov [es:1],al
    mov al,[CMDARG+2]
    mov [es:2],al
    mov al,[CMDARG+1]
    mov [es:3],al
    mov al,[CMDARG]
    mov [es:4],al
    pop es
    mov ax,5
    call spi_go_tx
    pop cx
    xor ch,ch
    dec cx
    mov ax,cx
    or ax,SPI_WREAD
    call spi_go
    call spi_flip
    jmp rx0

; 複数ブロック読み出しを止める (CMD12)
tf_stop:
    cmp byte [OPEN],0
    je .r
    call pipe_flush
    mov byte [OPEN],0
    mov byte [TOKF],0
    push es
    xor al,al
    out 0xCE,al
    mov ax,TXBANK
    out 0xD0,ax
    mov ax,0x1000
    mov es,ax
    mov byte [es:0],0x4C
    mov byte [es:1],0
    mov byte [es:2],0
    mov byte [es:3],0
    mov byte [es:4],0
    mov byte [es:5],0x95
    mov byte [es:6],0xFF
    pop es
    mov ax,6
    call spi_go_tx
    mov ax,SPI_WREAD
    call spi_go
    call spi_flip
    call tf_wait_ready
.r:
    ret

; DX:AX = カードの LBA。DSTPS / DSTBANK / DSTOFF の場所に 512 バイト読む。
; 連続する LBA なら CMD18 を続けて読む。BX, CX を壊す
; 1セクタを「データの始まり + 144バイト」と「368バイト + CRC」の2回に分けて受け、
; 前半を写す間に後半を、後半を写す間に次のセクタの前半を受ける (SPI の待ち時間を隠す)。
; PIPE = 1 のときは後半を写さずに残し (PEND)、次の read_lba か pipe_flush で写す。
read_lba:
    cmp byte [OPEN],0
    je .start0
    cmp ax,[NEXTLBA]
    jne .restart
    cmp dx,[NEXTLBA+2]
    je .cont
.restart:
    call pipe_flush
    push ax
    push dx
    call tf_stop
    pop dx
    pop ax
    jmp .start
.start0:
    call pipe_flush
.start:
    mov [NEXTLBA],ax
    mov [NEXTLBA+2],dx
    cmp byte [CARDBLK],0
    jne .blk
    mov cx,ax               ; バイト単位のカード: x512
    shr cx,7
    shl dx,9
    or dx,cx
    shl ax,9
.blk:
    mov [CMDARG],ax
    mov [CMDARG+2],dx
    mov ax,0x9552           ; CMD18
    mov cl,1
    call tf_cmd
    or al,al
    jnz .err
    mov byte [OPEN],1
    mov byte [TOKF],0
.cont:
%ifndef NOTRICK
    cmp byte [DSTPS],0
    jne .fast
    cmp byte [TOKF],0       ; trick の読み方の途中から本体の RAM へ読むときは、読み出しをやり直す
    jne .restart
%endif
    mov ax,SPI_WREAD|144    ; データの始まり (0xFE) を待って、それと前半 144 バイト
    call spi_go
    call pipe_flush         ; 受信している間に、前のセクタの後半を写す
    call spi_flip
    call rx0
    cmp al,0xFE
    jne .err
    mov ax,SPI_READ|369     ; 後半 368 バイト + CRC 2 バイト
    call spi_go
    push si
    push di
    mov si,1
    mov di,[DSTOFF]
    mov cx,72
    mov bl,[DSTPS]
    mov bh,[DSTBANK]
    call copy_rx_part       ; 受信している間に前半を写す
    pop di
    pop si
    call spi_flip
    mov ax,[DSTOFF]
    add ax,144
    mov [PENDOFF],ax
    mov al,[DSTPS]
    mov [PENDPS],al
    mov al,[DSTBANK]
    mov [PENDBANK],al
    mov byte [PEND],1
    cmp byte [PIPE],0
    jne .later
    call pipe_flush
%ifndef NOTRICK
    jmp .later
    ; ---- PSRAM へ読む: nileswan の LODSW trick (ポート 0xE3 bit5) を使う ----
    ; 受信バッファを ROM1 の窓から読むと、RAM の窓のバンクの同じ番地の PSRAM にも書かれる (実機で確認、2026-11-25)。
    ; 受信バッファは 512 バイトごとに繰り返し見えるので、データを受信バッファの 0 番目から受ければ、
    ; 書き先 (DSTOFF、512 の倍数) の番地から読むだけで写せる。読むのは一般 DMA (1 バイト 1 サイクル)。
    ; 1) 始まりの印 1 バイト  2) データ 512 バイト  3) CRC 2 バイト  4) 次のセクタの始まりの印 (出しておく)
    ; 写すのは次の読み込み (または pipe_flush) で、4) を待っている間に行う。
.fast:
%ifdef OLDPIPE
    cmp byte [TOKF],0
    jne .tok
    mov ax,SPI_WREAD|0      ; 始まりの印を待つ
    call spi_go
.tok:
    call pipe_flush         ; 印を待っている間に、前のセクタを写す
    call spi_flip
    call rx0
    mov byte [TOKF],0
    cmp al,0xFE
    jne .err
    mov ax,SPI_READ|511     ; データ 512 バイト (受信バッファの 0 番目から)
    call spi_go
    call spi_flip
    mov ax,SPI_READ|1       ; CRC 2 バイト (読み捨て)
    call spi_go
    mov ax,SPI_WREAD|0      ; 次のセクタの始まりの印 (待っている間に、このセクタを写す)
    call spi_go
    mov byte [TOKF],1
%else
    ; 2026-11-26: データ 512 バイトを受ける間 (約 500 サイクル) に前のセクタを写す。
    ; 受信バッファは 2 面あり、SPI が受けるのは見えていない側。前のセクタは見えている側に残っているので、
    ; 印・データを見えていない側へ受けながら、見えている側を一般 DMA で写せる。
    ; (始まりの印はデータで上書きされるので確かめない。印が来なければ spi_wait の時間切れになる)
    ; TOKF = 1: カードは前のセクタの CRC を送る前で止まっている
    cmp byte [TOKF],0
    je .ftok
    mov ax,SPI_READ|1       ; 前のセクタの CRC 2 バイト (読み捨て)
    call spi_go
.ftok:
    mov ax,SPI_WREAD|0      ; 始まりの印を待つ
    call spi_go
    mov ax,SPI_READ|511     ; データ 512 バイト (見えていない側の 0 番目から)
    call spi_go
    call pipe_flush         ; 受けている間に、前のセクタ (見えている側) を写す
    call spi_flip           ; 受け終わったら入れ替えて、このセクタを見えるようにする
    mov byte [TOKF],1
%endif
    mov al,[DSTBANK]
    mov [FPBANK],al
    mov ax,[DSTOFF]
    mov [FPOFF],ax
    mov byte [FPEND],1
    cmp byte [PIPE],0
    jne .later
    call pipe_flush
%endif
.later:
    add word [NEXTLBA],1
    adc word [NEXTLBA+2],0
    inc word [NSECT]
    mov byte [RETRY],0
    ret
.err:
    mov [ERRCODE],al
    inc byte [RETRY]
    cmp byte [RETRY],3
    ja .fatal
    mov byte [OPEN],1
    call tf_stop
    mov ax,[NEXTLBA]
    mov dx,[NEXTLBA+2]
    jmp read_lba
.fatal:
    mov al,[ERRCODE]
    mov si,msg_read
    jmp fatal

; 受信バッファに残っているセクタの後半を写す (なければ何もしない)。レジスタは壊さない
pipe_flush:
%ifndef NOTRICK
    cmp byte [FPEND],0
    je .nf
    ; trick: 受信バッファ (ROM1 = バンク 510) の FPOFF の番地から 512 バイトを一般 DMA で読む → PSRAM バンク FPBANK の FPOFF へ。
    ; 写し先 (本体の RAM) には読んだ内容が入るだけ (SECBUF を使い、FAT の覚えを消す)。
    ; trick の間に音のリングを足す割り込み (ROM0 の窓を読む) が入ると PSRAM を壊すので、割り込みを止める。
    push ax
    pushf
    cli
    mov byte [FPEND],0
    mov ax,RXBANK
    out 0xD4,ax
    mov al,1
    out 0xCE,al
    mov al,[FPBANK]
    xor ah,ah
    out 0xD0,ax
    mov al,0x20
    out 0xE3,al
    mov ax,[FPOFF]
    out 0x40,ax
    mov al,3                ; 0x30000 = ROM1 の窓
    out 0x42,al
    mov ax,SECBUF
    out 0x44,ax
    mov ax,512
    out 0x46,ax
    mov al,0x80
    out 0x48,al
    xor al,al
    out 0xE3,al
    out 0xCE,al
    mov word [FATCACHE],0xFFFF
    mov word [FATCACHE+2],0xFFFF
    popf
    pop ax
.nf:
%endif
    cmp byte [PEND],0
    je .r
    push ax
    push bx
    push cx
    push si
    push di
    mov byte [PEND],0
    xor si,si
    mov di,[PENDOFF]
    mov cx,184
    mov bl,[PENDPS]
    mov bh,[PENDBANK]
    call copy_rx_part
    pop di
    pop si
    pop cx
    pop bx
    pop ax
.r:
    ret

; 受信バッファ [SI] から CX 語を、BL = 0 なら内蔵RAM の DI へ、1 なら PSRAM バンク BH の DI へ。AX を壊す
copy_rx_part:
    push ds
    push es
    push cx
    mov ax,RXBANK
    out 0xD4,ax
    or bl,bl
    je .iram
    mov al,1
    out 0xCE,al             ; 0x10000 に PSRAM を出す
    mov al,bh
    xor ah,ah
    out 0xD0,ax
    mov ax,0x1000
    mov es,ax
    jmp .go
.iram:
    xor ax,ax
    mov es,ax
.go:
    mov ax,0x3000
    mov ds,ax
.lp:
    cmp cx,8
    jb .tail
%rep 8
    movsw                   ; REP を使わない (音声の割り込みを待たせないため)
%endrep
    sub cx,8
    jmp .lp
.tail:
    jcxz .end
.t1:
    movsw
    loop .t1
.end:
    xor al,al
    out 0xCE,al
    pop cx
    pop es
    pop ds
    ret

; ---------------------------------------------------------------------------
; nileswan のメニューへ戻る (libnile の nile_soft_reset と同じ手順)。戻らない。
; 音と割り込みを止め、microSD の読み出しを終え、フラッシュを起こしてから
; カートリッジの IPL0 (FFFF:0000) へ飛ぶ。IPL0 がハードを初期化して MENU.WS を読み直す。
; ---------------------------------------------------------------------------
menu_return:
    cli
    xor al,al
    out 0x90,al             ; 音を止める
    out 0xB2,al             ; 割り込みを止める
    out 0xA2,al             ; タイマーを止める
    call tf_stop
    call cs_high
    ; フラッシュを起こす (IPL0 はフラッシュが起きている前提): コマンド 0xAB を1バイト送る
    and word [SPICFG],~0x3000
    or word [SPICFG],0x2000 ; 装置 = フラッシュ
    push es
    xor al,al
    out 0xCE,al
    mov ax,TXBANK
    out 0xD0,ax
    mov ax,0x1000
    mov es,ax
    mov byte [es:0],0xAB
    pop es
    xor ax,ax
    call spi_go_tx
    call spi_wait
    in ax,0xE0
    and ax,0x0800           ; 装置の選択をやめる (速さのビットだけ残す)
    out 0xE0,ax
    mov al,0xDD
    out 0xE2,al             ; nileswan のレジスタのロック解除
    xor ax,ax
    out 0xB7,al             ; NMI を止める
    out 0x00,ax             ; 画面を消す
    ; ポート0xC0 を変えると今のコードが見えなくなるので、スタックに置いた小さなコードから飛ぶ:
    ;   out 0xC0,al / out 0xE4,ax / nop / jmp far FFFF:0000   (AX = 0xFFFF)
    dec ax
    push ax                 ; FFFF
    inc ax
    push ax                 ; 0000
    push 0xEA90
    push 0xE4E7
    push 0xC0E6
    dec ax                  ; AX = 0xFFFF
    mov bp,sp
    push ss
    push bp
    retf
; ---- SPI の基本操作 ----------------------------------------------------------
; 転送が終わるまで待つ (時間切れはエラー画面)
spi_wait:
    push cx
    xor cx,cx
.l:
    in al,0xE1
    test al,0x80
    jz .ok
    loop .l
    mov al,0xE1
    mov si,msg_sd
    jmp fatal
.ok:
    pop cx
    ret

; AX = (長さ-1) | モード。今の装置で受信を始める
spi_go:
    push dx
    mov dx,ax
    call spi_wait
    in ax,0xE0
    and ax,0x4000
    or ax,[SPICFG]
    or ax,dx
    or ax,0x8000
    out 0xE0,ax
    pop dx
    ret

; AX = 長さ-1。送信バッファの内容を送る (バッファを入れ替えて開始)
spi_go_tx:
    push dx
    mov dx,ax
    call spi_wait
    in ax,0xE0
    and ax,0x4000
    or ax,[SPICFG]
    xor ax,0xC000
    or ax,dx
    out 0xE0,ax
    pop dx
    ret

; 受信が終わったらバッファを入れ替えて、受信した内容を見えるようにする
spi_flip:
    call spi_wait
    in al,0xE1
    xor al,0x40
    out 0xE1,al
    ret

; AL = 受信バッファ[0]
rx0:
    xor bx,bx
; AL = 受信バッファ[BX]
rx_at:
    mov ax,RXBANK
    out 0xD4,ax
    push ds
    mov ax,0x3000
    mov ds,ax
    mov al,[bx]
    pop ds
    ret

; 1バイト受信して AL に
xfer_byte:
    mov ax,SPI_READ
    call spi_go
    call spi_flip
    jmp rx0

cs_high:
    and word [SPICFG],~0x3000
    mov ax,SPI_READ
    call spi_go
    jmp spi_wait

cs_low:
    or word [SPICFG],SPI_TF
    mov ax,SPI_READ
    call spi_go
; カードが 0xFF を返すまで待つ
tf_wait_ready:
    push cx
    push bx
    mov cx,20000
.l:
    call xfer_byte
    cmp al,0xFF
    je .ok
    loop .l
    mov al,0xE2
    mov si,msg_sd
    jmp fatal
.ok:
    pop bx
    pop cx
    ret

; AX 回の VBlank を待つ
wait_ticks:
    add ax,[TICK]
    mov [TOUT],ax
.l:
    hlt
    mov ax,[TICK]
    sub ax,[TOUT]
    js .l
    ret

; ---------------------------------------------------------------------------
; 文字表示 (画面Aを使う。フォントはタイル 0-63 = ASCII 32-95)
; ---------------------------------------------------------------------------
text_screen:
    pusha
    mov byte [OVL],0        ; 時間表示 (スクリーン2) は消す
    mov al,1
    out 0x00,al
    push ds
    push es
    xor ax,ax
    mov es,ax
    mov di,MAPA
    mov cx,1024
    rep stosw
    push cs
    pop ds
    mov si,font
    mov di,TILEA
    mov cx,64*16
    rep movsw
    pop es
    pop ds
    mov word [0xFE00],0x0000
    mov word [0xFE02],0x0FFF
    mov word [0xFE04],0x08AF
    mov al,MAPA_SEL
    out 0x07,al
    popa
    ret

; SI = 文字列 (CS), DI = POS(行,桁)
print:
    push ax
.l:
    mov al,[cs:si]
    inc si
    or al,al
    jz .e
    sub al,32
    xor ah,ah
    mov [MAPA+di],ax
    add di,2
    jmp .l
.e:
    pop ax
    ret

print_hex8:
    push ax
    push ax
    shr al,4
    call .nib
    pop ax
    and al,15
    call .nib
    pop ax
    ret
.nib:
    add al,'0'
    cmp al,'9'
    jbe .d
    add al,7
.d:
    sub al,32
    xor ah,ah
    mov [MAPA+di],ax
    add di,2
    ret

; エラー画面を出して止まる。SI = 文字列, AL = コード
fatal:
    push ax
    push si
    call audio_stop
    call text_screen
    pop si
    mov di,POS(7,2)
    call print
    mov si,msg_code
    mov di,POS(9,2)
    call print
    pop ax
    call print_hex8
    add di,2
    mov al,[STAGE]
    call print_hex8
    ; 診断用: 映像チャンク(読込/再生) 音声チャンク(読込/再生) 次のチャンク コマ 音ズレ補正回数
    mov di,POS(11,2)
    mov bx,diag_tab
.dg:
    mov si,[cs:bx]
    or si,si
    jz halt
    mov ax,[si]
    call print_hex16
    add di,2
    add bx,2
    cmp di,POS(11,27)
    jb .dg
    mov di,POS(12,2)
    jmp .dg
halt:
    sti
.h:
    hlt
    jmp .h

diag_tab:    dw VL, VCI, ALD, ACI, FC, FRAMEI, AVFIX, 0

%ifdef BENCH
; ---------------------------------------------------------------------------
; 実機の速さの測定 (-DBENCH のときだけ)。再生はせず、結果を画面に出して止まる。
;  1) microSD の読み込み: 先読みの後、最大 4096 セクタ読むのにかかった VBlank 数
;  2) 展開: 読み込み済みのコマを、待たずに続けて展開するのにかかった VBlank 数 (音は鳴らす)
; ---------------------------------------------------------------------------
bench:
    mov ax,[NSECT]
    mov [BS0],ax
    mov ax,[TICK]
    mov [BT0],ax
.rd:
    call stream_step
    or al,al
    jz .rdend
    mov ax,[NSECT]
    sub ax,[BS0]
    cmp ax,4096
    jb .rd
.rdend:
    mov ax,[TICK]
    sub ax,[BT0]
    mov [BT1],ax
    mov ax,[NSECT]
    sub ax,[BS0]
    mov [BS1],ax
    mov word [FRAMEI],0
    call audio_start
    mov ax,[TICK]
    mov [BT0],ax
    xor cx,cx
.dec:
    mov ax,[VCI]
    add ax,2
    cmp ax,[VL]
    jae .decend
    mov ax,[FRAMEI]
    cmp ax,[FRAMEN]
    jae .decend
    push cx
    call decode_frame
    pop cx
    xor byte [BUF],1
    inc word [FRAMEI]
    inc cx
    jmp .dec
.decend:
    mov [BF2],cx
    mov ax,[TICK]
    sub ax,[BT0]
    mov [BT2],ax
    call audio_stop
    ; 動画が何本かあるときは、1本ずつ測って結果を覚え、最後に表にする (種類ごとの重さの測定用)
    mov bl,[ITEM]
    xor bh,bh
    shl bx,3
    mov ax,[BS1]
    mov [BRES+bx],ax
    mov ax,[BT1]
    mov [BRES+bx+2],ax
    mov ax,[BF2]
    mov [BRES+bx+4],ax
    mov ax,[BT2]
    mov [BRES+bx+6],ax
    cmp byte [CFGCOUNT],1
    jbe .one
    inc byte [ITEM]
    mov al,[ITEM]
    cmp al,[CFGCOUNT]
    jae .table
    cmp al,8
    jae .table
    jmp play_item
.table:
    call text_screen
    mov si,msg_bench
    mov di,POS(1,1)
    call print
    xor bx,bx
    xor dx,dx
.trow:
    mov ax,dx
    add ax,3
    shl ax,6
    add ax,2
    mov di,ax
    mov ax,dx
    inc ax
    mov [MAPA+di],ax
    add word [MAPA+di],'0'-32
    add di,4
    mov ax,[BRES+bx+4]      ; コマ数
    call print_dec16
    add di,2
    mov ax,[BRES+bx+6]      ; VBlank 数
    call print_dec16
    add di,2
    mov ax,[BRES+bx+2]      ; SD (4096 セクタの VBlank 数)
    call print_dec16
    add bx,8
    inc dx
    cmp dl,[CFGCOUNT]
    jae .tend
    cmp dx,8
    jb .trow
.tend:
    jmp halt
.one:
    call text_screen
    mov si,msg_bench
    mov di,POS(2,2)
    call print
    mov si,msg_bsd
    mov di,POS(5,2)
    call print
    mov ax,[BS1]
    call print_dec16
    add di,2
    mov ax,[BT1]
    call print_dec16
    mov si,msg_bdec
    mov di,POS(7,2)
    call print
    mov ax,[BF2]
    call print_dec16
    add di,2
    mov ax,[BT2]
    call print_dec16
    mov si,msg_blines
    mov di,POS(9,2)
    call print
    mov ax,[LINESW]
    call print_dec16
    add di,2
    mov al,[CFGK]
    xor ah,ah
    call print_dec16
    jmp halt
%endif

%ifdef STATS
; ---------------------------------------------------------------------------
; 実機の試し用 (-DSTATS のときだけ)。動画を最後まで普通に再生し、終わるたびに
; 「番号 コマ数 遅れたコマ 読み込み待ち 音ズレ補正」の表を出す。A で次の動画へ。全部終わったら止まる。
; ---------------------------------------------------------------------------
stats_item_end:
    call audio_stop
    mov bl,[ITEM]
    xor bh,bh
    shl bx,3
    mov ax,[FRAMEI]
    mov [SRES+bx],ax
    mov ax,[LATE]
    mov [SRES+bx+2],ax
    mov ax,[STALL]
    mov [SRES+bx+4],ax
    mov ax,[AVFIX]
    mov [SRES+bx+6],ax
    call text_screen
    mov si,msg_stats
    mov di,POS(1,1)
    call print
    mov si,msg_shead
    mov di,POS(2,1)
    call print
    xor bx,bx
    xor dx,dx
.row:
    mov ax,dx
    add ax,4
    shl ax,6
    add ax,2
    mov di,ax
    mov ax,dx
    inc ax
    add ax,'0'-32
    mov [MAPA+di],ax
    add di,4
    mov ax,[SRES+bx]
    call print_dec16
    add di,2
    mov ax,[SRES+bx+2]
    call print_dec16
    add di,2
    mov ax,[SRES+bx+4]
    call print_dec16
    add di,2
    mov ax,[SRES+bx+6]
    call print_dec16
    add bx,8
    inc dl
    cmp dl,[ITEM]
    jbe .row
    mov al,[ITEM]
    inc al
    cmp al,[CFGCOUNT]
    jae .last
    cmp al,8
    jae .last
    mov [ITEM],al
    mov si,msg_snext
    mov di,POS(14,1)
    call print
.wait:
    hlt
    call take_keys
    test al,0x04            ; A
    jz .wait
    jmp play_item
.last:
    mov si,msg_send
    mov di,POS(14,1)
    call print
    jmp halt

msg_stats:   db 'TEST RESULT',0
msg_shead:   db '# FRAME  LATE STALL AVFIX',0
msg_snext:   db 'PRESS A: NEXT VIDEO',0
msg_send:    db 'ALL DONE',0
%endif

%if %isdef(BENCH) || %isdef(STATS)
; AX を10進5桁で DI に書いて進む (5桁 + 空白1)
print_dec16:
    push ax
    push bx
    push cx
    push dx
    mov bx,10
    mov cx,5
    add di,8
.l:
    xor dx,dx
    div bx
    add dl,'0'-32
    xor dh,dh
    mov [MAPA+di],dx
    sub di,2
    loop .l
    add di,12
    pop dx
    pop cx
    pop bx
    pop ax
    ret

msg_bench:   db 'SPEED TEST DONE',0
msg_bsd:     db 'SD  ',0
msg_bdec:    db 'DEC ',0
msg_blines:  db 'LK  ',0
%endif

print_hex16:
    push ax
    mov al,ah
    call print_hex8
    pop ax
    jmp print_hex8

msg_title:   db 'WS VIDEO PLAYER',0
msg_card:    db 'SD CARD...',0
msg_file:    db 'OPENING FILE...',0
msg_loading: db 'LOADING...',0
msg_sd:      db 'SD CARD ERROR',0
msg_fat:     db 'NO FAT32 PARTITION',0
msg_nofile:  db 'FILE NOT FOUND:',0
msg_root:    db 'COPY IT TO THE SD ROOT',0
msg_frag:    db 'FILE TOO FRAGMENTED',0
msg_bad:     db 'BAD DATA FILE',0
msg_read:    db 'SD READ ERROR',0
msg_code:    db 'CODE ',0

align 2
font:
    incbin "font.bin"