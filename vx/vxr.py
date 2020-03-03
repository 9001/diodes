#!/usr/bin/env python3

import re
import os
import sys
import time
import struct
import logging
import hashlib
import binascii
import argparse
import platform
import threading
import subprocess as sp
from datetime import datetime

try:
    from pynput.keyboard import Key as KbdKey
    from pynput.keyboard import Controller as KbdController

    HAVE_PYNPUT = True
except ImportError:
    HAVE_PYNPUT = False


"""
this will read a dotmatrix-encoded file from a virtual display
(vxt running in a virtual-machine or a VNC on this machine), and
optionally simulate keyboard input to automatically re-assemble the file

note that there is no perspective correction of any kind so
this will only work with screencaptures, not cameras

will probably offer to use qrcodes as an alternative in the future
but this was more fun to write

dependencies:
  ffmpeg (mandatory; for screen capture)
  pynput (optional; for simulating keyboard input)

Supported OS'es:
  windows (XP and newer)
  mac osx (any version probably)
  linux (any version probably)

Supported python versions:
  cpython 3.6 and later
  pypy3 7.1.0 and later (recommended)
"""


if platform.python_implementation() == "PyPy":
    # c:\users\ed\bin\pypy3\pypy3.exe vxr.py
    cali_fps = 15
    cali_wait = 0.05
    dec_fps = 30
    dec_wait = 0.002
else:
    cali_fps = 5
    cali_wait = 0.2
    dec_fps = 10
    dec_wait = 0.02


LINUX = sys.platform.startswith("linux")
WINDOWS = sys.platform == "win32"
MACOS = sys.platform == "darwin"


debug = logging.debug
info = logging.info
warn = logging.warning
error = logging.error


class LoggerFmt(logging.Formatter):
    def format(self, record):
        if record.levelno == logging.DEBUG:
            ansi = "\033[01;30m"
        elif record.levelno == logging.INFO:
            ansi = "\033[0;32m"
        elif record.levelno == logging.WARN:
            ansi = "\033[0;33m"
        else:
            ansi = "\033[01;31m"

        ts = datetime.utcfromtimestamp(record.created)
        ts = ts.strftime("%H:%M:%S.%f")[:-3]
        return f"\033[0;36m{ts}{ansi} {record.msg}"


def dec_vle(buf):
    """
    takes an (iterator of) bytestring or bytearray,
    returns the decoded value and how many bytes it ate from buf
    """
    vret = 0
    vpow = 0
    nate = 0
    for v in buf:
        nate += 1
        if v < 0x80:
            return vret | (v << vpow), nate

        vret = vret | ((v - 0x80) << vpow)
        vpow += 7
        if vpow >= 63:
            warn("VLE too big (probably garbage data)")
            return -1, 0
        # if vpow > 31:
        #     warn("reading unreasonably large VLE ({0} bits)".format(vpow))

    warn("need more bytes for this VLE")
    return -1, 0


def bits2bstr(bits):
    """takes u'00101001' and returns b'A'"""
    return bytes([int(bits[x : x + 8], 2) for x in range(0, len(bits) - 7, 8)])


def bstr2bitstr(buf):
    ints = struct.unpack(b"B" * len(buf), buf)
    return "".join(bin(x)[2:].zfill(8) for x in ints)


def bstr2bits(buf):
    return [x == "1" for x in bstr2bitstr(buf)]


def get_avfoundation_devs():
    rhdr = re.compile(r"^\[AVFoundation input device @ 0x[0-9a-f]+\] (.*)")
    rcat = re.compile(r"^AVFoundation ([^ ]*)")
    rdev = re.compile(r"^\[([0-9]+)\] (.*)")
    ret = []
    # fmt: off
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-f", "avfoundation",
        "-list_devices", "true",
        "-i", "",
    ]
    # fmt: on
    in_video = False
    _, txt = sp.Popen(cmd, stderr=sp.PIPE).communicate()
    for ln in txt.split(b"\n"):
        ln = ln.decode("utf-8", "ignore").strip()
        m = rhdr.match(ln)
        if not m:
            continue

        ln = m.group(1)
        m = rcat.match(ln)
        if m:
            in_video = m.group(1) == "video"
            continue

        m = rdev.match(ln)
        if m and in_video:
            di, dt = m.groups()
            if "FaceTime" in dt:
                continue

            ret.append([di, dt])

    return ret


def get_x11_bounds():
    r = re.compile(r"^\[x11grab @ 0x[0-9a-f]+\] .* screen size ([0-9]+)x([0-9]+)")
    # fmt: off
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-f", "x11grab",
        "-s", "99999x99999",
        "-i", ":0.0+0,0"
    ]
    # fmt: on
    _, txt = sp.Popen(cmd, stderr=sp.PIPE).communicate()
    for ln in txt.split(b"\n"):
        m = r.match(ln.decode("utf-8", "ignore").strip())
        if m:
            return [int(x) for x in m.groups()]

    return 1024, 768


