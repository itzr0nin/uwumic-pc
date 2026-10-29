#!/usr/bin/env python3
"""UwU Mic client for Windows (WO Mic style).

Enter the phone's IP and port, press Connect, and the phone's microphone is played
into the output device you choose. Pick "CABLE Input" (VB-Cable) and then select
"CABLE Output" as the microphone in Discord, OBS, games, etc.
"""
import datetime
import json
import math
import re
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import types
import urllib.request
import zipfile
import tkinter as tk
from tkinter import messagebox

# ----------------------------------------------------------------- global hotkey (Windows RegisterHotKey)
# Windows' own "register this key combination" call. It only gets told about the one combination you chose; it does
# not watch every keystroke the way a keyboard hook does (which is what antivirus programs flag as a keylogger).
WM_HOTKEY = 0x0312
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
_MODS = {"ctrl": MOD_CONTROL, "control": MOD_CONTROL, "alt": MOD_ALT, "shift": MOD_SHIFT,
         "win": MOD_WIN, "windows": MOD_WIN}
_VK = {"space": 0x20, "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B, "backspace": 0x08,
       "delete": 0x2E, "del": 0x2E, "insert": 0x2D, "ins": 0x2D, "home": 0x24, "end": 0x23,
       "pageup": 0x21, "page up": 0x21, "pgup": 0x21, "pagedown": 0x22, "page down": 0x22, "pgdn": 0x22,
       "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
       "-": 0xBD, "=": 0xBB, "+": 0xBB, "plus": 0xBB, ",": 0xBC, ".": 0xBE, ";": 0xBA, "/": 0xBF, "`": 0xC0,
       "[": 0xDB, "\\": 0xDC, "]": 0xDD, "'": 0xDE}
_CANON = ["space", "enter", "tab", "esc", "backspace", "delete", "insert", "home", "end", "pageup", "pagedown",
          "left", "up", "right", "down", "-", "=", ",", ".", ";", "/", "`", "[", "\\", "]", "'"]
_NAME_OF = {_VK[n]: n for n in reversed(_CANON)}
_MODIFIER_VKS = {0x10, 0x11, 0x12, 0x14, 0x5B, 0x5C, 0x90, 0x91, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5}


def parse_hotkey(text):
    """'ctrl+shift+m' -> (modifier flags, virtual-key code). Raises ValueError when it can't be understood."""
    t = (text or "").strip().lower()
    if not t:
        raise ValueError("no key chosen")
    parts = [t]
    if len(t) > 1:
        parts = t[:-2].split("+") + ["+"] if t.endswith("++") else t.split("+")
    mods, key = 0, None
    for part in (x.strip() for x in parts):
        if not part:
            continue
        if part in _MODS:
            mods |= _MODS[part]
        elif key is not None:
            raise ValueError("only one normal key is allowed")
        else:
            key = part
    if key is None:
        raise ValueError("a hotkey needs a key as well as modifiers")
    vk = _VK.get(key)
    if vk is None:
        if len(key) == 1 and key.isascii() and key.isalnum():
            vk = ord(key.upper())
        elif re.fullmatch(r"f([1-9]|1\d|2[0-4])", key):
            vk = 0x70 + int(key[1:]) - 1
        elif re.fullmatch(r"vk\d{1,3}", key) and 0 < int(key[2:]) < 255:
            vk = int(key[2:])
        else:
            raise ValueError(f"unsupported key '{key}'")
    return mods, vk


def vk_to_name(vk):
    """Windows virtual-key code -> the name used in saved hotkeys (None for modifier keys)."""
    if vk in _MODIFIER_VKS:
        return None
    if 0x30 <= vk <= 0x39 or 0x41 <= vk <= 0x5A:
        return chr(vk).lower()
    if 0x70 <= vk <= 0x87:
        return f"f{vk - 0x6F}"
    return _NAME_OF.get(vk) or (f"vk{vk}" if 0 < vk < 255 else None)


class _Win32:
    """The few Windows calls the hotkey needs."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.u, self.k, self.ct = ctypes.windll.user32, ctypes.windll.kernel32, ctypes
        self.msg = wintypes.MSG()

    def thread_id(self):
        return self.k.GetCurrentThreadId()

    def make_queue(self):
        self.u.PeekMessageW(self.ct.byref(self.msg), None, 0, 0, 0)

    def register(self, ident, mods, vk):
        return bool(self.u.RegisterHotKey(None, ident, mods, vk))

    def get_message(self):
        r = self.u.GetMessageW(self.ct.byref(self.msg), None, 0, 0)
        return 0 if r in (0, -1) else self.msg.message

    def unregister(self, ident):
        self.u.UnregisterHotKey(None, ident)

    def post_quit(self, tid):
        self.u.PostThreadMessageW(tid, 0x0012, 0, 0)


class HotkeyManager:
    """One global hotkey on its own thread. set() replaces it; stop() removes it."""

    def __init__(self, callback, api=None):
        self.callback, self.api = callback, api
        self.thread = self.tid = None

    def set(self, text):
        mods, vk = parse_hotkey(text)
        if self.api is None:
            self.api = _Win32()
        self.stop()
        api, ready, result = self.api, threading.Event(), {}

        def run():
            result["tid"] = api.thread_id()
            api.make_queue()
            result["ok"] = api.register(1, mods | MOD_NOREPEAT, vk)
            ready.set()
            if not result["ok"]:
                return
            while True:
                m = api.get_message()
                if m == WM_HOTKEY:
                    self.callback()
                elif m == 0:
                    break
            api.unregister(1)

        t = threading.Thread(target=run, daemon=True)
        t.start()
        ready.wait(3)
        self.thread, self.tid = t, result.get("tid")
        if not result.get("ok"):
            raise OSError("that key combination is already used by another program")

    def stop(self):
        t, tid, self.thread, self.tid = self.thread, self.tid, None, None
        if t is not None and tid is not None and self.api is not None:
            self.api.post_quit(tid)
            t.join(2)


def resource(name):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


MAGIC = b"UWUM"
HEADER = struct.Struct(">4sIBB")  # magic, sample rate, channels, bits
DEFAULT_PORT = 8125

CYAN, RED, AMBER, GREY = "#00E5FF", "#FF0A45", "#F5B544", "#6B7280"
INK_ON_CYAN = "#04121A"
SPEAKER_NOTE = "Play on speaker is on"
CONNECTED_TEXT = 'Connected - choose "CABLE Output" as the microphone in your app or game'
CFG_PATH = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "UwUMicClient.json")


# ----------------------------------------------------------------- audio plumbing
class Jitter:
    """Small jitter buffer: prefill before playing, drop old audio if latency builds up."""

    def __init__(self, rate, prefill=0.10, maxbuf=0.50):
        bps = rate * 2
        self.prefill = int(prefill * bps) // 2 * 2
        self.max = int(maxbuf * bps) // 2 * 2
        self.buf = bytearray()
        self.lock = threading.Lock()
        self.playing = False

    def push(self, data):
        with self.lock:
            self.buf += data
            if len(self.buf) > self.max:
                del self.buf[: len(self.buf) - self.prefill]

    def pull(self, n):
        with self.lock:
            if not self.playing:
                if len(self.buf) < self.prefill:
                    return b"\x00" * n
                self.playing = True
            if len(self.buf) < n:  # underrun: play what we have, rebuffer
                out = bytes(self.buf) + b"\x00" * (n - len(self.buf))
                self.buf.clear()
                self.playing = False
                return out
            out = bytes(self.buf[:n])
            del self.buf[:n]
            return out


class _Dummy:
    """Does nothing, whatever you do with it (call it, subclass it, read attributes from it)."""

    def __init__(self, *args, **kwargs):
        pass

    def __call__(self, *args, **kwargs):
        return _Dummy()

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return _Dummy()


class _AudiolabStandIn(types.ModuleType):
    """Stand-in for the "audiolab" package. pyrnnoise imports it only for its audio-FILE helpers, which this
    app never uses, but inside the packaged exe the real one can crash while loading (missing template folder).
    Every name in it is a do-nothing object."""

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return type(name, (_Dummy,), {})


class _AudiolabFinder:
    """Import hook: answers any `import audiolab...` (at any depth) with a stand-in module."""

    @staticmethod
    def find_spec(fullname, path=None, target=None):
        if fullname == "audiolab" or fullname.startswith("audiolab."):
            import importlib.machinery
            return importlib.machinery.ModuleSpec(fullname, _AudiolabFinder, is_package=True)
        return None

    @staticmethod
    def create_module(spec):
        m = _AudiolabStandIn(spec.name)
        m.__path__ = []
        return m

    @staticmethod
    def exec_module(module):
        pass


def load_rnnoise():
    """Returns pyrnnoise's RNNoise class, working around the audiolab problem described above."""
    import sys as _sys
    try:
        from pyrnnoise import RNNoise
        return RNNoise
    except Exception as first:
        if isinstance(first, ModuleNotFoundError) and getattr(first, "name", "") == "pyrnnoise":
            raise  # the library itself is missing: nothing to work around
        for name in [m for m in list(_sys.modules)
                     if m in ("audiolab", "pyrnnoise") or m.startswith(("audiolab.", "pyrnnoise."))]:
            _sys.modules.pop(name, None)
        if _AudiolabFinder not in _sys.meta_path:
            _sys.meta_path.insert(0, _AudiolabFinder)
        from pyrnnoise import RNNoise  # if this still fails, it is a different problem and is reported as such
        log(f"pyrnnoise loaded with a stand-in for audiolab (first error: {first!r})")
        return RNNoise


