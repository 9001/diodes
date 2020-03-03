#!/usr/bin/env python2
# coding: utf-8
from __future__ import print_function, unicode_literals

import re
import os
import sys
import stat
import struct
import hashlib
import argparse
import platform
import subprocess as sp


"""
run this in a VM with no soundcard or network (or maybe some VNC);
any files provided as arguments will be displayed as a dotmatrix code

run vxr.py on the host-machine and vxr will send keyboard inputs to
control this vxt, reading the dotmatrix code and assembling the file

will probably offer to use qrcodes as an alternative in the future
but this was more fun to write

zero dependencies, should run on anything winXP or newer

Supported OS'es:
  windows (XP and newer)
  mac osx (any version)
  linux (any version)

Supported python versions:
  cpython 2.6 and later
  cpython 3.2 and later
  pypy2 any version probably
  pypy3 any version definitely
  ironpython 2.7.8 (maybe older ones too)
  jython 2.7.1 (maybe older ones too)

Unsupported:
  on windows + python <= 3.5: unicode filenames anywhere
  on windows + jython: unicode filenames as arguments (folders ok)
  on windows + ironpython: unicode filenames as arguments (folders ok)

Speed estimates with various arguments (if any):
  vxt on winXP vboxsvga, vxr on linux 4.19 host:      45 kB/s
  vxt on linux 5.4.12 vbox-vmsvga, vxr on 4.19 host:  45 kB/s  -nf -nb
  vxt on jython, doesn't matter where:                 5 kB/s
  vxt on ironpython (cmd.exe on win10 LTSC 1809):     26 kB/s
  vxt in apple-terminal, osx 10.15.3                  32 kB/s  -nb -nf -fc
  vxt in cmd.exe, win10 LTSC 1809:                    27 kB/s  -nf -nb
  vxt in mintty on win10 LTSC 1809:                    6 kB/s  -nh -nb
  xfce4-terminal on linux 4.19 (no vm/vnc):           35 kB/s  -nf -nb
  xfce4-terminal on linux 4.19 (no vm/vnc):           39 kB/s  -nf -nb -nh
"""


PY2 = sys.version_info[0] == 2
WINDOWS = sys.platform in ["win32", "cli"] or sys.platform.startswith("java")
FS_ENC = sys.getfilesystemencoding()


def enc_vle(value):
    """takes an integer value, returns a bytearray"""
    if value >= 0xFFFFFFFF:
        print("writing unreasonably large VLE (0d{0})".format(value))

    ret = bytearray()
    while value >= 0x80:
        value, octet = divmod(value, 0x80)
        ret.append(octet + 0x80)

    ret.append(value)
    return bytes(ret)


def bstr2bits(buf):
    lim = 700 * 400 / 8
    if len(buf) > lim:
        raise Exception("{0} > {1}".format(len(buf), lim))

    ints = struct.unpack(b"B" * len(buf), buf)
    bits = "".join(bin(x)[2:].zfill(8) for x in ints)
    return [x == "1" for x in bits]


def calibration_boxes(w, h, use_halfblocks):
    """generates the calibration screen"""
    scr = [
        " " * w,
        " " + "█" * (w - 2) + " ",
        " █   █" + (" " * (w - 13)) + " █   █ ",
        " █ ▄ █" + (" " * (w - 13)) + " █ ▀ █ ",
        " █   █" + (" " * (w - 13)) + " █   █ ",
        " █████" + (" █" * w)[: w - 13] + " █████ ",
    ]
    for n in range(h - 9):
        if n % 2 == 0:
            scr.append(" █" + (" " * (w - 4)) + "█ ")
        else:
            scr.append(" █   █" + (" " * (w - 8)) + "█ ")

    scr.append(" █" + (" " * (w - 4)) + "█ ")
    scr.append(" " + "█" * (w - 2) + " ")
    scr.append(" " * (w - 1))

    if not use_halfblocks:
        scr[3] = scr[3].replace("▄", " ").replace("▀", " ")

    return scr


def boxes2bg(lines):
    """filter which translates █ to white-bg"""
    ret = []
    ptn = re.compile(r"(█+)", flags=re.U)
    for ln in lines:
        ln = (
            ptn.sub(r"\033[1;47m\1\033[0;37m", ln)
            .replace("█", " ")
            .replace("\033[0;37m\033[40m", "\033[40m")
        )
        ret.append(ln)

    return ret


def fillbg(lines):
    """filter which fills the background with black"""
    ret = ["\033[0;37;40m\033[J"]
    ptn = re.compile(r"( +)")
    for ln in lines:
        ln = ptn.sub(r"\033[40m\1", ln)
        ret.append(ln)

    return ret