class FFmpeg(threading.Thread):
    def __init__(self, fps, w=None, h=None, x=None, y=None, dev=None, show_region=True):
        super(FFmpeg, self).__init__(name="ffmpeg")
        self.daemon = True

        self.w = w
        self.h = h
        self.x = x
        self.y = y
        self.dev = dev

        self.yuv_mutex = threading.Lock()
        self.yuv = None
        # fmt: off
        self.cmd = [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-v", "warning",
            "-color_range", "jpeg",
            "-flush_packets", "1",
            "-fflags", "+nobuffer",
            "-flags", "+low_delay"
        ]
        
        if LINUX:
            # defaults to 640x480 but prints max size if exceed
            src = ":0.0"
            if x is not None:
                src = f":0.0+{x},{y}"
            
            if w is None:
                debug("getting display size")
                self.w, self.h = get_x11_bounds()
                if x is not None:
                    self.w -= x
                    self.h -= y
            
            self.cmd.extend([
                "-f", "x11grab",
                "-show_region", "1" if show_region else "0",
                "-framerate", f"{fps}",
                "-s", f"{self.w}x{self.h}"
            ])
            
            self.cmd.extend(["-i", src])

        elif MACOS:
            # only does fullscreen + requires source device id
            self.cmd.extend([
                "-f", "avfoundation",
                "-pixel_format", "bgr0",
                "-framerate", f"{fps}",
                "-i", f"{dev}:none",
                "-r", f"{fps}"
            ])
            
            if w is not None:
                self.cmd.extend([
                    "-vf", f"crop={w}:{h}:{x}:{y}"
                ])
        
        elif WINDOWS:
            # defaults to fullscreen, idk how multihead works
            self.cmd.extend([
                "-f", "gdigrab",
                "-show_region", "1" if show_region else "0",
                "-framerate", str(fps)
            ])

            if x is not None:
                self.cmd.extend([
                    "-offset_x", str(x),
                    "-offset_y", str(y)
                ])

            if w is not None:
                self.cmd.extend([
                    "-video_size", f"{w}x{h}"
                ])

            self.cmd.extend(["-i", "desktop"])

        self.cmd.extend([
            "-pix_fmt", "gray",
            #"-vf", "mpdecimate=hi=32",
            #"-vcodec", "rawvideo",
            #"-vf", "hqdn3d=16:0",
            "-f", "yuv4mpegpipe",
            "-",
        ])
        # fmt: on
        info(" ".join(self.cmd))
        self.p = sp.Popen(self.cmd, stdout=sp.PIPE)

    def run(self):
        re_size = re.compile("^YUV4MPEG2 W([0-9]+) H([0-9]+)")
        lastframe = None
        nframes = 0
        while True:
            yuv = b""
            fails = 0
            while True:
                tbuf = self.p.stdout.read(1)
                if not tbuf:
                    if self.p.poll() is not None:
                        debug("ffmpeg: exited")
                        return
                    else:
                        fails += 1
                        if fails < 30:
                            time.sleep(0.1)
                            continue

                        raise Exception("read err 1")

                yuv += tbuf
                if len(yuv) > 1024:
                    raise Exception(yuv)

                if yuv.endswith(b"\n"):
                    break

            if yuv != b"FRAME\n":
                meta = yuv.decode("utf-8", "ignore")
                m = re_size.match(meta)
                if not m:
                    raise Exception(meta)

                self.w, self.h = [int(x) for x in m.groups()]
                continue

            if self.w is None:
                warn("width/height unknown; looking for more y4m headers")
                continue

            rem = self.w * self.h
            yuv = self.p.stdout.read(rem)
            rem -= len(yuv)
            fails = 0
            while rem > 0:
                ayuv = self.p.stdout.read(rem)
                if ayuv:
                    yuv += ayuv
                    continue

                if self.p.poll() is not None:
                    debug("ffmpeg: exited")
                    return
                else:
                    fails += 1
                    if fails < 30:
                        time.sleep(0.1)
                        continue

                    raise Exception("read err 2")

            if yuv == lastframe:
                continue

            lastframe = yuv
            # with open('/dev/shm/vxbuf', 'wb') as f:
            #    f.write(yuv)

            lumas = ", ".join(str(int(x)) for x in yuv[-8:])
            debug(f"ffmpeg: got bitmap {nframes}, last 8 pixels: {lumas}")
            nframes += 1
            with self.yuv_mutex:
                self.yuv = yuv[:]

    def take_yuv(self):
        with self.yuv_mutex:
            ret = self.yuv
            self.yuv = None
            return ret

    def terminate(self):
        self.p.terminate()
        self.p.wait()
        # self.p.kill()