class _NativeRNNoise:
    """Talks straight to the RNNoise C library that ships inside pyrnnoise (through ctypes) and skips
    pyrnnoise's Python wrapper class, which leans on audio-file machinery this app does not need."""

    def __init__(self):
        import ctypes
        import importlib
        import numpy as np

        load_rnnoise()  # imports the package (using the audiolab stand-in if needed); raises if it is missing
        low = importlib.import_module("pyrnnoise.rnnoise")
        lib = getattr(low, "lib", None)
        if lib is None:
            lib = next((v for v in vars(low).values() if isinstance(v, ctypes.CDLL)), None)
        if lib is None and isinstance(getattr(low, "LIBRNNOISE", None), str):
            lib = ctypes.CDLL(low.LIBRNNOISE)
        if lib is None:
            raise RuntimeError("the native rnnoise library was not found inside pyrnnoise")
        fp = ctypes.POINTER(ctypes.c_float)
        lib.rnnoise_create.argtypes = [ctypes.c_void_p]
        lib.rnnoise_create.restype = ctypes.c_void_p
        lib.rnnoise_destroy.argtypes = [ctypes.c_void_p]
        lib.rnnoise_destroy.restype = None
        lib.rnnoise_process_frame.argtypes = [ctypes.c_void_p, fp, fp]
        lib.rnnoise_process_frame.restype = ctypes.c_float
        lib.rnnoise_get_frame_size.restype = ctypes.c_int
        size = lib.rnnoise_get_frame_size()
        if size != 480:
            raise RuntimeError(f"unexpected RNNoise frame size {size}")
        self.lib, self.fp, self.np = lib, fp, np
        self.state = lib.rnnoise_create(None)
        if not self.state:
            raise RuntimeError("rnnoise_create failed")

    def process(self, frame):
        """frame: 480 int16 samples (numpy). Returns the cleaned frame as little-endian int16 bytes."""
        np = self.np
        x = np.ascontiguousarray(frame, dtype=np.float32)  # RNNoise wants 16-bit-sized values stored as floats
        y = np.empty(480, dtype=np.float32)
        self.lib.rnnoise_process_frame(self.state, y.ctypes.data_as(self.fp), x.ctypes.data_as(self.fp))
        return np.clip(np.rint(y), -32768, 32767).astype("<i2").tobytes()

    def close(self):
        state, self.state = self.state, None
        if state:
            try:
                self.lib.rnnoise_destroy(state)
            except Exception:
                pass


class NoiseCanceller:
    """RNNoise: a small neural network that removes background noise from a voice.
    Works on the 48 kHz mono 16-bit audio the phone sends, in chunks of any size."""

    FRAME = 480  # 10 ms at 48 kHz, what RNNoise processes at a time

    def __init__(self):
        import numpy as np

        self.np = np
        self.buf = bytearray()
        self.call = None  # (wrapper route only) how this version of pyrnnoise wants to be called
        self.native = None
        self.rn = None
        try:
            self.native = _NativeRNNoise()
            self.mode = "native library"
            log("RNNoise: using the native library directly")
        except Exception as e:
            log(f"RNNoise native route unavailable ({e!r}); trying pyrnnoise's own class")
            RNNoise = load_rnnoise()
            self.rn = RNNoise(sample_rate=48000)
            self.mode = "pyrnnoise class"

    def close(self):
        if self.native is not None:
            self.native.close()

    def reset(self):
        self.buf.clear()

    def _tidy(self, audio):
        np = self.np
        a = np.asarray(audio).reshape(-1)
        if a.size != self.FRAME:
            raise ValueError(f"unexpected frame size {a.size}")
        if a.dtype.kind == "f" and float(np.max(np.abs(a))) <= 1.5:
            a = a * 32768.0
        return np.clip(a, -32768, 32767).astype("<i2")

    def _parse(self, r):
        if isinstance(r, tuple) and len(r) == 2:
            return self._tidy(r[1])
        if isinstance(r, list) and r:  # denoise_chunk yields (speech probability, frame)
            return self._tidy(self.np.concatenate([self.np.asarray(x[1]).reshape(-1) for x in r]))
        raise ValueError("unexpected result from RNNoise")

    def _frame(self, frame):
        if self.native is not None:
            return self.native.process(frame)
        if self.call is None:
            tried = []
            for name, fn in (("frame-1d", lambda f: self.rn.denoise_frame(f)),
                             ("frame-2d", lambda f: self.rn.denoise_frame(f.reshape(1, -1))),
                             ("chunk", lambda f: list(self.rn.denoise_chunk(f.reshape(1, -1))))):
                try:
                    out = self._parse(fn(frame))
                except Exception as e:
                    tried.append(f"{name}: {e}")
                    continue
                self.call = fn
                log("RNNoise call style: " + name)
                return out.tobytes()
            raise RuntimeError("could not use pyrnnoise (" + "; ".join(tried) + ")")
        return self._parse(self.call(frame)).tobytes()

    def process(self, data):
        """data = PCM16 little-endian mono bytes. Returns cleaned bytes (a few ms behind the input)."""
        self.buf += data
        step = self.FRAME * 2
        n = len(self.buf) // step
        if n == 0:
            return b""
        np = self.np
        arr = np.frombuffer(bytes(self.buf[: n * step]), dtype="<i2")
        del self.buf[: n * step]
        return b"".join(self._frame(arr[k * self.FRAME:(k + 1) * self.FRAME].copy()) for k in range(n))