if sys.platform.startswith("linux") or sys.platform in ["darwin", "cygwin", "msys"]:
    VT100 = True
    TERM_ENCODING = sys.stdout.encoding

    import fcntl  # pylint: disable=import-error
    import termios  # pylint: disable=import-error

    def getch():
        ch = 0
        fd = sys.stdin.fileno()
        old_cfg = termios.tcgetattr(fd)
        tmp_cfg = termios.tcgetattr(fd)
        tmp_cfg[3] &= ~termios.ICANON & ~termios.ECHO
        try:
            # tty.setraw(fd)
            termios.tcsetattr(fd, termios.TCSADRAIN, tmp_cfg)
            ch = sys.stdin.read(1)
            sys.stdout.flush()
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_cfg)
        return ch

    def termsize():
        env = os.environ

        def ioctl_GWINSZ(fd):
            try:
                sz = fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\x00" * 8)
                return struct.unpack(b"HHHH", sz)[:2]
            except Exception as e:
                print("std fd {0} failed: {1}".format(fd, repr(e)))
                return

        cr = ioctl_GWINSZ(0) or ioctl_GWINSZ(1) or ioctl_GWINSZ(2)
        if not cr:
            try:
                fd = os.open(os.ctermid(), os.O_RDONLY)
                cr = ioctl_GWINSZ(fd)
                os.close(fd)
            except:
                print("term fd {0} failed".format(fd))
                pass

        if not cr:
            try:
                cr = [env["LINES"], env["COLUMNS"]]
            except:
                print("env failed")
                cr = [25, 80]

        return int(cr[1]), int(cr[0])

    def wprint(txt):
        print("\033[H\033[0;1m" + txt.replace("\n", "\033[K\n") + "\033[J", end="")
        sys.stdout.flush()


elif sys.platform in ["win32", "cli"]:
    # plain python on windows +
    # ironpython ("cli")

    VT100 = False
    TERM_ENCODING = "cp437"
    PYPY = platform.python_implementation() == "PyPy"

    import msvcrt  # pylint: disable=import-error

    def getch():
        while msvcrt.kbhit():
            msvcrt.getch()

        rv = msvcrt.getch()
        try:
            return rv.decode(TERM_ENCODING, "replace")
        except:
            return rv  # pypy bug: getch is str()

    from ctypes import windll, create_string_buffer

    def termsize_native():
        ret = None
        try:
            # fd_stdin, fd_stdout, fd_stderr = [-10, -11, -12]
            h = windll.kernel32.GetStdHandle(-12)
            csbi = create_string_buffer(22)
            ret = windll.kernel32.GetConsoleScreenBufferInfo(h, csbi)
        except:
            return None

        if not ret:
            return None

        # bufx, bufy, curx, cury, wattr, left, top, right, bottom, maxx, maxy
        left, top, right, bottom = struct.unpack(b"hhhhHhhhhhh", csbi.raw)[5:-2]

        return [right - left + 1, bottom - top + 1]

    def termsize_ncurses():
        try:
            return [
                int(sp.Popen(["tput", "cols"], stdout=sp.PIPE).communicate()[0]),
                int(sp.Popen(["tput", "lines"], stdout=sp.PIPE).communicate()[0]),
            ]
        except:
            return None

    def termsize():
        ret = termsize_native() or termsize_ncurses()
        if ret:
            return ret

        raise Exception(
            "powershell is not supported; use cmd on win10 or use cygwin\n" * 5
        )

    from ctypes import Structure, c_short, c_char_p

    class COORD(Structure):
        pass

    COORD._fields_ = [("X", c_short), ("Y", c_short)]

    wcp = TERM_ENCODING
    if wcp.startswith("cp"):
        wcp = wcp[2:]

    v = sp.Popen("chcp", stdout=sp.PIPE, shell=True).communicate()[0].decode("utf-8")
    if " {0}".format(wcp) not in v:
        _ = os.system("chcp " + wcp)  # fix moonrunes  # nosec: B605
        msg = "\n\n\n\n  your  codepage  was  wrong     ({0})\n\n  dont worry, i just fixed it    ({1})\n\n    please  run  me  again\n\n\n\n             -- vxt, 2019\n\n\n".format(
            v.split(":")[-1].strip(), wcp
        )
        try:
            sys.stdout.buffer.write(msg.encode("ascii"))
        except:
            sys.stdout.write(msg.encode("ascii"))

        exit()

    _ = os.system("cls")  # somehow enables the vt100 interpreter??  # nosec: B605

    def wprint_try1(txt):
        _ = os.system("cls")  # nosec: B605
        print(txt, end="")
        sys.stdout.flush()

    def wprint(txt):
        # observed in cmd.exe on win10 LTSC 1809:
        #   somehow print() can occasionally insert a single mojibake
        #   in the middle of output that's otherwise fine;
        #   hasn't happened yet with this approach (bank i bordet)
        #
        # _ = os.system("cls")  # nosec: B605
        h = windll.kernel32.GetStdHandle(-11)
        c = ("\n" + txt).encode(TERM_ENCODING, "replace")
        windll.kernel32.WriteConsoleA(h, c_char_p(c), len(c), None, None)