class VxDecoder(object):
    def __init__(self, sw, sh):
        self.sw = sw
        self.sh = sh

        self.ledge = 128  # min diff between dark and light (0 and 1)
        self.noise = 64  # max diff before considered different color

        #      bg png: ledge 176, noise 0
        #  bg jpg q40: ledge 148, noise 56
        #     box png: ledge 221, noise 0
        # box jpg q30: ledge 186, noise 56
        # box jpg q20: NG

    def find_rise(self, n0, buf, allow_noise=False):
        return self.find_ledges(True, False, n0, buf, allow_noise)

    def find_fall(self, n0, buf, allow_noise=False):
        return self.find_ledges(False, True, n0, buf, allow_noise)

    def find_ledges(self, add_raise, add_fall, n0, buf, allow_noise=False):
        """
        finds and returns the first ledge,
        upon noise will abort and return None,
        returns None if nothing found
        """
        n = n0 + 1
        iterbuf = iter(buf)
        pvf = next(iterbuf)
        for vf in iterbuf:
            if vf - pvf > self.ledge:
                pvf = vf
                if add_raise:
                    return n

            elif pvf - vf > self.ledge:
                pvf = vf
                if add_fall:
                    return n

            elif abs(pvf - vf) > self.noise and not allow_noise:
                break

            n += 1
            # pvf = vf
            #   uncomment this to compare against previous value
            #   (probably gets buggy on blurred edges)

        return None

    def find_halfblock(self, yuv, fbh, y, xt):
        y = int(y + fbh * 1.5)
        debug(f"halfblock search at ({xt},{y})")

        row_ptr = y * self.sw + xt
        yt1 = self.find_rise(y, yuv[row_ptr :: self.sw], True)
        if yt1 is None or yt1 - y > fbh * 2:
            return None

        # shift down to result, look for bottom edge of halfblock
        row_ptr = yt1 * self.sw + xt
        yt2 = self.find_fall(yt1, yuv[row_ptr :: self.sw], True)
        if yt2 is None or yt2 - y > fbh * 2:
            return None

        ytd = yt2 - yt1
        # offset from cell-top to middle of halfblock
        ret = (yt1 - y) / fbh
        ret -= int(ret)
        ret = ret * fbh + ytd / 2.0
        ret = round(ret * 1000) / 1000.0
        info(f"halfblock at x{xt} between y{yt1} and y{yt2} = offset {ret}")
        # lower-halfblock between 92 and 102, offset 15
        # upper-halfblock between 83 and 92, offset 5

        return ret

    def gen_cali_pattern(self, w, h):
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
        return scr

    def find_cali(self, yuv):
        """
        takes an 8-byte grayscale bitmap,
        finds the biggest vx calibration screen
        
        LLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLL
        L█████████████████████████████████████████████████████████L
        L█LLL█LLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLL█LLL█L
        L█L▄L█LLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLL█L▀L█L
        L█LLL█LLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLL█LLL█L
        L█████LLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLL█████L
        L█LLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLL█L
        L█LLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLL█L
        L█LLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLL█L
        L█████████████████████████████████████████████████████████L
        LLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLL
        """

        # vq vw ve   prev row
        # va vs vd   curr row
        # vz vx vc   next row
        # || ||  `-- next
        # ||  `----- curr
        #  `-------- prev

        info("searching for calibration pattern (start vxt now)")

        for y in range(1, self.sh - 1):

            prow = self.sw * (y - 1)
            crow = self.sw * y
            nrow = self.sw * (y + 1)

            # look for top left pixel of top left █;
            #   sdx must be equal,
            #   qweaz must be equal,
            #   a must be darker than s

            # first collect a list of pixels which are
            # brighter than the previous pixel on the row
            x = 0
            pvs = 256
            rising = []
            for vs in yuv[crow : crow + self.sw - 12]:  # minwidth 12px
                if vs - pvs > self.ledge:
                    rising.append(x)

                pvs = vs
                x += 1

            # compare these against the remaining constraints
            for x in rising:

                # comparisons for current row
                va, vs, vd = yuv[crow + x - 1 : crow + x + 2]

                if vs - va < self.ledge or abs(vs - vd) > self.noise:
                    continue

                # comparisons for previous row
                vq, vw, ve = yuv[prow + x - 1 : prow + x + 2]

                if (
                    abs(va - vq) > self.noise
                    or abs(va - vw) > self.noise
                    or abs(va - ve) > self.noise
                ):
                    continue

                # comparisons of current/next row
                vz, vx = yuv[nrow + x - 1 : nrow + x + 1]

                if abs(va - vz) > self.noise or abs(vs - vx) > self.noise:
                    continue

                if False and y >= 43 and y <= 47:
                    m_ledge = vs - va
                    m_noise = max(
                        abs(va - vq),
                        abs(va - vw),
                        abs(va - ve),
                        abs(va - vz),
                        abs(vs - vd),
                        abs(vs - vx),
                    )

                    debug(f"topleft: {x}, {y}, ledge {m_ledge}, noise {m_noise}")

                x2 = self.find_fall(x, yuv[crow + x : crow + self.sw])
                if x2 is None or x2 < x + 12:
                    continue

                debug(f"row {y}, len {x2-x}, ({x} to {x2})")
                # r 45, x 11 to 1901

                # iterate downwards from the centre of the bar,
                # stop at the first fall (bottom of top fence)
                xc = int(x + (x2 - x) / 2)
                yt = self.find_fall(y, yuv[crow + xc :: self.sw])
                if yt is None:
                    continue

                # approx height of a fullblock GET
                fbah = yt - y
                if fbah > 64:
                    continue

                # offset from top edge to middle of block
                oy = 0
                if fbah > 2:
                    oy = int(fbah / 2.0)

                debug(f"row {y}, len {x2-x}, ({x} to {x2}), fbah {fbah}")

                # find width of a fullblock (fairly accurate) by going left
                # from the second row of blocks, stopping at first rise:
                #  █████████████████████████████████████████████████████████
                #  █  ><--here                                         █   █
                #  █ ▄ █                                               █ ▀ █

                # second row of blocks
                trow = crow + (fbah + oy) * self.sw

                # will go past 4 fullblocks horizontally;
                # assume block width less than 3x height
                # (accounting for crazy sjis memes if any)
                max_iter = min(trow + self.sw - 12, trow + x + fbah * 3 * 4)

                xt = self.find_rise(x, yuv[trow + x : max_iter])
                if xt is None:
                    continue

                # width of a fullblock GET
                fbw = (xt - x) / 4.0

                # offset from left edge to center of block
                ox = 0
                if fbw > 2:
                    ox = int(fbw / 2.0)

                debug(f"row {y}, len {x2-x}, ({x} to {x2}), fbah {fbah}, fbw {fbw:.2f}")
                # row 45, len 1890, (11 to 1901), fbah 19, fbw 10.00

                # get more accurate fullblock height by
                # going down along the 2nd cell into the first rise
                #  █████████████████████████████████████████████████████████
                #  █   █                                               █   █
                #  █ ▄ █                                               █ ▀ █
                #  █v  █                                               █   █
                #  █^-here █ █ █ █ █ █ █ █ █ █ █ █ █ █ █ █ █ █ █ █ █ █ █████

                xt = int(x + ox + fbw)
                yt = self.find_rise(y, yuv[crow + xt :: self.sw])
                if yt is None:
                    continue

                # height of a fullblock GET
                fbh = (yt - y) / 4.0

                # update offset from top edge to middle of block
                oy = 0
                if fbh > 2:
                    oy = int(fbh / 2.0)

                debug(
                    f"row {y}, len {x2-x}, ({x} to {x2}), fbh {fbh:.2f}, fbw {fbw:.2f}"
                )
                # row 45, len 1890, (11 to 1901), fbh 19, fbw 10.00

                # find height of test pattern
                y2 = self.find_fall(y, yuv[crow + x + ox :: self.sw])
                if y2 is None or y2 < 8:
                    continue

                nwf = (x2 - x) / fbw
                nhf = (y2 - y) / fbh
                nw = round(nwf)
                nh = round(nhf)
                if abs(nw - nwf) >= 0.1 or abs(nh - nhf) >= 0.1:
                    continue

                debug(
                    f"row {y}, len {x2-x}, ({x} to {x2}), fbh {fbh:.2f}, fbw {fbw:.2f}, nw {nw}, nh {nh}"
                )
                # row 45, len 1890, (11 to 1901), fbh 19, fbw 10.00, nw 189, nh 51

                if nw < 12 or nh < 8:
                    continue

                # x = fence x
                # y = fence y
                # nw = fence width in blocks
                # nh = fence height in blocks
                # fbw = fullblock width
                # fbh = fullblock height

                xl = int(x + ox + fbw * 2)
                xu = int(x + ox + fbw * (nw - 3))
                debug(f"halfblock search at {xl} and {xu}")
                ohl = self.find_halfblock(yuv, fbh, y, xl)
                ohu = self.find_halfblock(yuv, fbh, y, xu)

                # slice buffer to just the matrix area
                sx1 = int(x - fbw)
                sy1 = int(y - fbh)
                sx2 = int(x2 + fbw)
                sy2 = int(y2 + fbh)
                debug(f"matrix bounds ({sx1},{sy1}) to ({sx2},{sy2})")
                syuv = []
                for y in range(sy1, sy2):
                    syuv.append(yuv[y * self.sw + sx1 : y * self.sw + sx2])

                # get reference brightness:
                #   cell 3,1 is a safe bright (top fence, center of left alignment subfence)
                #   cell 3,7 is a safe dark (2nd block below center of left alignment subfence)
                lx = int(3 * fbw + ox)
                hx = int(3 * fbw + ox)
                ly = int(7 * fbh + oy)
                hy = int(1 * fbh + oy)
                debug(f"thresh from ({lx},{ly}), ({hx},{hy})")
                hi = syuv[hy][hx]
                lo = syuv[ly][lx]
                thresh = lo + (hi - lo) / 2
                debug(f"thresh from ({lx},{ly}), ({hx},{hy}) = {lo}-{hi} = {thresh}")
                if hi - lo < self.ledge:
                    warn(
                        "insufficient contrast (if that was even a calibration pattern)"
                    )
                    continue

                matrix = VxMatrix(thresh, fbw, fbh, nw + 2, nh + 2, ohl, ohu)
                matrix.set_yuv(syuv)

                cali = self.gen_cali_pattern(nw + 2, nh + 2)
                cy = 0
                valid = True
                for ln in cali:
                    cx = 0
                    for ch in ln:
                        expect = None
                        if ch == "█":
                            expect = True
                        elif ch == " ":
                            expect = False

                        sv, sx, sy, sl = matrix.get(cx, cy)
                        if expect is not None and expect != sv:
                            warn(
                                f"bad value at ({cx},{cy}), ({sx},{sy})={sl}={sv} != {expect}"
                            )
                            valid = False
                            break

                        cx += 1

                    cy += 1
                    if not valid:
                        break

                if not valid:
                    warn("calibration pattern incorrect")
                    continue

                info(f"found at {sx1}x{sy1}, size {sx2-sx1}x{sy2-sy1} ({nw+2}x{nh+2})")
                matrix.halfs = ohl is not None
                return matrix, sx1, sy1

        return None, None, None