def run_selftest(path):
    """UwUMicClient.exe --selftest out.txt : checks that noise cancellation really works in this build."""
    lines, code = [], 1
    try:
        import numpy as np
        try:
            import importlib.metadata as md
            lines.append("pyrnnoise " + md.version("pyrnnoise"))
        except Exception:
            pass
        nc = NoiseCanceller()
        rng = np.random.default_rng(1)
        noise = rng.normal(0, 600, 96000).clip(-32768, 32767).astype("<i2")  # 2 s of steady noise
        raw = noise.tobytes()
        out = b"".join(nc.process(raw[i:i + 1920]) for i in range(0, len(raw), 1920))
        o = np.frombuffer(out, dtype="<i2").astype(np.float64)
        n = noise.astype(np.float64)
        in_rms = float(np.sqrt(np.mean(n[len(n) // 2:] ** 2)))
        out_rms = float(np.sqrt(np.mean(o[len(o) // 2:] ** 2))) + 1e-9
        drop = 20 * np.log10(in_rms / out_rms)
        lines.append(f"route: {nc.mode}; samples in {len(n)} out {len(o)}")
        lines.append(f"steady noise reduced by {drop:.1f} dB")
        if len(out) == len(raw) and drop >= 6:
            lines.append("RESULT: OK")
            code = 0
        else:
            lines.append("RESULT: FAILED (output length or noise reduction is wrong)")
    except Exception:
        lines.append("RESULT: FAILED")
        lines.append(traceback.format_exc())
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception:
        pass
    return code


class _Out:
    """One Windows output stream fed from its own jitter buffer, with volume and mute applied."""

    def __init__(self, rate, device, app, meter):
        import numpy as np
        import sounddevice as sd

        self.jit = Jitter(rate)
        self.channels = 2

        def callback(outdata, frames, time_info, status):
            raw = self.jit.pull(frames * 2)
            a = np.frombuffer(raw, dtype="<i2").astype(np.float32)
            g = 0.0 if app.muted else app.gain
            a = np.clip(a * g, -32768, 32767)
            if meter:
                app.level = float(np.max(np.abs(a))) / 32768 if a.size else 0.0
            a = a.astype("<i2")
            if self.channels == 2:
                a = np.repeat(a, 2)
            outdata[:] = a.reshape(-1, self.channels)

        extra = None
        try:
            api = sd.query_hostapis(sd.query_devices(device)["hostapi"])["name"]
            if "WASAPI" in api:
                extra = sd.WasapiSettings(auto_convert=True)
        except Exception:
            pass
        extras = [extra, None] if extra is not None else [None]
        last = None
        for ch in (2, 1):
            for latency in ("low", "high"):
                for ex in extras:
                    self.channels = ch
                    stream = None
                    try:
                        stream = sd.OutputStream(samplerate=rate, device=device, channels=ch, dtype="int16",
                                                 callback=callback, blocksize=0, latency=latency,
                                                 extra_settings=ex)
                        stream.start()
                        self.stream = stream
                        return
                    except Exception as e:
                        last = e
                        if stream is not None:
                            try:
                                stream.close()
                            except Exception:
                                pass
        raise last

    def push(self, data):
        self.jit.push(data)

    def close(self):
        try:
            self.stream.stop()
            self.stream.close()
        except Exception:
            pass


class AudioSink:
    """Plays the phone's audio into the chosen output (e.g. CABLE Input) and, only when asked,
    also into a speaker so you can hear what is being received."""

    def __init__(self, rate, devices, app):
        self.app = app
        self.rate = rate
        self.mon = None
        self.nc = None
        if isinstance(devices, int):
            devices = [devices]
        names = dict(getattr(app, "devs", []))
        self.main, self.device, last, tried = None, None, None, 0
        for d in devices:
            tried += 1
            try:
                self.main = _Out(rate, d, app, True)
                self.device = d
                log(f"output opened: {names.get(d, d)}")
                break
            except Exception as e:
                last = e
                log(f"output failed: {names.get(d, d)}: {e}")
        if self.main is None:
            raise RuntimeError(f"could not open the virtual microphone ({tried} outputs tried): {last}")
        app.streaming = True
        app.sink = self
        if app.monitor_device is not None:
            self.set_monitor(app.monitor_device)

    def set_monitor(self, device):
        """device = Windows device index to also play on, or None to switch the speaker off."""
        old, self.mon = self.mon, None
        if old is not None:
            old.close()
        if device is None:
            return
        if device == self.device:
            self.app.status = "Speaker test: choose a different device than the output above"
            return
        try:
            self.mon = _Out(self.rate, device, self.app, False)
        except Exception as e:
            self.app.status = f"Speaker error: {e}"

    def push(self, data):
        app = self.app
        app.pkts = getattr(app, "pkts", 0) + 1
        if app.noise_cancel and not app.nc_error:
            if self.nc is None:
                try:
                    self.nc = NoiseCanceller()
                except Exception as e:
                    app.nc_error = str(e)[:120]
                    log("noise cancellation unavailable: " + traceback.format_exc())
            if self.nc is not None:
                try:
                    data = self.nc.process(data)
                except Exception as e:
                    app.nc_error = str(e)[:120]
                    log("noise cancellation failed: " + traceback.format_exc())
                    self.nc = None
        elif self.nc is not None:
            self.nc.reset()
        if not data:
            return
        self.main.push(data)
        mon = self.mon
        if mon is not None:
            mon.push(data)

    def close(self):
        mon, self.mon = self.mon, None
        if mon is not None:
            mon.close()
        if self.nc is not None:
            self.nc.close()
            self.nc = None
        self.main.close()
        self.app.streaming = False
        self.app.level = 0.0
        if getattr(self.app, "sink", None) is self:
            self.app.sink = None


# ----------------------------------------------------------------- network
def recv_exact(sock, n, stop):
    buf = b""
    while len(buf) < n:
        if stop.is_set():
            raise ConnectionAbortedError("stopped")
        try:
            d = sock.recv(n - len(buf))
        except socket.timeout:
            continue
        if not d:
            raise ConnectionError("phone closed the connection")
        buf += d
    return buf


def stream(host, port, stop, make_sink, status, on_connect=None):
    """Connect once and pump audio until the connection ends or `stop` is set."""
    sock = sink = None
    try:
        status(f"Connecting to {host}:{port} ...")
        t0 = time.perf_counter()
        sock = socket.create_connection((host, port), timeout=5)
        if on_connect:
            on_connect(round((time.perf_counter() - t0) * 1000))
        sock.settimeout(1)
        magic, rate, ch, bits = HEADER.unpack(recv_exact(sock, HEADER.size, stop))
        if magic != MAGIC or ch != 1 or bits != 16:
            raise ValueError("that is not the UwU Mic phone app")
        sink = make_sink(rate)
        status(CONNECTED_TEXT)
        carry = b""
        while not stop.is_set():
            try:
                d = sock.recv(4096)
            except socket.timeout:
                continue
            if not d:
                raise ConnectionError("phone closed the connection")
            d = carry + d
            if len(d) % 2:
                carry, d = d[-1:], d[:-1]
            else:
                carry = b""
            if d:
                sink.push(d)
    except ConnectionAbortedError:
        pass
    except Exception as e:
        status(f"{e}")
    finally:
        if sink:
            sink.close()
        if sock:
            try:
                sock.close()
            except OSError:
                pass


# ----------------------------------------------------------------- virtual microphone installer
VB_URLS = [
    "https://download.vb-audio.com/Download_CABLE/VBCABLE_Driver_Pack45.zip",   # current package (Oct 2024)
    "https://download.vb-audio.com/Download_CABLE/VBCABLE_Driver_Pack43.zip",   # old package, fallback only
]
VB_URL = VB_URLS[0]
AUTO_SETUP_TRIES = 3   # automatic setups attempted on startup before the app stops trying on its own


def log_path():
    return os.path.join(os.path.dirname(CFG_PATH), "UwUMicClient.log")


def log(msg):
    """Append a line to UwUMicClient.log (next to the settings file) so a failed setup can be diagnosed."""
    try:
        with open(log_path(), "a", encoding="utf-8") as f:
            f.write(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S ") + str(msg) + "\n")
    except Exception:
        pass


def download(url, dest, progress=None, timeout=30, min_size=100_000):
    """Download url to dest (HTTPS), reporting percent via progress(pct)."""
    req = urllib.request.Request(url, headers={"User-Agent": "UwUMic/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        got = 0
        while True:
            chunk = r.read(65536)
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
            if progress and total:
                progress(min(100, got * 100 // total))
    if os.path.getsize(dest) < min_size:
        raise ValueError("the download looks incomplete")
    return dest


def pick_setup(paths, arch):
    """Choose the right VB-Cable setup program for this Windows from the unpacked file list."""
    exes = [p for p in paths if p.lower().endswith(".exe") and "setup" in os.path.basename(p).lower()]
    name = lambda p: os.path.basename(p).lower()
    arch = (arch or "").upper()
    if "ARM64" in arch:
        prefs = [lambda n: "arm64" in n, lambda n: "x64" in n]
    elif "64" in arch:
        prefs = [lambda n: "x64" in n and "arm" not in n]
    else:
        prefs = [lambda n: "x64" not in n and "arm" not in n]
    for pref in prefs:
        for p in exes:
            if pref(name(p)):
                return p
    if exes:
        return exes[0]
    raise FileNotFoundError("no setup program in the driver pack (found: "
                            + ", ".join(sorted(os.path.basename(p) for p in paths)[:20]) + ")")


def extract_setup(zip_path, outdir):
    """Unzip the driver pack and return the path of the right setup program for this Windows."""
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(outdir)
    arch = os.environ.get("PROCESSOR_ARCHITEW6432") or os.environ.get("PROCESSOR_ARCHITECTURE") or ""
    paths = [os.path.join(folder, n) for folder, _, files in os.walk(outdir) for n in files]
    log("driver pack contains: " + ", ".join(sorted(os.path.basename(p) for p in paths)))
    return pick_setup(paths, arch)


def parse_signature(output):
    lines = [ln.strip() for ln in (output or "").strip().splitlines() if ln.strip()]
    return (lines[0] if lines else ""), (lines[1] if len(lines) > 1 else "")


def check_signature(path):
    """Ask Windows whether the file carries a valid digital signature. Returns (status, signer)."""
    env = dict(os.environ, UWU_FILE=path)
    cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command",
           "$s = Get-AuthenticodeSignature -LiteralPath $env:UWU_FILE; $s.Status; "
           "if ($s.SignerCertificate) { $s.SignerCertificate.Subject }"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=env, stdin=subprocess.DEVNULL,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return parse_signature(r.stdout)


def run_elevated(exe, args):
    """Start a program with administrator rights (Windows shows its permission prompt)."""
    import ctypes
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, args, os.path.dirname(exe), 1)
    return rc > 32


def refresh_audio_devices():
    """Make PortAudio notice newly installed devices (only safe while no stream is open)."""
    import sounddevice as sd
    sd._terminate()
    sd._initialize()


def is_virtual(name):
    """True for virtual-cable style outputs (VB-Cable, VoiceMeeter, ...) that make no sound on speakers."""
    n = (name or "").lower()
    return any(k in n for k in ("cable", "vb-audio", "voicemeeter", "virtual"))


def api_rank(label):
    """Lower is better. WASAPI is best, WDM-KS is avoided."""
    low = (label or "").lower()
    for key, rank in (("wasapi", 0), ("mme", 1), ("directsound", 2), ("wdm-ks", 9)):
        if key in low:
            return rank
    return 5


def cable_candidates(devs):
    """devs = [(index, label), ...]. Returns every usable output of the virtual microphone ("CABLE Input"),
    best first: the plain VB-Cable before A/B/C/D variants, WASAPI, then MME, DirectSound and WDM-KS last."""
    extra = ("cable-a", "cable-b", "cable-c", "cable-d", "a+b", "c+d", "hi-fi", "hifi")
    cands = [(i, n) for i, n in devs if "cable input" in n.lower()]
    if not cands:  # some other virtual cable (VoiceMeeter, ...)
        cands = [(i, n) for i, n in devs if is_virtual(n) and "input" in n.lower()]
    cands.sort(key=lambda c: (any(k in c[1].lower() for k in extra), api_rank(c[1]), c[0]))
    return [i for i, _ in cands]


def pick_cable(devs):
    """The single best virtual-microphone output, or None."""
    c = cable_candidates(devs)
    return c[0] if c else None


def pick_speaker(devs, default_index):
    """Index of the device for 'Play on speaker': your Windows default output, never a virtual cable."""
    names = dict(devs)
    if default_index in names and not is_virtual(names[default_index]):
        return default_index
    others = [(api_rank(n), i) for i, n in devs if not is_virtual(n) and "mapper" not in n.lower()]
    return min(others)[1] if others else None


def default_output_index():
    try:
        import sounddevice as sd
        idx = sd.default.device[1]
        return idx if isinstance(idx, int) and idx >= 0 else None
    except Exception:
        return None


def output_devices():
    import sounddevice as sd

    apis = sd.query_hostapis()
    out = []
    for i, d in enumerate(sd.query_devices()):
        if d["max_output_channels"] > 0:
            out.append((i, f"{d['name']}  [{apis[d['hostapi']]['name']}]"))
    return out


# ----------------------------------------------------------------- look
BG, HEAD_BG, PANEL, FIELD = "#050911", "#070C15", "#09111D", "#03060B"
EDGE, EDGE_HI, TEAL_EDGE = "#14243A", "#1F3B57", "#0E5566"
TXT, TXT_SOFT, DIM, DIMMER = "#E9F2FB", "#B8C7D9", "#6C7F99", "#46566C"
CYAN_BG, CYAN_EDGE = "#052B34", "#0B7A8C"
RED_BG, RED_EDGE = "#2A0714", "#9A1238"
GREEN, GREEN_BG, GREEN_EDGE = "#19D9AE", "#062A24", "#0C7A64"
AMBER_BG, AMBER_EDGE = "#2A2108", "#8A6A12"
APP_VERSION = "1.1.0"
F_UI, F_MONO, F_DISP = "Segoe UI", "Consolas", "Bahnschrift"


def mix(c1, c2, t):
    """Colour between two #rrggbb colours (t = 0 .. 1)."""
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(a, b))


def clip_x(poly, x0, x1):
    """Clip a polygon to the vertical strip x0 <= x <= x1 (used to paint gradients inside odd shapes)."""
    def clip(pts, inside, inter):
        out = []
        for i, cur in enumerate(pts):
            prev = pts[i - 1]
            if inside(cur):
                if not inside(prev):
                    out.append(inter(prev, cur))
                out.append(cur)
            elif inside(prev):
                out.append(inter(prev, cur))
        return out

    def at(xv):
        return lambda a, b: (xv, a[1] + (b[1] - a[1]) * (xv - a[0]) / (b[0] - a[0]))

    pts = clip(poly, lambda p: p[0] >= x0, at(x0))
    return clip(pts, lambda p: p[0] <= x1, at(x1)) if pts else []


def shorten(text, n):
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "\u2026"


# ----------------------------------------------------------------- merged meter + volume
class LevelSlider(tk.Canvas):
    """One bar: the bright fill is the live loudness, the glowing line and hexagon handle are the volume (0-100%)."""

    PAD = 12

    def __init__(self, master, command, value=1.0, width=330, height=40, bg=PANEL, scale=1.0):
        super().__init__(master, width=width, height=height, bg=bg, highlightthickness=0, cursor="hand2")
        self.command = command
        self.value = value
        self.level = 0.0
        self.muted = False
        self.k = scale
        self.bind("<Button-1>", self._drag)
        self.bind("<B1-Motion>", self._drag)
        self.bind("<Configure>", lambda e: self.redraw())
        self.redraw()

    def _size(self):
        w, h = self.winfo_width(), self.winfo_height()
        if w <= 1:
            w = int(self.cget("width"))
        if h <= 1:
            h = int(self.cget("height"))
        return w, h

    def set_state(self, value, level, muted):
        level = round(level, 3)
        if (value, level, muted) != (self.value, self.level, self.muted):
            self.value, self.level, self.muted = value, level, muted
            self.redraw()

    def _drag(self, e):
        w, _ = self._size()
        pad = self.PAD * self.k
        span = w - 2 * pad
        if span <= 0:
            return
        v = round(min(1.0, max(0.0, (e.x - pad) / span)) * 100) / 100
        if v != self.value:
            self.value = v
            self.redraw()
            self.command(v)

    def redraw(self):
        self.delete("all")
        k = self.k
        w, h = self._size()
        left, right, cy = self.PAD * k, w - self.PAD * k, h / 2
        span = right - left
        th = 6 * k
        self.create_rectangle(left, cy - th / 2, right, cy + th / 2, fill="#0A1626", outline="#16314A")
        x = left + span * self.value
        if not self.muted and self.value > 0:  # glowing volume line
            for width, col in ((9 * k, "#062F39"), (5 * k, "#0A6070"), (2 * k, CYAN)):
                self.create_line(left, cy, x, cy, fill=col, width=max(1, round(width)))
        if not self.muted and self.level > 0:  # live loudness
            col = RED if self.level > 0.9 else AMBER if self.level > 0.7 else CYAN
            self.create_rectangle(left, cy - th / 2 + k, left + span * self.level, cy + th / 2 - k, fill=col, outline="")
        body = "#5B6573" if self.muted else CYAN
        shine = "#98A2B0" if self.muted else "#C4F8FF"
        for hw, hh, col, edge in ((8 * k, 13 * k, body, "#0A8FA3" if not self.muted else "#3A4452"),
                                  (3.5 * k, 6.5 * k, shine, "")):
            self.create_polygon(x, cy - hh, x + hw, cy - hh * 0.55, x + hw, cy + hh * 0.55,
                                x, cy + hh, x - hw, cy + hh * 0.55, x - hw, cy - hh * 0.55,
                                fill=col, outline=edge)


class Painter:
    """Draws in logical (96 dpi) units, scaled for the real screen."""

    def __init__(self, canvas, scale, measure):
        self.c, self.S, self.measure = canvas, scale, measure

    def _w(self, width):
        return max(1, round(width * self.S))

    def rect(self, x1, y1, x2, y2, fill="", outline="", width=1, tags=()):
        S = self.S
        return self.c.create_rectangle(x1 * S, y1 * S, x2 * S, y2 * S, fill=fill, outline=outline,
                                       width=self._w(width) if outline else 0, tags=tags)

    def poly(self, pts, fill="", outline="", width=1, tags=()):
        flat = [v * self.S for pt in pts for v in pt]
        return self.c.create_polygon(*flat, fill=fill, outline=outline,
                                     width=self._w(width) if outline else 0, tags=tags)

    def line(self, pts, fill, width=1, tags=()):
        flat = [v * self.S for pt in pts for v in pt]
        return self.c.create_line(*flat, fill=fill, width=self._w(width), tags=tags)

    def oval(self, x1, y1, x2, y2, fill="", outline="", width=1, tags=()):
        S = self.S
        return self.c.create_oval(x1 * S, y1 * S, x2 * S, y2 * S, fill=fill, outline=outline,
                                  width=self._w(width) if outline else 0, tags=tags)

    def tw(self, s, font, track=0):
        """Width of a text in logical units."""
        return self.measure(font, s) / self.S + track * max(0, len(s) - 1)

    def text(self, x, y, s, fill, font, anchor="w", tags=(), track=0, wrap=None):
        S = self.S
        if not track:
            kw = {"width": wrap * S} if wrap else {}
            self.c.create_text(x * S, y * S, text=s, fill=fill, font=font, anchor=anchor, tags=tags, **kw)
            return self.tw(s, font)
        total = self.tw(s, font, track)
        cx = x - total if anchor == "e" else x - total / 2 if anchor in ("c", "center") else x
        for ch in s:
            self.c.create_text(cx * S, y * S, text=ch, fill=fill, font=font, anchor="w", tags=tags)
            cx += self.measure(font, ch) / S + track
        return total

    def chip(self, x, cy, s, fg, bg, edge, anchor="w", dot=None, tags=(), font=(F_MONO, 8, "bold"), track=0.8):
        """Small label box. x is its left edge (anchor 'w') or right edge ('e'). Returns its width."""
        pad = 8
        tw = self.tw(s, font, track)
        w = tw + 2 * pad + (11 if dot else 0)
        x0 = x - w if anchor == "e" else x
        self.rect(x0, cy - 10, x0 + w, cy + 10, fill=bg, outline=edge, tags=tags)
        tx = x0 + pad
        if dot:
            self.oval(tx, cy - 3, tx + 6, cy + 3, fill=dot, tags=tags)
            tx += 11
        self.text(tx, cy, s, fg, font, tags=tags, track=track)
        return w


class Ui:
    """The window: a dark, cyan 'receiver' look drawn on one canvas, driven by the state in App."""

    W, H = 600, 566

    def __init__(self, root, app):
        import tkinter.font as tkfont

        self.root, self.app = root, app
        self.S = max(1.0, float(root.winfo_fpixels("1i")) / 96.0)
        S = self.S
        self._fonts = {}

        def measure(font, text):
            f = self._fonts.get(font)
            if f is None:
                weight = font[2] if len(font) > 2 else "normal"
                f = self._fonts[font] = tkfont.Font(root=root, family=font[0], size=font[1], weight=weight)
            return f.measure(text)

        root.configure(bg=BG)
        root.geometry(f"{round(self.W * S)}x{round(self.H * S)}")
        self.c = tk.Canvas(root, width=round(self.W * S), height=round(self.H * S), bg=BG, highlightthickness=0, bd=0)
        self.c.pack()
        self.p = Painter(self.c, S, measure)
        self.keys = {}
        self.focus = {"ip": False, "port": False}
        self._static()
        self._widgets()
        self._actions()
        self.dark_title_bar()

    # -- helpers
    def d(self, v):
        return round(v * self.S)

    def dark_title_bar(self):
        if sys.platform != "win32":
            return
        try:
            import ctypes
            self.root.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            for attr in (20, 19):  # dark title bar (Windows 10/11)
                ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(ctypes.c_int(1)), 4)
            rgb = int(HEAD_BG[5:7], 16) << 16 | int(HEAD_BG[3:5], 16) << 8 | int(HEAD_BG[1:3], 16)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(ctypes.c_int(rgb)), 4)  # Windows 11
        except Exception:
            pass

    # -- fixed parts
    def _static(self):
        p, W, H = self.p, self.W, self.H
        for x in range(0, W + 1, 24):
            p.line([(x, 0), (x, H)], "#08131E")
        for y in range(0, H + 1, 24):
            p.line([(0, y), (W, y)], "#08131E")
        p.rect(0, 0, W, 2, fill="#0AA9BE")
        p.rect(0, 2, W, 46, fill=HEAD_BG)
        p.line([(0, 46), (W, 46)], EDGE)
        p.poly([(30, 15), (39, 24), (30, 33), (21, 24)], fill=CYAN)
        p.poly([(30, 19), (35, 24), (30, 29), (25, 24)], fill=HEAD_BG)
        x = 50
        x += p.text(x, 24, "UwU Mic", TXT, (F_DISP, 13, "bold")) + 8
        x += p.text(x, 24, "// PC CLIENT", CYAN, (F_DISP, 10, "bold")) + 10
        p.chip(x, 24, f"v{APP_VERSION}", DIM, HEAD_BG, EDGE, font=(F_MONO, 8, "normal"), track=0.3)
        p.text(20, 66, "AUDIO STREAM RECEIVER", DIM, (F_MONO, 9, "normal"), track=1.6)
        # sample rate / format, right aligned
        parts = [("SAMPLE RATE:", DIM, "normal"), (" 48.0 kHz", TXT, "bold"), ("  |  ", DIMMER, "normal"),
                 ("FORMAT:", DIM, "normal"), (" PCM 16-bit", TXT, "bold")]
        fnt = lambda wt: (F_MONO, 8, wt)
        total = sum(p.tw(s, fnt(wt)) for s, _, wt in parts)
        x = W - 20 - total
        for s, col, wt in parts:
            x += p.text(x, 66, s, col, fnt(wt))
        p.line([(20, 112), (W - 20, 112)], EDGE)
        for x, w, y in ((20, 368, 124), (400, 180, 124)):
            p.rect(x, y, x + w, y + 80, fill=PANEL, outline=EDGE)
        p.text(414, 142, "Port", DIM, (F_UI, 9, "normal"))
        p.rect(20, 280, 580, 512, fill=PANEL, outline=EDGE)
        p.line([(34, 358), (566, 358)], EDGE)
        p.text(38, 376, "VOLUME OUTPUT", DIM, (F_MONO, 9, "normal"), track=1.2)
        x0, span = 48, 504  # under the slider (it starts at x=36 and has 12 px of padding)
        for pct in (0, 25, 50, 75, 100):
            p.text(x0 + span * pct / 100, 438, f"{pct}%", DIMMER, (F_MONO, 8, "normal"), anchor="c")
        p.rect(0, 528, W, H, fill=HEAD_BG)
        p.line([(0, 528), (W, 528)], EDGE)

    # -- real widgets
    def _widgets(self):
        a, p, S, c = self.app, self.p, self.S, self.c
        digits = (self.root.register(lambda s: s == "" or (s.isdigit() and len(s) <= 5)), "%P")
        common = dict(bg=FIELD, fg=TXT, insertbackground=CYAN, relief="flat", bd=0, highlightthickness=0,
                      disabledbackground=FIELD, disabledforeground=CYAN, font=(F_MONO, 13, "bold"))
        self.ip_e = tk.Entry(self.root, textvariable=a.ip, **common)
        self.port_e = tk.Entry(self.root, textvariable=a.port, justify="center", validate="key",
                               validatecommand=digits, **common)
        c.create_window(self.d(20 + 10 + 40), self.d(124 + 52), window=self.ip_e, anchor="w",
                        width=self.d(368 - 20 - 44), height=self.d(26))
        c.create_window(self.d(400 + 90), self.d(124 + 52), window=self.port_e, anchor="center",
                        width=self.d(180 - 30), height=self.d(26))
        for key, e in (("ip", self.ip_e), ("port", self.port_e)):
            e.bind("<FocusIn>", lambda ev, k=key: self._focus(k, True))
            e.bind("<FocusOut>", lambda ev, k=key: self._focus(k, False))
        self.slider = LevelSlider(self.root, a.on_vol, a.gain, width=self.d(528), height=self.d(40), bg=PANEL, scale=S)
        c.create_window(self.d(36), self.d(280 + 114), window=self.slider, anchor="nw")

    def _focus(self, key, on):
        self.focus[key] = on
        self.keys.pop("cards", None)

    def set_locked(self, on):
        for e in (self.ip_e, self.port_e):
            e.config(state="disabled" if on else "normal")
        self.keys.clear()

    def _actions(self):
        c = self.c
        acts = {"act_connect": self.app.toggle, "act_nc": lambda: self._flip(self.app.nc_var, self.app.on_noise),
                "act_mon": lambda: self._flip(self.app.mon_var, self.app.on_monitor),
                "act_mute": self.app.toggle_mute, "act_hotkey": self.app.capture_hotkey}
        for tag, fn in acts.items():
            c.tag_bind(tag, "<Button-1>", lambda ev, f=fn: f())
            c.tag_bind(tag, "<Enter>", lambda ev: c.config(cursor="hand2"))
            c.tag_bind(tag, "<Leave>", lambda ev: c.config(cursor=""))

    @staticmethod
    def _flip(var, handler):
        var.set(not var.get())
        handler()

    # -- state -> pictures
    def render(self):
        a = self.app
        st = dict(active=bool(a.active), streaming=bool(a.streaming), muted=bool(a.muted),
                  pct=round(a.gain * 100), nc=bool(a.nc_var.get()), mon=bool(a.mon_var.get()))
        mode = "live" if st["streaming"] else "connecting" if st["active"] else "off"
        layers = {
            "hdr": (mode,),
            "chips": (st["streaming"], st["muted"]),
            "cards": (mode, self.focus["ip"], self.focus["port"]),
            "btn": (st["active"],),
            "opts": (st["nc"], st["mon"], mode == "live"),
            "vol": (st["muted"], st["pct"], self._db_text()),
            "mute": (st["muted"], a.hotkey_label()),
            "foot": (mode, a.status, a.ip.get(), a.port.get(), a.pps, a.ping_ms),
        }
        for name, key in layers.items():
            if self.keys.get(name) != key:
                self.keys[name] = key
                self.c.delete(name)
                getattr(self, "_draw_" + name)(st, mode)
        self.slider.set_state(0.0 if st["muted"] else a.gain, a.shown, st["muted"])

    def _db_text(self):
        s = self.app.shown
        return "-\u221e dBFS" if s < 0.003 else f"{20 * math.log10(min(1.0, s)):.1f} dBFS"

    def _draw_hdr(self, st, mode):
        p, W = self.p, self.W
        txt, fg, bg, edge, dot = {
            "live": ("CONNECTED", CYAN, CYAN_BG, CYAN_EDGE, GREEN),
            "connecting": ("CONNECTING", AMBER, AMBER_BG, AMBER_EDGE, AMBER),
            "off": ("DISCONNECTED", DIM, HEAD_BG, EDGE_HI, AMBER),
        }[mode]
        p.chip(W - 20, 24, txt, fg, bg, edge, anchor="e", dot=dot, tags=("hdr",), font=(F_MONO, 8, "bold"), track=1.0)

    def _draw_chips(self, st, mode):
        p = self.p
        x = 20
        if st["muted"]:
            x += p.chip(x, 92, "AUDIO MUTED", RED, RED_BG, RED_EDGE, dot=RED, tags=("chips",)) + 8
        elif st["streaming"]:
            x += p.chip(x, 92, "LIVE AUDIO", GREEN, GREEN_BG, GREEN_EDGE, dot=GREEN, tags=("chips",)) + 8
        p.chip(x, 92, "LOW-LATENCY", CYAN, CYAN_BG, CYAN_EDGE, tags=("chips",))

    def _draw_cards(self, st, mode):
        p, a = self.p, self.app
        live = mode == "live"
        # phone ip card
        lx = 34
        if live:
            p.oval(lx, 139, lx + 6, 145, fill=CYAN, tags=("cards",))
            lx += 12
        p.text(lx, 142, "Phone IP address", CYAN if live else DIM, (F_UI, 9, "bold" if live else "normal"), tags=("cards",))
        x = 376
        x -= p.chip(x, 142, "IPv4", CYAN, CYAN_BG, CYAN_EDGE, anchor="e", tags=("cards",), font=(F_MONO, 7, "bold")) + 6
        if live:
            p.chip(x, 142, "STREAMING", CYAN, CYAN_BG, CYAN_EDGE, anchor="e", tags=("cards",), font=(F_MONO, 7, "bold"))
        p.chip(568, 142, "TCP", CYAN, CYAN_BG, CYAN_EDGE, anchor="e", tags=("cards",), font=(F_MONO, 7, "bold"))
        for key, x1, x2 in (("ip", 30, 378), ("port", 410, 570)):
            col = CYAN_EDGE if (live or self.focus[key]) else EDGE_HI
            p.rect(x1, 158, x2, 194, fill=FIELD, outline=col, tags=("cards",))
        # globe icon
        gc = CYAN if live else "#2C8696"
        p.oval(42, 168, 56, 182, outline=gc, tags=("cards",))
        p.oval(46, 168, 52, 182, outline=gc, tags=("cards",))
        p.line([(42, 175), (56, 175)], gc, tags=("cards",))

    def _draw_btn(self, st, mode):
        p, on = self.p, st["active"]
        x, y, w, h, cut = 20, 218, 560, 46, 14
        shape = [(x, y), (x + w - cut, y), (x + w, y + cut), (x + w, y + h), (x + cut, y + h), (x, y + h - cut)]
        c1, c2 = ("#FF0A45", "#FF3B6B") if on else ("#00F0FF", "#00C8FF")
        n = 28
        for i in range(n):
            pts = clip_x(shape, x + w * i / n - (0.6 if i else 0), x + w * (i + 1) / n + 0.6)
            if len(pts) >= 3:
                p.poly(pts, fill=mix(c1, c2, i / (n - 1)), tags=("btn", "act_connect"))
        label = "DISCONNECT" if on else "CONNECT"
        fg = "#FFE6EC" if on else "#04121A"
        font = (F_DISP, 13, "bold")
        tw = p.tw(label, font, 2.5)
        gx = x + w / 2 - (14 + 12 + tw) / 2
        cy = y + h / 2
        if on:
            p.rect(gx, cy - 5, gx + 10, cy + 5, fill="#FFB3C5", tags=("btn", "act_connect"))
        else:
            p.poly([(gx, cy - 6), (gx + 10, cy), (gx, cy + 6)], fill=fg, tags=("btn", "act_connect"))
        p.text(gx + 26, cy, label, fg, font, tags=("btn", "act_connect"), track=2.5)

    def _check(self, y, checked, label, tag):
        p = self.p
        p.rect(34, y - 12, 300, y + 12, fill=PANEL, tags=("opts", tag))  # click area
        p.rect(34, y - 8, 50, y + 8, fill=CYAN if checked else FIELD, outline=CYAN if checked else EDGE_HI, tags=("opts", tag))
        if checked:
            p.line([(37, y), (41, y + 4), (48, y - 4)], "#04121A", width=2, tags=("opts", tag))
        p.text(62, y, label, TXT_SOFT, (F_UI, 10), tags=("opts", tag))

    def _draw_opts(self, st, mode):
        self._check(304, st["nc"], "Noise suppression", "act_nc")
        self._check(334, st["mon"], "Play on speaker", "act_mon")
        if mode == "live":
            p = self.p
            p.rect(318, 292, 566, 346, fill=FIELD, outline=CYAN_EDGE, tags=("opts",))
            p.oval(330, 311, 346, 327, outline=CYAN, tags=("opts",))
            p.text(338, 319, "i", CYAN, (F_MONO, 8, "bold"), anchor="c", tags=("opts",))
            p.text(358, 319, 'Choose "CABLE Output" as the microphone in your app or game', TXT_SOFT, (F_MONO, 8, "normal"),
                   anchor="w", tags=("opts",), wrap=196)

    def _draw_vol(self, st, mode):
        p = self.p
        if st["muted"]:
            p.chip(170, 376, "MUTED (0%)", RED, RED_BG, RED_EDGE, tags=("vol",), font=(F_MONO, 8, "bold"))
        else:
            p.chip(170, 376, f"Volume {st['pct']}%", CYAN, CYAN_BG, CYAN_EDGE, tags=("vol",), font=(F_MONO, 8, "bold"), track=0.4)
        p.text(566, 376, self._db_text(), DIM, (F_MONO, 8, "normal"), anchor="e", tags=("vol",))

    def _draw_mute(self, st, mode):
        p, muted = self.p, st["muted"]
        y1, y2 = 460, 496
        p.rect(36, y1, 294, y2, fill=RED_BG if muted else "#0A1626", outline=RED if muted else EDGE_HI,
               tags=("mute", "act_mute"))
        col = RED if muted else TXT_SOFT
        cy = (y1 + y2) / 2
        label = "MUTED (TAP TO UNMUTE)" if muted else "MUTE"
        font = (F_MONO, 9, "bold")
        gx = 165 - (22 + p.tw(label, font, 1.2)) / 2
        # speaker with a cross
        p.poly([(gx, cy - 3), (gx + 4, cy - 3), (gx + 9, cy - 7), (gx + 9, cy + 7), (gx + 4, cy + 3), (gx, cy + 3)],
               outline=col, tags=("mute", "act_mute"))
        p.line([(gx + 12, cy - 3), (gx + 18, cy + 3)], col, tags=("mute", "act_mute"))
        p.line([(gx + 18, cy - 3), (gx + 12, cy + 3)], col, tags=("mute", "act_mute"))
        p.text(gx + 26, cy, label, col, font, tags=("mute", "act_mute"), track=1.2)
        p.rect(306, y1, 564, y2, fill="#08111C", outline=EDGE, tags=("mute", "act_hotkey"))
        key = self.app.hotkey_label()
        kf = (F_MONO, 9, "bold")
        kw = max(22, p.tw(key, kf, 0.5) + 16)
        lab = "HOTKEY:"
        lw = p.tw(lab, (F_MONO, 9, "normal"), 1.2)
        gx = 435 - (lw + 10 + kw) / 2
        p.text(gx, cy, lab, DIM, (F_MONO, 9, "normal"), tags=("mute", "act_hotkey"), track=1.2)
        kx = gx + lw + 10
        p.rect(kx, cy - 10, kx + kw, cy + 10, fill=FIELD, outline=EDGE_HI, tags=("mute", "act_hotkey"))
        p.text(kx + kw / 2, cy, key, CYAN, kf, anchor="c", tags=("mute", "act_hotkey"), track=0.5)

    def _draw_foot(self, st, mode):
        p, a = self.p, self.app
        col = {"live": CYAN, "connecting": AMBER, "off": "#56677D"}[mode]
        p.oval(22, 541, 34, 553, fill=col if mode != "off" else "", outline="#56677D" if mode == "off" else "", tags=("foot",))
        if mode == "live":
            p.text(46, 547, f"Connected // Receiving stream ({a.ip.get().strip()}:{a.port.get()})", CYAN,
                   (F_MONO, 9, "bold"), anchor="w", tags=("foot",))
        else:
            msg, fnt = shorten(a.status, 92), (F_MONO, 9, "bold")
            colr = TXT_SOFT if mode == "connecting" else DIM
            if p.tw(msg, fnt) <= 330:  # fits on one line: centre it on the dot
                p.text(46, 547, msg, colr, fnt, anchor="w", tags=("foot",))
            else:
                p.text(46, 537, msg, colr, fnt, anchor="nw", tags=("foot",), wrap=330)
        ping = f"{a.ping_ms}" if (mode == "live" and a.ping_ms is not None) else "\u2014"
        pk = f"{a.pps}" if mode == "live" else "0"
        parts = [("PACKETS: ", DIM, "normal"), (pk, CYAN, "bold"), (" pkts/s", DIM, "normal"), ("   PING: ", DIM, "normal"),
                 (ping, CYAN, "bold"), (" ms", DIM, "normal")]
        f = lambda wt: (F_MONO, 8, wt)
        total = sum(p.tw(s, f(wt)) for s, _, wt in parts)
        x = 580 - total
        for s, colr, wt in parts:
            x += p.text(x, 547, s, colr, f(wt), tags=("foot",))


# ----------------------------------------------------------------- GUI
class App:
    def __init__(self, root):
        self.root = root
        root.title("UwU Mic Client")
        root.resizable(False, False)
        try:
            root.iconbitmap(resource("uwumic.ico"))
        except Exception:
            pass
        cfg = {}
        try:
            with open(CFG_PATH, encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            pass
        self.gain = max(0.0, min(1.0, float(cfg.get("gain", 1.0))))  # 0-100%
        self.muted = False
        self.level = 0.0
        self.shown = 0.0
        self.streaming = False
        self.active = False
        self.sink = None
        self.noise_cancel = bool(cfg.get("noise_cancel", True))
        self.nc_error = ""
        self.installing = False
        self.setup_tries = int(cfg.get("setup_tries", 0))
        self.monitor_device = None  # speaker test is always off when the app starts
        self.status = "Idle"
        self.stop = threading.Event()
        self.hotkey = cfg.get("hotkey", "ctrl+shift+m")
        self.hotkeys = HotkeyManager(lambda: self.root.after(0, self.toggle_mute))
        self.hotkey_ok = False

        self.ip = tk.StringVar(value=cfg.get("ip", ""))
        self.port = tk.StringVar(value=str(cfg.get("port", DEFAULT_PORT)))
        self.nc_var = tk.BooleanVar(value=self.noise_cancel)
        self.mon_var = tk.BooleanVar(value=False)
        self.devs = []
        self.devmap = {}
        self.load_devices()
        self.pkts = 0       # received audio packets (for the footer's pkts/s)
        self.pps = 0
        self._pk_n, self._pk_t = 0, time.monotonic()
        self.ping_ms = None  # time the phone took to accept the connection
        self.capturing = False
        self.ui = Ui(root, self)

        self.apply_hotkey(self.hotkey)
        root.bind("<KeyPress>", self._on_key, add="+")
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.poll()
        if self.needs_setup():
            root.after(800, self.first_run_setup)

    @property
    def slider(self):
        return self.ui.slider

    # -- devices (chosen automatically)
    def load_devices(self):
        try:
            self.devs = output_devices()
        except Exception as e:
            self.devs = []
            self.status = f"Audio system unavailable: {e}"
        self.devmap = {name: i for i, name in self.devs}
        if not any(is_virtual(n) for _, n in self.devs):
            self.status = "No virtual microphone found - it will be set up automatically"

    # -- virtual microphone install (fully automatic)
    def needs_setup(self):
        """True when there is no virtual microphone yet and the automatic setup has not failed 3 launches in a row."""
        return (sys.platform == "win32" and self.setup_tries < AUTO_SETUP_TRIES
                and not any(is_virtual(n) for n in self.devmap))

    def first_run_setup(self):
        """Runs by itself on launch: no question from this app (Windows still shows its own prompts)."""
        if self.installing or self.active or any(is_virtual(n) for n in self.devmap):
            return
        self.setup_tries += 1
        self.save()
        log(f"first-run setup, attempt {self.setup_tries} of {AUTO_SETUP_TRIES}")
        self.status = "Setting up your virtual microphone - allow the Windows prompts ..."
        self.install_cable()

    def install_cable(self):
        if self.installing:
            return
        if self.active:
            self.status = "Disconnect first, then the virtual microphone can be set up."
            return
        self.installing = True
        threading.Thread(target=self._install_worker, daemon=True).start()

    def _install_worker(self):
        try:
            log("install started")
            tmp = tempfile.mkdtemp(prefix="uwumic_")
            bundled = resource("vbcable.zip")
            if os.path.exists(bundled):
                zpath = bundled
                log("using the driver pack bundled in the app")
                self.status = "Preparing the virtual microphone driver ..."
            else:
                zpath, last = None, None
                for url in VB_URLS:
                    try:
                        log("downloading " + url)
                        self.status = "Downloading the virtual microphone driver ..."
                        zpath = download(url, os.path.join(tmp, "vbcable.zip"),
                                         lambda pct: setattr(self, "status",
                                                             f"Downloading the virtual microphone driver ... {pct}%"))
                        log(f"downloaded {os.path.getsize(zpath)} bytes")
                        break
                    except Exception as e:
                        last = e
                        log(f"download failed: {e!r}")
                if zpath is None:
                    raise last
            exe = extract_setup(zpath, os.path.join(tmp, "x"))
            log("installer: " + exe)
            self.status = "Checking the installer's digital signature ..."
            sig, signer = check_signature(exe)
            log(f"signature: {sig} / {signer}")
            if not (sig == "Valid" and ("vb-audio" in signer.lower() or "burel" in signer.lower())):
                self.status = ("Setup stopped: the installer's signature could not be verified "
                               f"({sig or 'unknown'}). Details: {log_path()}")
                return
            self.status = "Starting the installer - allow it in the Windows prompts ..."
            if not run_elevated(exe, "-i -h"):
                log("run_elevated returned failure (permission refused or blocked)")
                self.status = "The installer was not started (administrator permission was refused)."
                return
            log("installer started, waiting for CABLE Input to appear")
            self.status = "Installing - confirm the driver in the Windows window if it asks ..."
            seen = 0   # polls since CABLE Input first appeared
            for _ in range(90):
                time.sleep(2)
                try:
                    refresh_audio_devices()
                    devs = output_devices()
                    names = dict(devs)
                    cands = cable_candidates(devs)
                    good = any(api_rank(names[i]) < 9 for i in cands)
                except Exception as e:
                    log(f"device refresh failed: {e!r}")
                    cands, good = [], False
                if cands:
                    seen += 1
                    log(f"CABLE Input visible ({len(cands)} outputs, usable={good})")
                    # right after the install Windows can list only the kernel-streaming copy for a while:
                    # give the normal ones (WASAPI/MME/DirectSound) up to ~40 s to appear
                    if good or seen >= 20:
                        self.root.after(0, self.after_install)
                        return
            log("CABLE Input never appeared")
            self.status = ("The driver may need a restart: restart Windows once (VB-Cable asks for it), "
                           "then open UwU Mic again.")
        except Exception as e:
            log("install failed: " + traceback.format_exc())
            self.status = f"Install failed: {e}  (details: {log_path()})"
        finally:
            self.installing = False

    def after_install(self):
        self.setup_tries = 0
        self.save()
        self.load_devices()
        self.status = "Virtual microphone installed. In Discord or your game, choose CABLE Output as the microphone."

    # -- connection
    def set_status(self, text):
        self.status = text

    def toggle(self):
        if self.active:
            self.disconnect()
        else:
            self.connect()

    def connect(self):
        ip = self.ip.get().strip()
        try:
            port = int(self.port.get())
        except ValueError:
            messagebox.showerror("UwU Mic", "Port must be a number.")
            return
        if not ip:
            messagebox.showerror("UwU Mic", "Enter the phone's IP address.")
            return
        self.refresh_devices()
        devices = cable_candidates(self.devs)
        if not devices:
            self.status = "Setting up the virtual microphone first - press Connect again when it is ready."
            self.install_cable()
            return
        log("outputs: " + "; ".join(f"{i}={n}" for i, n in self.devs))
        log("virtual microphone outputs to try, best first: " + ", ".join(map(str, devices)))
        self.save()
        self.stop = threading.Event()
        self.lock(True)
        threading.Thread(target=self.worker, args=(ip, port, devices, self.stop), daemon=True).start()

    def refresh_devices(self):
        """Re-read the Windows audio devices (safe only while no audio stream is open)."""
        try:
            if not self.installing and self.sink is None:
                refresh_audio_devices()
        except Exception as e:
            log(f"device refresh failed: {e!r}")
        try:
            self.devs = output_devices()
            self.devmap = {name: i for i, name in self.devs}
        except Exception as e:
            log(f"could not list audio devices: {e!r}")
        return cable_candidates(self.devs)

    def worker(self, ip, port, devices, stop):
        first = True
        while not stop.is_set():
            if not first:
                devices = self.refresh_devices() or devices
            first = False
            stream(ip, port, stop, lambda rate: AudioSink(rate, devices, self), self.set_status,
                   on_connect=self.set_ping)
            if stop.wait(2):
                break

    def disconnect(self):
        self.stop.set()
        self.lock(False)
        self.streaming = False
        self.level = 0.0
        self.shown = 0.0
        self.ping_ms = None
        self.status = "Disconnected"

    def lock(self, on):
        self.active = on
        self.ui.set_locked(on)

    def set_ping(self, ms):
        self.ping_ms = ms

    # -- controls
    def on_vol(self, v):
        self.gain = max(0.0, min(1.0, float(v)))
        if self.gain > 0 and self.muted:
            self.set_muted(False)

    def on_noise(self):
        on = bool(self.nc_var.get())
        self.noise_cancel = on
        if on:
            self.nc_error = ""  # try again if it failed before
        self.save()

    def on_monitor(self):
        on = bool(self.mon_var.get())
        device = pick_speaker(self.devs, default_output_index()) if on else None
        if on and device is None:
            self.mon_var.set(False)
            self.status = "No speaker or headphones found for the test"
            return
        self.monitor_device = device
        worked = True
        if self.sink is not None:
            self.sink.set_monitor(device)
            worked = (not on) or self.sink.mon is not None
            if not worked:
                self.mon_var.set(False)
                self.monitor_device = None
        if worked:
            if on:
                self.status = SPEAKER_NOTE
            elif self.status == SPEAKER_NOTE:  # switched off: remove our own message, leave anything else alone
                self.status = "Connecting ..." if self.active else "Disconnected"
        self.save()

    def set_muted(self, m):
        self.muted = m

    def toggle_mute(self):
        self.set_muted(not self.muted)

    def hotkey_label(self):
        if self.capturing:
            return "PRESS KEYS..."
        hk = self.hotkey
        if not hk or not self.hotkey_ok:
            return "-" if not hk else "N/A"
        return "+".join(x.capitalize() if len(x) > 1 else x.upper() for x in hk.split("+"))

    def apply_hotkey(self, hk):
        previous = self.hotkey if self.hotkey_ok else None
        try:
            self.hotkeys.set(hk)
            self.hotkey, self.hotkey_ok = hk, True
        except Exception as e:
            self.hotkey_ok = False
            self.status = f"Hotkey '{hk}' could not be set: {e}"
            if previous and previous != hk:  # keep the old one working
                try:
                    self.hotkeys.set(previous)
                    self.hotkey, self.hotkey_ok = previous, True
                except Exception:
                    pass

    def capture_hotkey(self):
        if self.capturing:
            return
        self.capturing = True
        try:
            self.ui.c.focus_set()  # take the keyboard away from the IP / port boxes
        except Exception:
            pass
        self.root.after(8000, self._cancel_capture)  # give up if nothing is pressed

    def _cancel_capture(self):
        if self.capturing:
            self.capturing = False

    def _on_key(self, event):
        """While 'Hotkey' is waiting for a key combination, take the next normal key as the new hotkey."""
        if not self.capturing:
            return None
        vk = getattr(event, "keycode", 0)
        if vk in _MODIFIER_VKS:
            return "break"  # wait for the real key
        name = vk_to_name(vk)
        self.capturing = False
        if name is None or name == "esc":
            return "break"
        state = getattr(event, "state", 0)
        mods = [n for n, bit in (("ctrl", 0x4), ("alt", 0x20000), ("shift", 0x1)) if state & bit]
        self.apply_hotkey("+".join(mods + [name]))
        self.save()
        return "break"

    # -- housekeeping
    def poll(self):
        if self.nc_error and self.noise_cancel:  # the noise remover could not start: fall back to plain audio
            self.noise_cancel = False
            self.nc_var.set(False)
            self.status = f"Noise cancellation unavailable ({self.nc_error}) - sending plain audio"
        # the meter only shows sound while audio is really flowing and not muted; otherwise it falls to empty
        target = self.level if (self.streaming and not self.muted) else 0.0
        self.shown = max(target, self.shown * 0.8)
        if self.shown < 0.01:
            self.shown = 0.0
        now = time.monotonic()
        if now - self._pk_t >= 1.0:
            self.pps = round((self.pkts - self._pk_n) / (now - self._pk_t))
            self._pk_n, self._pk_t = self.pkts, now
        if not self.streaming:
            self.pps = 0
        self.ui.render()
        self.root.after(40, self.poll)

    def save(self):
        try:
            with open(CFG_PATH, "w", encoding="utf-8") as f:
                json.dump({"ip": self.ip.get().strip(), "port": self.port.get(), "setup_tries": self.setup_tries, "noise_cancel": self.noise_cancel,
                           "gain": self.gain, "hotkey": self.hotkey}, f)
        except Exception:
            pass

    def close(self):
        self.stop.set()
        self.hotkeys.stop()
        self.save()
        self.root.destroy()


def main():
    if "--selftest" in sys.argv:
        i = sys.argv.index("--selftest")
        path = sys.argv[i + 1] if len(sys.argv) > i + 1 else os.path.join(os.path.dirname(CFG_PATH), "UwUMicSelfTest.txt")
        sys.exit(run_selftest(path))
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # crisp text instead of a blurry upscaled window
        except Exception:
            pass
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
