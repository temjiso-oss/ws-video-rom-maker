"""FAT32 のディスクイメージ(nileswan エミュレーター用の nileswan.img)に
ファイルを書き込む小さな道具。テスト専用。

    python tests/fat32img.py nileswan.img put ローカルファイル 8.3名 [--fragment N]
    python tests/fat32img.py nileswan.img ls

--fragment N : わざと N クラスタごとに1クラスタ空けて書く(断片化したファイルのテスト用)
"""
import struct
import sys


class Fat32:
    def __init__(self, path):
        self.f = open(path, 'r+b')
        mbr = self.read(0, 512)
        self.part = 0
        if mbr[0x52:0x57] != b'FAT32':
            self.part = struct.unpack_from('<I', mbr, 0x1C6)[0]
        bpb = self.read(self.part * 512, 512)
        assert bpb[0x52:0x57] == b'FAT32', 'FAT32 ではありません'
        self.bps, = struct.unpack_from('<H', bpb, 0x0B)
        self.spc = bpb[0x0D]
        self.rsvd, = struct.unpack_from('<H', bpb, 0x0E)
        self.nfats = bpb[0x10]
        self.fatsz, = struct.unpack_from('<I', bpb, 0x24)
        self.root, = struct.unpack_from('<I', bpb, 0x2C)
        tot, = struct.unpack_from('<I', bpb, 0x20)
        self.fat0 = self.part + self.rsvd
        self.data0 = self.fat0 + self.nfats * self.fatsz
        self.nclus = (tot - (self.data0 - self.part)) // self.spc + 2
        self.csize = self.bps * self.spc
        self.fat = bytearray(self.read(self.fat0 * 512, self.fatsz * 512))

    def read(self, off, n):
        self.f.seek(off)
        return self.f.read(n)

    def write(self, off, data):
        self.f.seek(off)
        self.f.write(data)

    def ent(self, c):
        return struct.unpack_from('<I', self.fat, c * 4)[0] & 0x0FFFFFFF

    def set(self, c, v):
        struct.pack_into('<I', self.fat, c * 4, v)

    def chain(self, c):
        out = []
        while 2 <= c < 0x0FFFFFF8:
            out.append(c)
            c = self.ent(c)
        return out

    def coff(self, c):
        return (self.data0 + (c - 2) * self.spc) * 512

    def find(self, name11, dclus=None):
        for c, i, e in self.dir_entries(dclus):
            if e[0] == 0:
                return None
            if e[:11] == name11 and e[11] != 0x0F and e[0] != 0xE5:
                return struct.unpack_from('<H', e, 20)[0] << 16 | struct.unpack_from('<H', e, 26)[0]
        return None

    def mkdir(self, name11):
        """ルートにフォルダを作る (あればそのクラスタ番号を返す)"""
        c = self.find(name11)
        if c:
            return c
        c = next(k for k in range(3, self.nclus) if self.ent(k) == 0)
        self.set(c, 0x0FFFFFFF)
        d = bytearray(self.csize)
        d[0:11] = b'.          '
        d[11] = 0x10
        struct.pack_into('<H', d, 20, c >> 16)
        struct.pack_into('<H', d, 26, c & 0xFFFF)
        d[32:43] = b'..         '
        d[43] = 0x10
        self.write(self.coff(c), bytes(d))
        self.add_entry(None, name11, c, 0, 0x10)
        return c

    def add_entry(self, dclus, name11, first, size, attr=0x20):
        for cc, i, e in self.dir_entries(dclus):
            if e[0] in (0, 0xE5):
                ent = bytearray(32)
                ent[:11] = name11
                ent[11] = attr
                struct.pack_into('<H', ent, 20, first >> 16)
                struct.pack_into('<H', ent, 26, first & 0xFFFF)
                struct.pack_into('<I', ent, 28, size)
                self.write(self.coff(cc) + i, bytes(ent))
                break
        else:
            raise RuntimeError('ディレクトリがいっぱいです')
        for n in range(self.nfats):
            self.write((self.fat0 + n * self.fatsz) * 512, bytes(self.fat))
        self.f.flush()

    def dir_entries(self, dclus=None):
        for c in self.chain(dclus or self.root):
            data = self.read(self.coff(c), self.csize)
            for i in range(0, self.csize, 32):
                yield c, i, data[i:i + 32]

    def ls(self):
        for c, i, e in self.dir_entries():
            if e[0] == 0:
                break
            if e[0] == 0xE5 or e[11] == 0x0F:
                continue
            hi, lo = struct.unpack_from('<H', e, 20)[0], struct.unpack_from('<H', e, 26)[0]
            size, = struct.unpack_from('<I', e, 28)
            first = hi << 16 | lo
            print(e[:11], size, 'clusters', len(self.chain(first)) if first else 0)

    def delete(self, name11, dclus=None):
        for c, i, e in self.dir_entries(dclus):
            if e[0] == 0:
                return
            if e[:11] == name11 and e[11] != 0x0F:
                first = struct.unpack_from('<H', e, 20)[0] << 16 | struct.unpack_from('<H', e, 26)[0]
                for k in self.chain(first):
                    self.set(k, 0)
                self.write(self.coff(c) + i, b'\xe5')

    def put(self, data, name11, fragment=0, dclus=None):
        self.delete(name11, dclus)
        need = max(1, -(-len(data) // self.csize))
        clus = []
        c = 3
        run = 0
        while len(clus) < need:
            if c >= self.nclus:
                raise RuntimeError('空きがありません')
            if self.ent(c) == 0:
                if fragment and run == fragment:
                    run = 0
                    c += 1
                    continue
                clus.append(c)
                run += 1
            c += 1
        for a, b in zip(clus, clus[1:] + [0x0FFFFFFF]):
            self.set(a, b)
        for k, cl in enumerate(clus):
            chunk = data[k * self.csize:(k + 1) * self.csize]
            self.write(self.coff(cl), chunk.ljust(self.csize, b'\0'))
        self.add_entry(dclus, name11, clus[0], len(data))
        return clus


def name83(s):
    base, _, ext = s.upper().partition('.')
    return (base.ljust(8) + ext.ljust(3)).encode('ascii')


def main():
    img = Fat32(sys.argv[1])
    print(f'part={img.part} spc={img.spc} cluster={img.csize} root={img.root} clusters={img.nclus}')
    if sys.argv[2] == 'ls':
        img.ls()
    elif sys.argv[2] == 'put':
        frag = int(sys.argv[sys.argv.index('--fragment') + 1]) if '--fragment' in sys.argv else 0
        data = open(sys.argv[3], 'rb').read()
        target = sys.argv[4].split('/')
        dclus = img.mkdir(name83(target[0])) if len(target) == 2 else None
        cl = img.put(data, name83(target[-1]), frag, dclus)
        print('wrote', len(data), 'bytes in', len(cl), 'clusters, first', cl[0])


if __name__ == '__main__':
    main()