class VxMatrix(object):
    def __init__(self, thresh, fbw, fbh, nw, nh, ohl, ohu):
        self.yuv = None  # 2D greyscale matrix (list of lists)
        self.thresh = thresh  # lo/hi threshold
        self.bw = fbw  # block width in pixels (float)
        self.bh = fbh  # block height in pixels (float)
        self.nw = nw  # num modules x
        self.nh = nh  # num modules y
        self.ohl = ohl  # vertical offset into centre of lower halfblock
        self.ohu = ohu  # vertical offset into centre of upper halfblock
        self.ox = fbw / 2 if fbw > 2 else 0  # horizontal offset into fullblock centre
        self.oy = fbh / 2 if fbh > 2 else 0  # vertical offset into fullblock centre
        self.halfs = False  # whether halfblocks are in use (double vertical resolution)

    def set_yuv(self, yuv):
        self.yuv = yuv

    def get(self, nx, ny):
        x = self.ox + self.bw * nx
        if not self.halfs or self.ohl is None or self.ohu is None:
            y = self.oy + self.bh * ny
        else:
            yfull, yhalf = divmod(ny, 2)
            y = yfull * self.bh
            y += self.ohu if yhalf else self.ohl

        luma = self.yuv[int(y)][int(x)]
        return self.thresh < luma, x, y, luma

    def width(self, ny):
        max_ny = self.nh - 1
        if self.halfs:
            ny = int(ny / 2)

        return self.nw if ny < max_ny else self.nw - 1

    def height(self):
        return self.nh * 2 if self.halfs else self.nh


