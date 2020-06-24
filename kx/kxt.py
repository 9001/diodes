#!/usr/bin/env python3

__author__ = "ed <diodes@ocv.me>"
__copyright__ = 2020
__license__ = "MIT"
__url__ = "https://github.com/9001/diodes/"

"""
kxt.py: filetransfer into vm/vnc/rdp sessions through keyboard simulation.
one of the following approaches can be used:

1) just type the contents of the file as-is (better be plaintext)

2) the entire file(s) are compressed and base64-encoded,
   typed into the vm and checksum-verified, then unpacked automatically

3) a TCP socket is opened on the host, and kxt initiates the download
   on the guest, using either /dev/tcp or netcat/curl/wget/...
   (this one is TODO)

dependencies on host:
  if host is windows: none
  if host is mac-osx: none
  if host is linux:   xdotool (recommended) or pynput

dependencies in guest:
  none

supported host OS'es:
  windows (XP and newer)
  mac-osx (any version probably)
  linux (any version probably)

supported guest OS'es for plaintext data:
  any

supported guest OS'es for binary data:
  linux
  mac-osx
  windows, vista and later (with no dependencies)
  windows, any version (with msys2/mingw/cygwin/similar)

supported python versions:
  cpython 3.6 and later
  pypy3 7.1.0 and later

TODO:
  in-memory zip creation
  network transport
"""


import re
import os
import sys
import stat
import time
import zlib
import base64
import struct
import signal
import tarfile
import hashlib
import logging
import argparse
import threading
import subprocess as sp
from datetime import datetime
from queue import Queue

try:
    from pynput.keyboard import Key as KbdKey
    from pynput.keyboard import Controller as KbdController
    from pynput._info import __version__ as pynput_version

    HAVE_PYNPUT = ".".join(str(x) for x in pynput_version)
except ImportError:
    HAVE_PYNPUT = None

try:
    WINDOWS = True
    from ctypes import windll, wintypes
    import ctypes
except:
    WINDOWS = False


LINUX = sys.platform.startswith("linux")
MACOS = sys.platform in ["Mac", "darwin", "os2", "os2emx"]
FS_ENC = sys.getfilesystemencoding()


def getver(cmd, ptn):
    try:
        p = sp.Popen(cmd.split(" "), stdout=sp.PIPE)
        m = re.match(ptn, p.communicate()[0].decode("utf-8"))
        return m.group(1)
    except:
        return None


if LINUX:
    HAVE_XDOTOOL = getver("xdotool -v", r"^xdotool version ([0-9\._-]+)")
    HAVE_XPROP = getver(
        r"xprop -root 32x \t$0 _NET_ACTIVE_WINDOW", r".*\t(0x[0-9a-f]+)"
    )

    HAVE_KEYBOARD_SIM = HAVE_XDOTOOL or HAVE_PYNPUT
    HAVE_FOCUS_DETECT = HAVE_XDOTOOL or HAVE_XPROP
elif WINDOWS or MACOS:
    HAVE_KEYBOARD_SIM = True
    HAVE_FOCUS_DETECT = True
else:
    raise Exception("unsupported python or host-os")


# HAVE_PYNPUT = False
# HAVE_XDOTOOL = False
# HAVE_XPROP = False


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

        return f"\033[0;36m{ts}{ansi} {str(record.msg)}\033[0m"


class QFile(object):
    """file-like object which buffers writes into a queue"""

    def __init__(self):
        self.q = Queue(64)

    def write(self, buf):
        self.q.put(buf)


class StreamLog(object):
    """logs a stream to file"""

    def __init__(self, src_gen, tag):
        self.src_gen = src_gen
        self.ci = 0
        self.co = 0
        self.tag = tag
        self.logfile = open(f"kxt.debug.{tag}", "wb")

    def collect(self):
        for buf in self.src_gen:
            if buf is None:
                break

            self.ci += len(buf)
            self.co += len(buf)
            self.logfile.write(buf)
            yield buf

        debug(f"eof log {self.tag} c*({self.ci})")
        self.logfile.close()
        yield None