elif sys.platform.startswith("java"):
    # this will be fun
    TERMSIZE = [200, 60]
    # TERMSIZE = [250, 80]
    # TERMSIZE = [79, 25]

    def termsize():
        return TERMSIZE

    def getch():
        try:
            return sys.stdin.readline()[0]
        except:
            raise Exception("probably you pressing ctrl-c")

    def wprint(txt):
        msg = ""
        w = TERMSIZE[0]
        for ln in txt.replace("\r", "").split("\n"):
            while len(ln) > w:
                msg += "\r\n" + ln[:w]
                ln = ln[w:]

            if len(msg) == 0 or len(ln) > 0:
                msg += "\r\n" + ln

        _ = os.system("cls")  # nosec: B605
        print(msg[2:], end="")
        sys.stdout.flush()


else:
    raise Exception("unsupported platform: {0}".format(sys.platform))


class Vxt(object):
    def __init__(self, ar, files):
        """
        need list of figments (thx kurisu) that have appeared in frames;
        fields for headers:
        - rel_path: rel-path from $PWD
        - last_mod: last-modified, int(unix-sec)
        - checksum: sha512[:16]
        fields for payloads:
        - just the data

        each vx-1 frame consists of:
        - sha512(vfn+data)[:4]
        - vfn = vle(frameno)
        - data;
          (80*24)-(1+16+32) = 1920-49 = 1871 bit = 233 byte
          (80*48)-(2+16+32) = 3840-50 = 3790 bit = 473 byte

        fixed offset into first frame since vx-1 starts with:
        - vle(vxt_ver)
        - vle(num_files)
        - vle(num_bytes)

        need list of frames encoded so far (frame numbers are vle),
        each entry is list of figments included in the frame;
        - bit offset into figment
        - position of figment in frame
        - first/last fig bit displayed in frame
        """
        self.ar = ar
        self.files = files
        self.frames = []
        self.figs = []

        for fo in self.files:
            fo["pl"] = False
            header = self.gen_header(fo, True)
            fo["len"] = len(header)
            self.figs.append(fo)
            self.figs.append({"len": fo["sz"], "pl": True})

        # import pprint; pprint.pprint(self.figs); return

        nfiles = len(self.files)
        nbytes = sum(x["sz"] for x in self.files)
        self.header0 = enc_vle(1) + enc_vle(nfiles) + enc_vle(nbytes)

        self.w, self.h = termsize()
        self.nbits = self.w * self.h - 1
        if self.ar.halfs:
            self.nbits *= 2

    def draw(self, scr, is_cali=False):
        if self.ar.b:
            scr = fillbg(scr)

        if self.ar.f or (is_cali and self.ar.fc):
            scr = boxes2bg(scr)

        wprint("".join(scr))

    def gen_header(self, fo, fake_csum):
        if "csum" not in fo and not fake_csum:
            # ufn = fo["fn"].decode("utf-8", "replace")
            # print("hashing [{0}]".format(ufn))

            hasher = hashlib.sha512()
            with open(fo["fn"], "rb", 512 * 1024) as f:
                while True:
                    buf = f.read(512 * 1024)
                    if not buf:
                        break

                    hasher.update(buf)

            fo["csum"] = hasher.digest()[:16]

        csum = b"x" * 16 if fake_csum else fo["csum"]

        rfn = fo["fn"]
        if rfn.find(b":\\") == 1:
            rfn = rfn[3:]

        rfn = rfn.replace(b"\\", b"/")
        while rfn.startswith(b"/"):
            rfn = rfn[1:]

        # fmt: off
        return (
            enc_vle(fo["sz"])
            + enc_vle(int(fo["ts"]))
            + csum
            + enc_vle(len(rfn))
            + rfn
        )
        # fmt: on

    def get_frame_info(self, nframe):
        if nframe > 0 and nframe > len(self.frames) - 1:
            self.get_frame_info(nframe - 1)

        if len(self.frames) > nframe:
            return self.frames[nframe]

        vframe = enc_vle(nframe)
        bits_spent = (4 + len(vframe)) * 8

        fig_idx = 0
        fig_bit = 0
        if nframe == 0:
            bits_spent += len(self.header0) * 8
        else:
            pframe = self.frames[nframe - 1]
            fig_idx = self.figs.index(pframe[-1]["fig"])
            fig_bit = pframe[-1]["bit2"]

        displayed = []
        while bits_spent < self.nbits and fig_idx < len(self.figs):
            bits_left = self.nbits - bits_spent
            fig = self.figs[fig_idx]
            if fig_bit >= fig["len"] * 8:
                fig_idx += 1
                fig_bit = 0
                continue

            bit1 = fig_bit
            bit2 = bit1 + bits_left
            if bit2 > fig["len"] * 8:
                bit2 = fig["len"] * 8

            displayed.append(
                {"fig": fig, "bit1": bit1, "bit2": bit2, "ofs": bits_spent}
            )

            fig_bit = bit2
            bits_spent += bit2 - bit1

        if not displayed:
            return None  # end of stream

        self.frames.append(displayed)
        return displayed

    def gen_frame(self, nframe):
        inf = self.get_frame_info(nframe)
        if not inf:
            return None

        bits = bstr2bits(enc_vle(nframe))
        if nframe == 0:
            bits += bstr2bits(self.header0)

        for chunk in inf:
            fig = chunk["fig"]
            bit1 = chunk["bit1"]
            bit2 = chunk["bit2"]
            if not fig["pl"]:
                buf = self.gen_header(fig, False)
                bits += bstr2bits(buf)[bit1:bit2]
            else:
                byte1, trunc1 = divmod(bit1, 8)
                byte2 = int((bit2 + 7) / 8)
                if byte2 > fig["len"]:
                    byte2 = fig["len"]

                metafig = self.figs[self.figs.index(fig) - 1]
                with open(metafig["fn"], "rb") as f:
                    f.seek(byte1)
                    need = byte2 - byte1
                    buf = f.read(need)
                    while len(buf) < need:
                        b2 = f.read(need - len(buf))
                        if not b2:
                            ex = "read error, [{0}] at [{1}]".format(
                                fig["fn"], byte1 + len(buf)
                            )
                            raise Exception(ex)

                        buf += b2

                    bits += bstr2bits(buf)[trunc1:][: bit2 - bit1]

        # zero-padding to get the correct checksum
        pad = (self.w * self.h - 1) * (2 if self.ar.halfs else 1)
        pad -= len(bits) + 4 * 8
        bits += [False] * pad

        hashbuf = "".join(["1" if x else "0" for x in bits]).encode("utf-8")
        csum = bstr2bits(hashlib.sha512(hashbuf).digest()[:4])
        return csum + bits

    def rasterize(self, frameno):
        y_mul = 2 if self.ar.halfs else 1
        if True:
            # the real pattern
            bits = self.gen_frame(frameno)
        else:
            # test pattern 1
            bits = [True, False] * (self.w * 2)
            bits += [False] * (self.w * ((self.h * y_mul) - 8))
            bits += ([False, True] * (self.w * 2))[:-y_mul]

            # test pattern 2
            bits = b"hello world "
            bits = bits * int((8 + self.nbits / len(bits)) / 8)
            bits = bstr2bits(bits)[: self.nbits]

        if not bits:
            return ""

        # all rows except last fills entire screen width;
        # last row leaves a blank cell at the end,
        # last cell covers two last rows if halfs enabled (trunc 1 bit on each)
        fullw_bits = (self.w * (self.h - 1)) * y_mul
        range_fullwidth = range(0, fullw_bits, self.w)
        range_sans_one = range(fullw_bits, fullw_bits + self.w * 2, (self.w - 1))

        rows1 = [bits[x : x + self.w] for x in range_fullwidth]
        rows2 = [bits[x : x + self.w - 1] for x in range_sans_one]
        rows = rows1 + rows2
        while rows and not rows[-1]:
            rows.pop()

        scr = ""
        if not self.ar.halfs:
            for row in rows:
                scr += "".join(["█" if x else " " for x in row])
        else:
            for r1, r2 in zip(rows[::2], rows[1::2]):
                for v1, v2 in zip(r1, r2):
                    if v1 and v2:
                        scr += "█"
                    elif v1:
                        scr += "▀"
                    elif v2:
                        scr += "▄"
                    else:
                        scr += " "

        return scr

    def run(self):
        scr = calibration_boxes(self.w, self.h, self.ar.halfs)
        self.draw(scr, True)

        cur_frame = -1
        pend = [-1, None]
        while True:
            k = getch()
            # print("[{}]".format(k))

            if k in ["\x03", "k"]:
                break
            if k == "a":
                cur_frame = max(cur_frame - 1, 0)
            if k == "d":
                cur_frame += 1

            if cur_frame < 0:
                continue

            if pend[0] == cur_frame:
                scr = pend[1]
            else:
                scr = self.rasterize(cur_frame)

            if not scr:
                cur_frame -= 1
                continue

            self.draw(scr)

            # rasterize the next frame and stash it for later
            if k == "d":
                pend = [cur_frame + 1, self.rasterize(cur_frame + 1)]


