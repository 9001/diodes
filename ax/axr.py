#!/usr/bin/env python3

import sys
import time
import struct
import threading
import subprocess as sp


def eprint(*a, **ka):
    ka["file"] = sys.stderr
    print(*a, **ka)


class A(object):
    def __init__(self, nch, profile):
        self.nch = nch
        self.profile = profile
        self.dec = []
        self.emitted = 0
        self.procs_alive = self.nch
        self.mutex = threading.Lock()

    def rd(self, ch, p):
        # eprint(ch)
        padding = True
        # with open(f"{ch}.rd", "wb") as f:
        if True:
            while True:
                buf = p.stdout.read(256)
                # eprint(ch, len(buf))
                if not buf:
                    break

                if padding:
                    buf = buf.lstrip(b"\x00")
                    if buf.startswith(b"\xff"):
                        padding = False
                        buf = buf[1:]
                    else:
                        continue

                # f.write(buf)
                with self.mutex:
                    self.dec[ch] += buf

        with self.mutex:
            self.procs_alive -= 1

    def emit(self):
        # check for complete frames to emit
        szs = []
        with self.mutex:
            for n, buf in enumerate(self.dec):
                if len(buf) < 4:
                    return False

                magic, sz = struct.unpack(">HH", buf[:4])
                if magic != 0xCADE:
                    if not sz:
                        return False

                    m = f"axr: FATAL: desync @ sample {self.emitted} ch{n} magic {magic:04x} sz {sz} hex {sz:04x}"
                    eprint(m)
                    try:
                        eprint("\\x" + buf[:64].hex(" ").replace(" ", "\\x"))
                    except Exception as ex:
                        eprint(buf[:64].hex())
                        eprint(ex)
                    sys.exit(1)

                if len(buf) < sz + 4:
                    return False

                szs.append(sz)

            bufs = [x[4 : sz + 4] for x, sz in zip(self.dec, szs)]
            self.dec = [x[sz + 4 :] for x, sz in zip(self.dec, szs)]

        if not bufs[0]:
            return False

        for n in range(1, len(bufs)):
            if len(bufs[n]) < len(bufs[n - 1]):
                eprint(f"axr: WARNING: ch{n} shorter than ch0")
                bufs[n] += b"\x00" * 4

        # mux and write
        dec = [y for x in zip(*bufs) for y in x]
        dec = struct.pack(f"{len(dec)}B", *dec)[: sum(szs)]
        sys.stdout.buffer.write(dec)
        # eprint("emit", len(dec))

        self.emitted += len(bufs[0])
        return True

    def run(self):
        # 1ch f32le 44k1
        header = b"RIFFH\x0f<\x1eWAVEfmt \x10\x00\x00\x00\x03\x00\x01\x00D\xac\x00\x00\x10\xb1\x02\x00\x04\x00 \x00fact\x04\x00\x00\x00\xc0\x03\x8f\x07PEAK\x10\x00\x00\x00\x01\x00\x00\x00\xd6\xe0\x1d^\x84\x121<O6\xdf\x04data\x00\x0f<\x1e\xe4^\xd25\xd5\xd0?\xb6k\x92a68s5\xb6"

        procs = []
        # fds = [open(f"{n}.rp", "wb") for n in range(self.nch)]
        for ch in range(self.nch):
            self.dec.append(b"")
            cmd = ["./quiet-decode", self.profile, "/dev/stdin"]
            p = sp.Popen(cmd, stdin=sp.PIPE, stdout=sp.PIPE)
            threading.Thread(target=self.rd, args=(ch, p), daemon=True).start()
            p.stdin.write(header)
            procs.append(p)

        rem = b""
        vhist = []
        runtime = 0
        while True:
            buf = sys.stdin.buffer.read(65535)
            if not buf:
                break

            buf = rem + buf
            n = len(buf) % (self.nch * 4)
            if n:
                rem = buf[-n:]
                buf = buf[:-n]
            else:
                rem = b""

            buf = [x[0] for x in struct.iter_unpack("f", buf)]
            peak = max(max(buf), abs(min(buf)))
            vhist = vhist[-5:] + [peak]
            vol = max(vhist)
            eprint(f" VOL {int(vol * 100)} %\n\033[A", end="")

            for n in range(self.nch):
                b = buf[n :: self.nch]
                b = struct.pack(f"{len(b)}f", *b)  # funfact: lossless
                # fds[n].write(b)
                procs[n].stdin.write(b)

            self.emit()
            if peak > 0.1:
                runtime += 1
            elif vol < 0.1 and runtime > 4:
                eprint("axr: INFO: input went silent; exiting")
                break

        for p in procs:
            p.stdin.close()

        spins = 0
        while True:
            if self.emit():
                spins = 0
                continue

            time.sleep(0.02)
            spins += 1
            if spins > 20:
                break


def main():
    nch = 2  # stereo
    profile = "w"
    A(nch, profile).run()


if __name__ == "__main__":
    main()


# f() { printf '%s\n' "$1" | tee v1 | ~/dev/diodes/ax/axt.py | tee pcm | ~/dev/diodes/ax/axr.py | tee v2; cat v1; cmp v1 v2 && return 0; hexdump -C v1; hexdump -C v2; return 1; }
# s=''; for c in a b c d e f g h i j k l m n o p q r s t u v w x y z; do s=$s$c; f $s || break; done
#
# (for ((a=1; a<255; a++)); do x=$(printf '\\x%02x' $a); printf "$x$x$x$x"; head -c 124 /dev/zero; done) | tee v1 | ~/dev/diodes/ax/axt.py | tee pcm | ~/dev/diodes/ax/axr.py > v2; cmp v1 v2
#
# p=w; (cat dbg/header-44khz.wav; cat ~/Videos/dashcam.webm | ./quiet-encode $p) | tee pcm | ffmpeg -re -v warning -ar 96000 -f f32le -i - -f f32le - -filter_complex "[a:0]showspectrum=s=1024x576:fps=30:legend=1:slide=scroll:color=intensity:fscale=lin:orientation=horizontal,crop=1280:640,format=yuv420p[vo]" -map "[vo]" -f sdl - | ./quiet-decode $p /dev/stdin | pv -Wapterbi 0.5 | cmp ~/Videos/dashcam.webm
#
# ./axt.py <"$f" | tee pcm | ffmpeg -re -v warning -ar 96000 -ac 2 -f f32le -i - -f f32le - -filter_complex "[a:0]showspectrum=s=1024x576:fps=30:legend=1:slide=scroll:color=intensity:fscale=lin:orientation=horizontal,crop=1280:640,format=yuv420p[vo]" -map "[vo]" -f sdl - | ./axr.py | pv -Wapterbi 0.5 | cmp "$f"
#
# ./axt.py <"$f" | ./axr.py | pv -apterbi 0.5 | tee f2 | cmp "$f"