class StreamTar(object):
    """construct in-memory tar file from the given path"""

    def __init__(self):
        self.ci = 0
        self.co = 0
        self.qfile = QFile()
        self.srcdirs = Queue()
        self.srcfiles = Queue()

        # python 3.8 changed to PAX_FORMAT as default,
        # waste of space and don't care about the new features
        fmt = tarfile.GNU_FORMAT
        self.tar = tarfile.open(fileobj=self.qfile, mode="w|", format=fmt)

        w = threading.Thread(target=self._gen)
        w.start()

    def collect(self):
        while True:
            buf = self.qfile.q.get()
            if buf is None:
                break

            self.co += len(buf)
            yield buf

        debug(f"eof tarc co({self.co})")
        yield None

    def _put(self, root, path):
        arcname = path[len(root) :].decode(FS_ENC, "replace")
        while arcname.startswith("../"):
            arcname = arcname[3:]

        inf = tarfile.TarInfo(name=arcname)

        stat = os.stat(path)
        inf.mode = stat.st_mode
        inf.size = stat.st_size
        inf.mtime = stat.st_mtime
        inf.uid = 0
        inf.gid = 0

        utf = path.decode(FS_ENC, "replace")
        if os.path.isdir(path):
            inf.type = tarfile.DIRTYPE
            self.tar.addfile(inf)
        else:
            mode = f"{inf.mode:o}"[-3:]
            debug(f"m({mode}) ts({inf.mtime:.3f}) sz({inf.size}) {utf}$")
            self.ci += inf.size
            with open(path, "rb") as f:
                self.tar.addfile(inf, f)

    def _gen(self):
        while True:
            srcdir = self.srcdirs.get()
            if not srcdir:
                break

            for root, dirs, files in os.walk(srcdir):
                dirs.sort()
                files.sort()
                for name in dirs + files:
                    path = os.path.join(root, name)
                    self._put(srcdir, path)

        while True:
            srcfile = self.srcfiles.get()
            if not srcfile:
                break

            self._put(b"", srcfile.replace(b"\\", b"/"))

        self.tar.close()
        self.qfile.q.put(None)
        debug(f"eof targ ci({self.ci})")

    def add_dir(self, dirpath):
        self.srcdirs.put(dirpath)

    def add_file(self, filepath):
        self.srcfiles.put(filepath)

    def end_input(self):
        self.srcdirs.put(None)
        self.srcfiles.put(None)


class StreamFile(object):
    """yield chunks from a file"""

    def __init__(self, fn):
        self.fn = fn
        self.ci = 0
        self.co = 0

    def collect(self):
        with open(self.fn, "rb", 512 * 1024) as f:
            while True:
                buf = f.read(128)
                if not buf:
                    break

                self.ci += len(buf)
                self.co += len(buf)
                yield buf

        debug(f"eof file c*({self.co})")
        yield None


class StreamHash(object):
    """
    md5-checksum generator middleman
    (md5 is good enough; no risk of malicious modifications)
    """

    def __init__(self, src_gen):
        self.src_gen = src_gen
        self.ci = 0
        self.co = 0
        self.hasher = hashlib.md5()

    def collect(self):
        for buf in self.src_gen:
            if buf is None:
                break

            self.hasher.update(buf)
            self.ci += len(buf)
            self.co += len(buf)
            yield buf

        debug(f"eof hash c*({self.co})")
        yield None

    def get_hash(self):
        return self.hasher.hexdigest()


class StreamGzip(object):
    """yield stream as gzip"""

    def __init__(self, src_gen):
        self.src_gen = src_gen
        self.ci = 0
        self.co = 0

    def collect(self):
        # https://stackoverflow.com/questions/44185486/generate-and-stream-compressed-file-with-flask
        # https://stackoverflow.com/a/44387566

        header = (
            b"\x1F\x8B\x08\x00"  # Gzip file, deflate, no filename
            + struct.pack("<L", int(time.time()))  # compression start time
            + b"\x02\xFF"  # maximum compression, no OS specified
        )
        self.co += len(header)
        yield header

        pk = zlib.compressobj(9, zlib.DEFLATED, -zlib.MAX_WBITS, zlib.DEF_MEM_LEVEL, 0)
        crc = zlib.crc32(b"")
        length = 0

        for buf in self.src_gen:
            if buf is None:
                break

            self.ci += len(buf)
            outbuf = pk.compress(buf)
            crc = zlib.crc32(buf, crc) & 0xFFFFFFFF
            length += len(buf)
            if outbuf:
                self.co += len(outbuf)
                yield outbuf

        buf = pk.flush() + struct.pack("<2L", crc, length & 0xFFFFFFFF)
        self.co += len(buf)
        yield buf

        debug(f"eof gzip ci({self.ci})")
        debug(f"eof gzip co({self.co})")
        yield None


