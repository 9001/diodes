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
        self.samples = []
        self.procs_alive = self.nch
        self.mutex = threading.Lock()

    # @profile
    def rd(self, ch, p):
        # eprint(ch)
        # with open(f"{ch}.tp", "wb") as f:
        if True:
            while True:
                if len(self.samples[ch]) > 1024 * 256:
                    time.sleep(0.1)
                    continue

                buf = p.stdout.read(1024 * 4)
                # eprint("rd", ch, len(buf))
                if not buf:
                    break

                # f.write(buf)
                buf = [x[0] for x in struct.iter_unpack("f", buf)]
                with self.mutex:
                    self.samples[ch] += buf

        with self.mutex:
            self.procs_alive -= 1

    # @profile
    def emit(self):
        # take as much buffered pcm data as possible
        # (length of the shortest channel)
        with self.mutex:
            # eprint("emit sizes", *[len(x) for x in self.samples])
            n = min(32768, min([len(x) for x in self.samples]))
            if not n:
                return False

            smps = [x[:n] for x in self.samples]
            self.samples = [x[n:] for x in self.samples]

        # mux and write
        # eprint(f"axt: DEBUG: emitting {n} samples")
        smp = [y for x in zip(*smps) for y in x]
        # eprint(smp)
        pcm = struct.pack(f"{len(smp)}f", *smp)
        sys.stdout.buffer.write(pcm)
        # eprint("emit", len(pcm))
        return True

    def emitter(self):
        spins = 0
        while True:
            if self.emit():
                spins = 0
                continue

            time.sleep(0.02)
            spins += 1
            if spins > 100:
                break

    # @profile
    def run(self):
        procs = []
        # fds = [open(f"{n}.td", "wb") for n in range(self.nch)]
        for ch in range(self.nch):
            self.samples.append([])
            cmd = ["./quiet-encode", self.profile]
            p = sp.Popen(cmd, stdin=sp.PIPE, stdout=sp.PIPE)
            pad = b"\x00" * 64 + b"\xff"
            p.stdin.write(pad)
            # fds[ch].write(pad)
            procs.append(p)
            threading.Thread(target=self.rd, args=(ch, p), daemon=True).start()

        emitter = threading.Thread(target=self.emitter)
        emitter.start()
        rem = b""
        while True:
            # read a slice of the original data,
            # max supported read size is 65535 (0xffff) per chan
            buf = sys.stdin.buffer.read((32767 - 4) * self.nch)
            # eprint("read", len(buf), "rem", len(rem))
            if buf:
                # still more data to read,
                # give each channel the same amount
                buf = rem + buf
                n = len(buf) % self.nch
                if n:
                    rem = buf[-n:]
                    buf = buf[:-n]
                else:
                    rem = b""

            elif not rem:
                break
            else:
                # send whatever's left
                eprint("axt: INFO: final iteration on source data")
                buf = rem
                rem = b""

            # one buffer for each channel,
            # prefix with magic and chunklen
            bufs = [buf[n :: self.nch] for n in range(self.nch)]
            bufs = [struct.pack(">HH", 0xCADE, len(b)) + b for b in bufs]
            # for buf, p, f in zip(bufs, procs, fds):
            for buf, p in zip(bufs, procs):
                # eprint("writing to quiet", len(buf))
                p.stdin.write(buf)
                # eprint("k")
                # f.write(buf)

            for b1, b2 in zip(bufs, bufs[1:]):
                if len(b1) != len(b2):
                    eprint("axt: INFO: uneven channel durations (fine if EOF)")

        for p in procs:
            p.stdin.write(b"\x00" * 128)
            p.stdin.close()

        emitter.join()


def main():
    nch = 2  # stereo
    profile = "w"
    A(nch, profile).run()


if __name__ == "__main__":
    main()


# python3 -m pip install --user line-profiler
# head -c $((1024*1024*2)) /dev/urandom | kernprof -l ~/dev/diodes/ax/axt.py > /dev/null
# python3 -m line_profiler axt.py.lprof