class Assembler(object):
    def __init__(self):
        self.buf = b""
        self.remainder = ""
        self.next_frame = 0
        self.frametab = {}
        self.fig_no = 0
        self.files_total = None
        self.bytes_total = None
        self.files_done = 0
        self.bytes_done = 0
        self.payload = None

    def put(self, nframe, data):
        # "data" is the remaining part of the vx frame,
        # having stripped the checksum and frame-number

        if nframe < self.next_frame:
            return False

        # save ram by storing the bitstring as bytes with incorrect offset,
        # (convert back to bits and realign when enough frames are present)
        bstr = bits2bstr(data)
        remains = data[len(bstr) * 8 :]
        self.frametab[nframe] = [bstr, remains]

        # then decode what we can
        while True:
            if self.bytes_total and self.bytes_done >= self.bytes_total:
                return True

            if not self._process():
                return False

    def _realign(self):
        """
        consumes ready frames from frametab into the ordered buffer;
        returns [True,bits] if success, [False,missingno] otherwise
        """
        while self.frametab:
            if self.next_frame not in self.frametab:
                return False, self.next_frame

            frame_data, frame_remainder = self.frametab[self.next_frame]
            bits = bstr2bitstr(frame_data)
            bits = self.remainder + bits + frame_remainder
            bstr = bits2bstr(bits)
            self.buf += bstr
            self.remainder = bits[len(bstr) * 8 :]

            del self.frametab[self.next_frame]
            self.next_frame += 1

        return True, 0

    def _process(self):
        """
        processes all pending frames in the frametab;
        returns True if all done, False if more data needed
        """
        self._realign()

        if self.fig_no == 0:
            # figment type 0: stream header
            #  - vle "1" (vx version)
            #  - vle num_files
            #  - vle num_payload_bytes

            it = iter(self.buf)
            pos = 0

            vx_ver, ate = dec_vle(it)
            pos += ate
            if vx_ver != 1:
                raise Exception(f"this vxr supports version 1 only (got {vx_ver})")

            self.files_total, ate = dec_vle(it)
            pos += ate
            self.bytes_total, ate = dec_vle(it)
            pos += ate

            self.buf = self.buf[pos:]
            self.fig_no += 1
            return True

        if not self.files_total:
            raise Exception("awawa")

        if self.fig_no % 2 == 1:
            # figment type 1: file header
            #  - vle figment length
            #  - vle file size
            #  - vle unixtime last-modified
            #  - sha512[:16]
            #  - vle filepath length
            #  - filepath

            pos = 0
            it = iter(self.buf)

            # fig_len, ate = dec_vle(it)
            # pos += ate
            #
            # if fig_len > len(self.buf) - pos:
            #    return False

            sz, ate = dec_vle(it)
            pos += ate
            if ate == 0:
                return False

            ts, ate = dec_vle(it)
            pos += ate
            if ate == 0:
                return False

            try:
                cksum = bytes(next(it) for _ in range(16))
                pos += 16
            except RuntimeError:
                return False

            fn_len, ate = dec_vle(it)
            pos += ate
            if ate == 0:
                return False

            try:
                fn = bytes(next(it) for _ in range(fn_len))
                pos += fn_len
            except RuntimeError:
                return False

            # TODO output directory config
            fn = b"inc/" + fn
            os.makedirs(fn.rsplit(b"/", 1)[0], exist_ok=True)

            self.buf = self.buf[pos:]
            self.payload = {
                "fn": fn,
                "sz": sz,
                "ts": ts,
                "cksum": cksum,
                "fo": open(fn, "wb"),
            }

            hfn = fn.decode("utf-8", "ignore")
            hsum = binascii.hexlify(cksum).decode("utf-8")
            info("")
            info(f"receiving file: {sz} bytes, {ts} lastmod, {hsum},\n  {hfn}")

            self.fig_no += 1
            return True

        # figment type 2: payload
        while self.buf:
            remains = self.payload["sz"] - self.payload["fo"].tell()
            if remains <= 0:
                break

            written = self.payload["fo"].write(self.buf[:remains])
            if written == 0:
                raise Exception("uhh")

            self.buf = self.buf[written:]
            self.bytes_done += written

        if self.payload["fo"].tell() >= self.payload["sz"]:
            self.payload["fo"].close()

            fn = self.payload["fn"]
            ts = self.payload["ts"]
            cksum = self.payload["cksum"]

            hasher = hashlib.sha512()
            with open(fn, "rb", 512 * 1024) as f:
                while True:
                    buf = f.read(512 * 1024)
                    if not buf:
                        break

                    hasher.update(buf)

            cksum2 = hasher.digest()[:16]
            if cksum != cksum2:
                h1 = binascii.hexlify(cksum).decode("utf-8")
                h2 = binascii.hexlify(cksum2).decode("utf-8")
                raise Exception(f"file corrupted: expected {h1}, got {h2}")

            info("file verification OK\n")
            os.utime(self.payload["fn"], (ts, ts))

            self.files_done += 1
            self.fig_no += 1
            return True

        return False