class StreamBase64(object):
    """yield stream as base64"""

    def __init__(self, src_gen):
        self.src_gen = src_gen
        self.buf = b""
        self.ci = 0
        self.co = 0

    def collect(self):
        eof = False
        for buf in self.src_gen:
            if buf is None:
                eof = True
                ofs = len(self.buf)
            else:
                self.buf += buf
                ofs, _ = divmod(len(self.buf), 3)
                if ofs == 0:
                    continue

                ofs *= 3

            buf = self.buf[:ofs]
            self.buf = self.buf[ofs:]

            self.ci += len(buf)
            buf = base64.b64encode(buf)
            self.co += len(buf)
            yield buf

            if eof:
                break

        debug(f"eof b-64 ci({self.ci})")
        debug(f"eof b-64 co({self.co})")
        yield None


class WindowFocusProviderOSX(threading.Thread):
    def __init__(self):
        threading.Thread.__init__(self)
        self.daemon = True
        self.focused = None
        self.lock = threading.Lock()

    def run(self):
        # delay 0.5 = 9% cpu load on mba-2017
        # (oneshot script every sec = 50% cpu)
        cmd = rb"""
set _delay to 0.15
set _last to 0
repeat
  tell application "System Events"
    set _app to first application process whose frontmost is true
    set _id to the unix id of _app
    set _title to the title of _app
    log {_id, _title}
  end tell
  if _last is not _id and _last is not 0 then set _delay to 0.5
  set _last to _id
  delay _delay
end repeat
"""
        t0 = time.time()
        ptn = re.compile("^([0-9]+), (.*)")
        p = sp.Popen(["osascript"], stdin=sp.PIPE, stdout=sp.DEVNULL, stderr=sp.PIPE)
        p.stdin.write(cmd)
        p.stdin.close()
        debug(f"wfp-osx up in {time.time() - t0 :.2f}")
        while True:
            try:
                ln = p.stderr.readline()
                m = ptn.match(ln.rstrip(b"\n").decode("utf-8"))
            except:
                m = None

            r = [-1, None]
            if m:
                a, b = m.groups()
                r = [int(a), b]

            debug(f"wfp-osx {r}")
            with self.lock:
                self.focused = r

            if r[0] < 0:
                error("applescript failed; " + ln.decode("utf-8", "replace"))
                return


class WindowFocusProvider:
    def __init__(self):
        self.busted = False
        self.subprovider = None
        if MACOS:
            self.subprovider = WindowFocusProviderOSX()
            self.subprovider.start()

    def _set_busted(self):
        warn("cannot determine active window")
        self.busted = True

    def get(self, include_title=True):
        if self.busted:
            return None

        if self.subprovider:
            for _ in range(20):
                with self.subprovider.lock:
                    v = self.subprovider.focused

                if v is None:
                    time.sleep(0.1)
                    continue

            if not v:
                self._set_busted()

            return v if include_title else v[0]

        if MACOS and False:
            # pyobjc init takes forever, just use osascript
            app = NSWorkspace.sharedWorkspace().activeApplication()
            if include_title:
                return app["NSApplicationProcessIdentifier"], app["NSApplicationName"]
            else:
                return app["NSApplicationProcessIdentifier"]

        if WINDOWS:
            hwnd = windll.user32.GetForegroundWindow()
            if not include_title:
                return hwnd

            bufsz = windll.user32.GetWindowTextLengthW(hwnd) + 1
            buf = ctypes.create_unicode_buffer(bufsz)
            windll.user32.GetWindowTextW(hwnd, buf, bufsz)
            return hwnd, buf.value

        if HAVE_XDOTOOL:
            hwnd = None
            try:
                p = sp.Popen(["xdotool", "getactivewindow"], stdout=sp.PIPE)
                hwnd = p.communicate()[0].decode("utf-8")
                hwnd = re.match(r"^([0-9]+)$", hwnd).group(1)
            except Exception as e:
                warn(f"xdotool getActive failed; {hwnd} // {repr(e)}")
                hwnd = None

            if hwnd is None or not include_title:
                return hwnd

            try:
                p = sp.Popen(["xdotool", "getwindowname", hwnd], stdout=sp.PIPE)
                title = p.communicate()[0].decode("utf-8", "replace").rstrip()
                return hwnd, title
            except Exception as e:
                warn(f"xdotool getTitle failed; {title} // {repr(e)}")
                return hwnd, ""

        stdout = None
        try:
            cmd = ["xprop", "-root", "32x", r"\t$0", "_NET_ACTIVE_WINDOW"]
            p = sp.Popen(cmd, stdout=sp.PIPE)
            stdout = p.communicate()[0].decode("utf-8")
            # _NET_ACTIVE_WINDOW(WINDOW) 0x3c00003
            hwnd = stdout.split("\t")[1]
            hwnd = re.match(r"^(0x[0-9a-f]+)$", hwnd).group(1)
        except Exception as e:
            warn(f"xprop getActive failed; {stdout}, {repr(e)}")
            hwnd = None

        if hwnd is None or not include_title:
            return hwnd

        if hwnd is not None:
            stdout = None
            try:
                cmd = ["xprop", "-id", hwnd, r"\t$0", "_NET_WM_NAME"]
                p = sp.Popen(cmd, stdout=sp.PIPE)
                stdout = p.communicate()[0].decode("utf-8", "replace")
                # _NET_WM_NAME(UTF8_STRING) "Terminal - fdsa"
                if not stdout.startswith("_NET_WM_NAME"):
                    raise Exception()

                title = stdout.split("\n")[0].split("\t", 1)[1][1:-1]
                return hwnd, title.rstrip()
            except:
                warn(f"xprop getTitle failed; {stdout}")
                return hwnd, ""