def main():
    ap = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        prog="vxt",
        description="transfer files as dotmatrix patterns",
        epilog="hints:\n  use -nh if your font is broken (centos 6)\n  use -nf -nb on slow terminals (win10 default)\n\nexample:\n  vxt.py some.file some.folder",
    )
    # fmt: off
    ap.add_argument("-nh", dest="halfs", action="store_false", help="disable half-blocks (50%% speed)",)
    if WINDOWS:
        ap.add_argument("-f", dest="f", action="store_true", help="enable full-block filling")
        ap.add_argument("-b", dest="b", action="store_true", help="enable background filling")
    else:
        ap.add_argument("-nf", dest="f", action="store_false", help="disable full-block filling")
        ap.add_argument("-nb", dest="b", action="store_false", help="disable background filling")

    ap.add_argument("-fc", action="store_true", help="always fill cali fullblocks")
    ap.add_argument("files", metavar="file", nargs="+", help="files to transmit")
    ar = ap.parse_args()
    # fmt: on

    fns = []
    skipped = []
    for fn in ar.files:
        if not isinstance(fn, (bytes, bytearray)):
            fn = fn.encode(FS_ENC, "replace")
        if os.path.isfile(fn):
            fns.append(fn)
        elif os.path.isdir(fn):
            for walk_root, walk_dirs, walk_files in os.walk(fn):
                walk_dirs.sort()
                for walk_fn in sorted(walk_files):
                    fns.append(os.path.join(walk_root, walk_fn))
        else:
            skipped.append(fn)

    files = []
    for fn in fns:
        sr = os.stat(fn)
        if not stat.S_ISREG(sr.st_mode):
            skipped.append(fn)
            continue

        sz = sr.st_size
        ts = sr.st_mtime

        # create "csum" (md5) when encountered
        files.append({"fn": fn, "sz": sz, "ts": ts})

    if skipped:
        print("skipped some files (non-folder / non-regular):")
        for fn in skipped:
            print(fn.decode("utf-8", "replace"))

        print("abort with ctrl-c, or hit enter to accept and continue")
        if PY2:
            raw_input()  # noqa: F821  # pylint: disable=undefined-variable
        else:
            input()  # nosec: B322

    Vxt(ar, files).run()