def switch_frame(next_frame, automatic, hit_enter):
    if not automatic:
        warn(f"please switch to frame {next_frame}")
    else:
        kbd = KbdController()
        kbd.press("d")
        kbd.release("d")
        if hit_enter:
            kbd.press(KbdKey.enter)
            kbd.release(KbdKey.enter)


def main():
    logging.basicConfig(
        level=logging.INFO,  # INFO DEBUG
        format="\033[36m%(asctime)s.%(msecs)03d\033[0m %(message)s",
        datefmt="%H%M%S",
    )
    lh = logging.StreamHandler(sys.stderr)
    lh.setFormatter(LoggerFmt())
    logging.root.handlers = [lh]

    if WINDOWS:
        os.system("cls")

    devs = []
    dev = None
    need_dev = False
    if MACOS:
        debug("collecting video device list")
        devs = get_avfoundation_devs()
        debug(f"found {len(devs)} devices")
        # devs.append([4, "asdf fdsa"])

    ap = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        prog="vxr",
        description="receive files by recording a dotmatrix pattern",
    )

    if len(devs) == 1:
        dev = devs[0][0]
    elif len(devs) > 1:
        need_dev = True

    if MACOS:
        ap.add_argument("-i", metavar="SCREEN", help="video device (ID or name)")

    if HAVE_PYNPUT:
        ap.add_argument("-nk", action="store_true", help="disable keyboard simulation")
        ap.add_argument(
            "-ret",
            action="store_true",
            help="press enter after each keystroke (support getchar-less vxt's)",
        )

    ar = ap.parse_args()

    if need_dev and ar.i is not None:
        dev = next((x for x in devs if x[0] == ar.i), None)
        if not dev:
            next((x for x in devs if ar.i in x[1]), None)
        if not dev:
            error("none of the available screens match the one you specified ;_;")
        if dev:
            debug(f"using screen #{dev[0]} = {dev[1]}")
            dev = dev[0]

    if need_dev and dev is None:
        error("found multiple screens; you must choose one to record from.")
        error('use "-i <ID/Name>" to choose one of these:')
        for di, dt in devs:
            info(f"  {di}: {dt}")

        sys.exit(1)
        return

    if HAVE_PYNPUT:
        use_pynput = not ar.nk
    else:
        warn("could not import pynput; disabling keyboard simulation")
        use_pynput = False
        ar.ret = False

    show_region = not WINDOWS or not use_pynput
    ffmpeg = FFmpeg(cali_fps, dev=dev, show_region=show_region)
    ffmpeg.start()

    matrix = None
    pyuv = None
    while not matrix:
        while True:
            time.sleep(cali_wait)
            yuv = ffmpeg.take_yuv()
            if yuv:
                break

        if pyuv == yuv:
            continue

        pyuv = yuv
        t0 = time.time()
        vxdec = VxDecoder(ffmpeg.w, ffmpeg.h)
        matrix, sx, sy = vxdec.find_cali(yuv)
        t = time.time()
        debug("spent {:.2f} sec".format(t - t0))

    ffmpeg.terminate()

    sw = len(matrix.yuv[0])
    sh = len(matrix.yuv)

    ffmpeg = FFmpeg(dec_fps, sw, sh, sx, sy, dev, show_region)
    ffmpeg.start()

    switch_frame(1, use_pynput, ar.ret)

    t0_xfer = time.time()
    asm = Assembler()
    pyuv = None
    while True:
        while True:
            time.sleep(dec_wait)
            yuv = ffmpeg.take_yuv()
            if yuv:
                break

        if pyuv == yuv:
            continue

        if False:
            with open("dump.raw", "wb") as f:
                f.write(yuv)
                return

        t0_frame = time.time()
        pyuv = yuv
        syuv = [yuv[sw * n : sw * (n + 1)] for n in range(sh)]
        matrix.set_yuv(syuv)
        bits = ""
        cx = 0
        cy = 0
        dbuf = b""
        vis = []
        while True:
            sv, sx, sy, sl = matrix.get(cx, cy)
            bits += "1" if sv else "0"
            # if len(bits) >= 8 and len(bits) % 8 == 0:
            #    vis.append("".join(["@" if x == "1" else " " for x in bits[-8:]]))

            cx += 1
            if cx >= matrix.width(cy):
                cx = 0
                cy += 1
                if vis:
                    vis.append("/")
                if cy >= matrix.height():
                    break

        ofs = 4 * 8
        cksum = bits2bstr(bits[:ofs])
        cksum = binascii.hexlify(cksum).decode("utf-8")
        frameno, ofs2 = dec_vle(bits2bstr(bits[ofs : ofs + 8 * 8]))

        cksum2 = hashlib.sha512(bits[ofs:].encode("utf-8")).digest()[:4]
        cksum2 = binascii.hexlify(cksum2).decode("utf-8")
        bits = bits[ofs + ofs2 * 8 :]
        if cksum != cksum2:
            warn(f"bad checksum; need {cksum}, got {cksum2}, maybe frame {frameno}")
            continue

        if frameno == asm.next_frame:
            debug(f"got frame {frameno}, thx")
        else:
            need = asm.next_frame
            info(f"got frame {frameno}, need {need}, please go {need-frameno:+d}")
            continue

        switch_frame(asm.next_frame + 1, use_pynput, ar.ret)

        now = time.time()
        td_xfer = now - t0_xfer
        td_frame = now - t0_frame
        if asm.bytes_total:
            kbps = (asm.bytes_done / td_xfer) / 1024.0
        else:
            kbps = 0

        if True:
            if asm.put(frameno, bits):
                info(f"transfer completed in {td_xfer:.2f} sec ({kbps:.3f} kBps)\n")
                return
        else:
            bits = bits[frameno % 8 :]
            dbuf = bits2bstr(bits)
            debug(
                f"got {len(dbuf)} bits in {td_frame:.3f} seconds, halfs: {matrix.halfs}"
            )
            debug(dbuf.decode("latin1", "replace"))
            # debug(" ".join(str(int(x)) for x in dbuf))
            if vis:
                print("".join(vis))

        if asm.bytes_total is not None:
            perc_bytes = 100.0 * asm.bytes_done / asm.bytes_total
            perc_files = 100.0 * asm.files_done / asm.files_total
            info(
                f"frame {frameno}, "
                + f"{asm.files_done}/{asm.files_total} files ({perc_files:.2f}%), "
                + f"{asm.bytes_done/1024:.0f} of {asm.bytes_total/1024:.0f} kB ({perc_bytes:.2f}%), "
                + f"{td_xfer:.2f} sec, {kbps:.3f} kB/s"
            )