def assert_deps():
    debug(f"have pynput {HAVE_PYNPUT}")
    if not HAVE_PYNPUT:
        py_bin = sys.executable.split("/")[-1].split("\\")[-1]
        get_pynput = py_bin + " -m pip install --user pynput"

    if not LINUX:
        if HAVE_KEYBOARD_SIM and HAVE_FOCUS_DETECT:
            return

        error('need "pynput" for keyboard simulation')
        error(f"possible fix:  {get_pynput}")
        sys.exit(1)

    debug(f"have xdotool {HAVE_XDOTOOL}")
    debug(f"have xprop {HAVE_XPROP}")

    if HAVE_KEYBOARD_SIM and HAVE_FOCUS_DETECT:
        return

    if os.path.exists("/etc/apk"):
        get_pkg = "apk add"
    elif os.path.exists("/etc/apt"):
        get_pkg = "apt install"
    elif os.path.exists("/etc/yum.repos.d"):
        get_pkg = "yum install"
    else:
        get_pkg = "install"

    if not HAVE_FOCUS_DETECT:
        warn('need "xdotool" or "xprop" to determine active window')
        warn(f"  option 1: {get_pkg} xdotool")
        warn(f"  option 2: {get_pkg} xprop")
        print()

    if not HAVE_KEYBOARD_SIM:
        error('need "xdotool" or "pynput" for keyboard simulation')
        error(f"  option 1: {get_pkg} xdotool")
        error(f"  option 2: {get_pynput}")
        sys.exit(1)