if __name__ == "__main__":
    main()


# cd c:\users\ed\dev\vx
#
# copy C:\Users\ed\Downloads\argparse-1.4.0.tar.gz\dist\argparse-1.4.0.tar\argparse-1.4.0\argparse.py to C:\Python26\Lib
# c:\Python26\python.exe vxt.py c:\windows\notepad.exe
# c:\Python27\python.exe vxt.py c:\windows\notepad.exe
# c:\Python32\python.exe vxt.py c:\windows\notepad.exe
# c:\Python33\python.exe vxt.py c:\windows\notepad.exe
# c:\Python34\python.exe vxt.py c:\windows\notepad.exe
# c:\Python35\python.exe vxt.py c:\windows\notepad.exe
# c:\Python36\python.exe vxt.py c:\windows\notepad.exe "c:\users\ed\moon\ハム - あの夏がきこえる.opus"
# "c:\Program Files\Java\jre1.8.0_241\bin\java.exe" -jar c:\users\ed\bin\jython-standalone-2.7.1.jar vxt.py c:\windows\notepad.exe
# C:\Users\ed\bin\jdk-13.0.1\bin\java.exe -jar c:\users\ed\bin\jython-standalone-2.7.1.jar vxt.py c:\windows\notepad.exe "c:\users\ed\moon"
# "c:\Program Files\IronPython 2.7\ipy.exe" vxt.py c:\windows\notepad.exe "c:\users\ed\moon"