def prof():
    import cProfile
    import pstats
    from pstats import SortKey

    cProfile.run("main()", "profiler.stats")
    print("\n\n")
    p = pstats.Stats("profiler.stats")
    p.strip_dirs().sort_stats(SortKey.CUMULATIVE).print_stats()


if __name__ == "__main__":
    main()
    # prof()


r"""
# initial mpdecimate test; it fails on bottom right pixel if not 8x8 aligned
ffmpeg -v warning -hide_banner -nostdin -f x11grab -r 10 -s 1902x553 -i :0.0+0,0 -pix_fmt gray -vf mpdecimate -f yuv4mpegpipe - | ffplay -f yuv4mpegpipe -
while true; do for c in 0 5 7; do printf '\033[H\033[J\033[30;900H\033[4%sm \033[0m' $c; sleep 1; done; done

# moveable color block; use wsad and 0-7
x=1; y=1; c=7; while true; do printf '\033[H\033[J\033[%s;%sH\033[4%sm \033[0m\033[999;999H' $y $x $c; IFS= read -u1 -n1 -r ch; [ "$ch" = w ] && y=$((y-1)); [ "$ch" = s ] && y=$((y+1)); [ "$ch" = a ] && x=$((x-1)); [ "$ch" = d ] && x=$((x+1)); [ "$ch" = W ] && y=$((y-6)); [ "$ch" = S ] && y=$((y+6)); [ "$ch" = A ] && x=$((x-16)); [ "$ch" = D ] && x=$((x+16)); printf '%s\n' "$ch" | grep -E '^[0-7]$' && c=$ch; done
ffmpeg -v warning -hide_banner -nostdin -f x11grab -r 10 -s 1904x544 -i :0.0+0,0 -pix_fmt gray -vf mpdecimate -f yuv4mpegpipe - | ffplay -f yuv4mpegpipe -
ffmpeg -v warning -hide_banner -nostdin -f x11grab -r 30 -s 1880x544 -i :0.0+0,0 -pix_fmt gray -vf mpdecimate -f yuv4mpegpipe - | ffplay -f yuv4mpegpipe -vf crop=64:48:iw-64:ih-48 -

# test python frame retrieval performance
ttf="$(fc-match monospace -f '%{file}\n')"; ffplay -f lavfi -i testsrc2=r=30:s=1792x1008 -vf "drawtext=fontfile=$ttf: text='%{gmtime}  %{pts\:hms}  %{n}': x=7: y=43: fontcolor=white: fontsize=41: box=1: boxcolor=black@0.3: boxborderw=5"
ffmpeg -nostdin -hide_banner -v warning -color_range jpeg -flush_packets 1 -fflags +nobuffer -flags +low_delay -f x11grab -show_region 1 -framerate 30 -s 1920x1080 -i :0.0+0,0 -pix_fmt gray -f yuv4mpegpipe - | pv -qS 4M | ffplay -vf yuv4mpeg -
# 59.3 MiB/s uncapped

# actually nevermind here's the test
ffplay -f lavfi -i testsrc2=r=60

"""