if WINDOWS:
    # existing python keyboard libraries fail with virtualbox as target

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = (
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", wintypes.WPARAM),
        )

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = (
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", wintypes.WPARAM),
        )

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = (
            ("uMsg", wintypes.DWORD),
            ("wParamL", wintypes.WORD),
            ("wParamH", wintypes.WORD),
        )

    class _INPUTunion(ctypes.Union):
        _fields_ = (
            ("mi", MOUSEINPUT),
            ("ki", KEYBDINPUT),
            ("hi", HARDWAREINPUT),
        )

    class INPUT(ctypes.Structure):
        _fields_ = (("type", wintypes.DWORD), ("union", _INPUTunion))

    class Kbd(object):
        # https://stackoverflow.com/questions/13564851/how-to-generate-keyboard-events-in-python
        # https://stackoverflow.com/questions/11906925/python-simulate-keydown

        # takes threadId or 0
        GetKeyboardLayout = windll.user32.GetKeyboardLayout
        GetKeyboardLayout.argtypes = (wintypes.HKL,)
        GetKeyboardLayout.restype = wintypes.HKL

        # char to vk
        VkKeyScanExW = windll.user32.VkKeyScanExW
        VkKeyScanExW.argtypes = (wintypes.WCHAR, wintypes.HKL)  # WCHAR ch, HKL dwhkl

        # vk to sc
        MapVirtualKeyExW = windll.user32.MapVirtualKeyExW
        MapVirtualKeyExW.argtypes = (
            wintypes.UINT,  # UINT uCode
            wintypes.UINT,  # UINT uMapType
            wintypes.HKL,  # HKL dwhkl
        )

        # send key
        SendInput = windll.user32.SendInput
        SendInput.argtypes = (
            wintypes.UINT,  # UINT cInputs,
            ctypes.POINTER(INPUT),  # LPINPUT pInputs
            wintypes.INT,  # int cbSize
        )

        # disable capslock
        # SetKeyboardState = windll.user32.SetKeyboardState
        # SetKeyboardState.argtypes = (wintypes.PCHAR,)  # LPBYTE lpKeyState

        VK_RETURN = 0x0D
        VK_SHIFT = 0x10
        VK_MENU = 0x12  # alt
        VK_CAPITAL = 0x14  # capslock
        VK_SPACE = 0x20
        VK_LSHIFT = 0xA0
        VK_RSHIFT = 0xA1
        VK_LMENU = 0xA4
        VK_RMENU = 0xA5
        # 0 = 0x30
        # a = 0x41

        def __init__(self, keyint):
            self.keyint = keyint / 2000.0
            self.lut = {}
            self.hkl = Kbd.GetKeyboardLayout(0)
            debug(f"using hkl: {self.hkl:x}")

            self.mod_state = [False, False]
            self.mod_vk = Kbd.VK_LSHIFT, Kbd.VK_RMENU
            self.mod_sc = []
            for vk in self.mod_vk:
                sc = Kbd.MapVirtualKeyExW(vk, 0, self.hkl)
                self.mod_sc.append(sc)

            # buf = ctypes.create_string_buffer(256)
            # Kbd.SetKeyboardState(buf)

        def send(self, txt):
            for ch in txt:
                try:
                    vk, sc, mods = self.lut[ch]
                except KeyError:
                    if ch == "\n":
                        vk = Kbd.VK_RETURN
                    else:
                        vk = Kbd.VkKeyScanExW(ch, self.hkl)

                    mods = [bool(vk & 0x100), bool(vk & 0x600)]
                    vk = vk % 0x100
                    sc = Kbd.MapVirtualKeyExW(vk, 0, self.hkl)
                    self.lut[ch] = [vk, sc, mods]
                    # debug(f"{ch} {vk:x} {sc:x} {mods[0]} {mods[1]}")

                for n in range(len(mods)):
                    if self.mod_state[n] != mods[n]:
                        self.mod_state[n] = mods[n]
                        sta = 0 if mods[n] else 2
                        for _ in range(1):
                            mvk = self.mod_vk[n]
                            msc = self.mod_sc[n]
                            ki = KEYBDINPUT(mvk, msc, sta, 0, 0)
                            io = INPUT(1, _INPUTunion(ki=ki))
                            Kbd.SendInput(1, ctypes.byref(io), ctypes.sizeof(io))
                            if self.keyint > 0:
                                time.sleep(self.keyint)

                for sta in [0, 2]:
                    ki = KEYBDINPUT(vk, sc, sta, 0, 0)
                    io = INPUT(1, _INPUTunion(ki=ki))
                    Kbd.SendInput(1, ctypes.byref(io), ctypes.sizeof(io))
                    if self.keyint > 0:
                        time.sleep(self.keyint)

        def flush(self):
            pass


elif MACOS:
    # existing python keyboard libraries fail with virtualbox as target

    class Kbd(object):
        def __init__(self, keyint):
            self.keyint = keyint / 500.0  # ???
            self.batch = []
            self.vk = {
                "0": 29,
                "1": 18,
                "2": 19,
                "3": 20,
                "4": 21,
                "5": 23,
                "6": 22,
                "7": 26,
                "8": 28,
                "9": 25,
                ".": 47,
            }

        def send(self, txt):
            # debug(f"[{repr(txt)}]")
            self.batch.append(txt)
            if len(self.batch) >= 4:
                self.flush()

        def flush(self):
            p = sp.Popen(["osascript"], stdin=sp.PIPE, stderr=sp.PIPE)
            stdin = 'tell application "System Events"\n'
            for key in [a for b in self.batch for a in b]:
                cmd = None
                if key == '"':
                    key = r"\""
                elif key == "\\":
                    key = r"\\"
                elif key == "\n":
                    key = r"\n"
                elif key in self.vk:
                    cmd = f"key code {{{self.vk[key]}}}\n"

                if cmd is None:
                    cmd = f'keystroke "{key}"\n'

                if self.keyint > 0:
                    cmd += f"delay {self.keyint}\n"

                stdin += cmd
            stdin += 'end tell\nlog "ok"\n'

            p.stdin.write(stdin.encode("utf-8"))
            p.stdin.close()
            p.stderr.readline()
            self.batch = []


elif LINUX:

    class Kbd_Xdotool(object):
        def __init__(self, keyint):
            self.keyint = keyint
            self.batch = []

        def send(self, txt):
            txt = txt.replace("\n", "\r")
            self.batch.append(txt)
            if len(self.batch) >= 4:
                self.flush()

        def flush(self):
            # fmt: off
            cmd = [
                "xdotool", "type",
                "--delay", str(int(self.keyint)),
                "--clearmodifiers",
                "--args", "1",
                "".join(self.batch),
            ]
            # fmt: on

            p = sp.Popen(cmd, stdin=sp.PIPE)
            p.communicate()

            self.batch = []

    class Kbd_Pynput(object):
        def __init__(self, keyint):
            self.keyint = keyint / 2000.0
            self.kbd = KbdController()

        def send(self, txt):
            # return self.kbd.type(txt)
            txt = [KbdKey.enter if x == "\n" else x for x in list(txt)]
            for ch in txt:
                self.kbd.press(ch)
                if self.keyint > 0:
                    time.sleep(self.keyint)

                self.kbd.release(ch)
                if self.keyint > 0:
                    time.sleep(self.keyint)

        def flush(self):
            pass


class Typist(object):
    def __init__(self, ar, wfp, hwnd, use_xdt, encoding):
        self.ar = ar
        self.wfp = wfp
        self.hwnd = hwnd
        self.encoding = encoding
        self.compression = 1

        if not LINUX:
            self.kbd = Kbd(ar.keyint)
        elif use_xdt:
            self.kbd = Kbd_Xdotool(ar.keyint)
        else:
            self.kbd = Kbd_Pynput(ar.keyint)

        self.logfile = None
        if ar.debug:
            self.logfile = open("kxt.debug.09.kbd", "wb")

        self.dead = False
        self.q = Queue(64)
        self.lock = threading.Lock()

        thr = threading.Thread(target=self._w)
        # thr.daemon = True
        thr.start()

    def end_input(self):
        self.q.put(None)

    def put(self, txt, crlf=True):
        if crlf:
            txt += "\n"

        lines = txt.split("\n")
        for ln in lines[:-1]:
            self.q.put(ln + "\n")

        txt = lines[-1]
        if txt:
            self.q.put(txt)

        return not self.dead

    def _w(self):
        n_chars = 0
        t0 = time.time()
        last_info_msg = t0
        last_focus_check = 0
        while True:
            txt = self.q.get()
            if txt is None:
                break

            if self.dead:
                continue

            if self.wfp and time.time() - last_focus_check > 0.5:
                hwnd = self.wfp.get(False)
                # debug(f"focus @ {hwnd}")
                if hwnd != self.hwnd:
                    _, title = self.wfp.get(True)
                    error(f"lost focus to: ({hwnd}, {title})")
                    self.dead = True
                    continue

                last_focus_check = time.time()

            if self.logfile:
                self.logfile.write(txt.encode("utf-8"))

            self.kbd.send(txt)
            n_chars += len(txt)

            now = time.time()
            td = now - t0
            with self.lock:
                spd = n_chars / td
                spd *= self.encoding
                spd2 = spd / self.compression

            msg = (
                f"wire({spd/1024:.2f} kB/s) "
                + f"eff({spd2/1024:.2f} kB/s) "
                + f"chr({n_chars}) sec({td:.1f})"
            )

            td = now - last_info_msg
            if td < 2.2:
                debug(msg)
            else:
                info(msg)
                last_info_msg = now

        self.kbd.flush()

        debug("eof")
        if self.logfile:
            self.logfile.close()


def get_files(ar):
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
    total_size = 0
    for fn in fns:
        sr = os.stat(fn)
        if not stat.S_ISREG(sr.st_mode):
            skipped.append(fn)
            continue

        sz = sr.st_size
        ts = sr.st_mtime

        total_size += sz
        files.append({"fn": fn, "sz": sz, "ts": ts})

    if skipped:
        warn("skipped some items (non-folder and non-regular):")
        for fn in skipped:
            print(fn.decode(FS_ENC, "replace"))

        info("abort with ctrl-c, or hit enter to accept and continue")
        input()  # nosec: B322

    if not files:
        error("no files; aborting")
        sys.exit(1)

    return files


def sighandler(signo, frame):
    os._exit(0)


def main():
    logging.basicConfig(
        level=logging.DEBUG,  # INFO DEBUG
        format="\033[36m%(asctime)s.%(msecs)03d\033[0m %(message)s",
        datefmt="%H%M%S",
    )
    lh = logging.StreamHandler(sys.stderr)
    lh.setFormatter(LoggerFmt())
    logging.root.handlers = [lh]

    signal.signal(signal.SIGINT, sighandler)

    if WINDOWS:
        os.system("")

    debug(f"filesystem is {FS_ENC}")
    assert_deps()

    ap = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        prog="kxt",
        description="transfer files through keyboard simulation",
    )

    network_cmds = ["bash", "nc", "ncat", "netcat", "socat", "curl", "wget"]

    b64_def = 76

    wait_def = 2
    wait_ex = ""
    if HAVE_FOCUS_DETECT:
        wait_ex = " (after focus change)"
        wait_def = 0.2

    keyint_def = 0
    if WINDOWS:
        keyint_def = 1
    elif LINUX:
        keyint_def = 7

    # fmt: off
    ap.add_argument("-s", dest="delay", metavar="SECONDS", default=wait_def, type=float, help=f"wait{wait_ex} before starting, default {wait_def}")
    ap.add_argument("-p", dest="plain", action="store_true", help="just plaintext; no compression")
    ap.add_argument("-a", dest="arc", action="store_true", help="always create archive, even single files")
    ap.add_argument("-w", dest="windows", action="store_true", help="recipient is windows (vista or newer, unless plaintext)")
    ap.add_argument("-d", dest="debug", action="store_true", help="enable debug (logging + kxt.bin)")
    ap.add_argument("-l", dest="length", metavar="LETTERS", default=b64_def, type=int, help=f"num chars per line of base64, default {b64_def}")
    ap.add_argument("-t", dest="keyint", metavar="MSEC", default=keyint_def, type=float, help=f"time per keystroke, default {keyint_def} milisec")
    # ap.add_argument("-z", dest="zip", action="store_true", help="create zip archive, instead of tar.gz")
    # ap.add_argument("-c", dest="net", metavar="COMMAND", choices=network_cmds, help="network xfer using COMMAND on guest")

    if HAVE_FOCUS_DETECT:
        ap.add_argument("-nf", action="store_true", help="disable window focus checks")

    if LINUX and HAVE_XDOTOOL and HAVE_PYNPUT:
        ap.add_argument("--pynput", action="store_true", help="use pynput for keyboard, instead of xdotool")

    ap.add_argument("files", metavar="FILEPATH", nargs="+", help="files / folders to transmit")
    ar = ap.parse_args()
    # fmt: on

    warns = []

    chk_focus = HAVE_FOCUS_DETECT and not ar.nf

    use_xdt = False
    if LINUX and HAVE_XDOTOOL:
        if "pynput" in vars(ar) and ar.pynput:
            debug("keyboard provider: pynput")
        else:
            debug("keyboard provider: xdotool")
            use_xdt = True

    if LINUX and not use_xdt:
        e = "WARNING: pynput does not work if target is virtualbox/qemu/libvirt, please consider installing xdotool"
        warn(e)

    if not ar.debug:
        lh.setLevel(logging.INFO)

    files = get_files(ar)

    archive = True
    if len(files) == 1 and not ar.arc:
        archive = False

    if archive:
        info("WILL create a compressed archive")
    else:
        info("will NOT create an archive; single file without metadata")

    if archive and ar.plain:
        error("cannot send archive as plaintext")
        sys.exit(1)

    ##
    # decoder header/footer

    header = ""
    footer = ""
    linepre = ""
    linepost = ""
    if ar.plain:
        pass
    elif not ar.windows:
        header = "\nt=$(mktemp);awk '/^$/{exit}1'|base64 -d>$t\n"
        footer = '\necho "`h *$t"|md5sum -c&&'
        if not archive:
            footer += 'gzip -d<$t>\\\n\\\n"`f" &&rm -f $t'
        else:
            footer += "tar zxvf $t&&rm -f $t"
    else:
        header = "\necho ^\n"
        linepost = "^"
        fn = "`f.gz"
        if archive:
            fn = "kxt.tgz"

        footer = f' >%tmp%\\kxt &certutil -decode -f %tmp%\\kxt "{fn}" &&certutil -hashfile "{fn}" md5 &&"{fn}"'
        # &echo( &echo(   pls unpack `f.gz with 7zip'
        warns.append('pls verify checksum + unpack "`f.gz" with 7zip')  # TODO

    ##
    # window focus

    wfp = None
    if chk_focus:
        wfp = WindowFocusProvider()
        own_window = wfp.get(True)
        if not own_window:
            error("window focus detection failed; disabling feature")
            wfp = None

    if wfp:
        info(f"active window: {own_window}")
        warn(">> waiting for you to switch focus to the target window")
        while True:
            time.sleep(0.1)
            hwnd = wfp.get(False)
            if hwnd != own_window[0] and hwnd != 0:
                break

        target_window = wfp.get(True)
        info(f"target window: {target_window}")

    info(f"start in {ar.delay} seconds")
    time.sleep(ar.delay)

    ##
    # datasrc

    packer = None
    if archive:
        debug("stream source: tar")
        s = src = StreamTar()
        for fn in files:
            s.add_file(fn["fn"])

        s.end_input()
        # if ar.debug:
        #    s = StreamLog(s.collect(), "01.tar")

        debug("stream filter: gzip")
        s = packer = StreamGzip(s.collect())
        # if ar.debug:
        #    s = StreamLog(s.collect(), "02.gz")
    else:
        debug("stream source: file")
        s = src = StreamFile(files[0]["fn"])
        # if ar.debug:
        #    s = StreamLog(s.collect(), "01.file")

        if not ar.plain:
            debug("stream filter: gzip")
            s = packer = StreamGzip(s.collect())
            # if ar.debug:
            #    s = StreamLog(s.collect(), "02.gz")

    debug("stream filter: hash")
    s = hasher = StreamHash(s.collect())

    efficiency = 1
    if not ar.plain:
        debug("stream filter: base64")
        efficiency /= 1.35
        s = StreamBase64(s.collect())
        # if ar.debug:
        #    s = StreamLog(s.collect(), "03.b64")

    ##
    # stream it

    kbd = Typist(ar, wfp, hwnd, use_xdt, efficiency)
    kbd.put(header, False)
    # kbd.end_input()
    # time.sleep(999999)

    if False:
        kbd.put("".join([chr(n) for n in range(ord("a"), ord("z") + 1)]))
        kbd.put("".join([chr(n) for n in range(ord("A"), ord("Z") + 1)]))
        kbd.put(r"01234 56789-=`,./';[\]")
        kbd.put(r")!@#$%^&*(_+~<>?" + '"' + r":{|}")
        kbd.put("aAaAazAZazAZazcAZCazcAZCazcvAZCVazcvAZCV")
        kbd.put("-l-l--ll--ll---lll---lll----llll----llll")
        kbd.end_input()
        while not kbd.q.empty():
            time.sleep(0.1)
        os._exit(0)

    leftovers = ""
    wrap = ar.length
    for buf in s.collect():
        if not buf:
            break

        if packer and packer.ci > 100 and packer.co > 100:
            with kbd.lock:
                kbd.compression = packer.co / packer.ci

        buf = buf.decode("utf-8", "ignore")
        # .replace("\n", linepre + "\n" + linepost)
        buf = leftovers + buf
        if not ar.plain:
            while len(buf) >= wrap:
                kbd.put(linepre + buf[:wrap] + linepost)
                buf = buf[wrap:]

            leftovers = buf
        else:
            lines = buf.replace("\r", "").split("\n")
            for ln in lines[:-1]:
                kbd.put(ln)

            leftovers = lines[-1]

    if leftovers:
        kbd.put(linepre + leftovers + linepost)

    md5 = hasher.get_hash()
    fpath = files[0]["fn"].decode("utf-8", "ignore")
    fn = fpath.split("/")[-1].split("\\")[-1]
    # TODO normalize separator somewhere

    kbd.put(footer.replace("`h", md5).replace("`f", fn))
    kbd.end_input()

    while not kbd.q.empty():
        time.sleep(0.1)

    for w in warns:
        warn(w.replace("`h", md5).replace("`f", fn))

    info(f"checksum: {md5}")


if __name__ == "__main__":
    main()
