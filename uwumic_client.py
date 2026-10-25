#!/usr/bin/env python3
"""UwU Mic client for Windows (WO Mic style).

Enter the phone's IP and port, press Connect, and the phone's microphone is played
into the output device you choose. Pick "CABLE Input" (VB-Cable) and then select
"CABLE Output" as the microphone in Discord, OBS, games, etc.
"""
import datetime
import json
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
from tkinter import ttk, messagebox

try:
    import keyboard  # global hotkeys (Windows)
except Exception:  # pragma: no cover
    keyboard = None

def resource(name):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


MAGIC = b"UWUM"
HEADER = struct.Struct(">4sIBB")  # magic, sample rate, channels, bits
DEFAULT_PORT = 8125

CYAN, RED, AMBER, GREY = "#00E5FF", "#FF0A45", "#F5B544", "#6B7280"
INK_ON_CYAN = "#04121A"
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


class _AudiolabStandIn(types.ModuleType):
    """Stand-in for the "audiolab" package. pyrnnoise imports it only for its audio-FILE helpers, which this
    app never uses, but inside the packaged exe the real one can crash while loading (missing template folder)."""

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return type(name, (), {})


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
        _sys.modules["audiolab"] = _AudiolabStandIn("audiolab")
        from pyrnnoise import RNNoise  # if this still fails, it is a different problem and is reported as such
        log(f"pyrnnoise loaded with a stand-in for audiolab (first error: {first!r})")
        return RNNoise


class NoiseCanceller:
    """RNNoise: a small neural network that removes background noise from a voice.
    Works on the 48 kHz mono 16-bit audio the phone sends, in chunks of any size."""

    FRAME = 480  # 10 ms at 48 kHz, what RNNoise processes at a time

    def __init__(self):
        import numpy as np
        RNNoise = load_rnnoise()

        self.np = np
        self.rn = RNNoise(sample_rate=48000)
        self.buf = bytearray()
        self.call = None  # how this version of pyrnnoise wants to be called, found on first use

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
        lines.append(f"call style: {getattr(nc.call, '__name__', 'detected')}; samples in {len(n)} out {len(o)}")
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


def stream(host, port, stop, make_sink, status):
    """Connect once and pump audio until the connection ends or `stop` is set."""
    sock = sink = None
    try:
        status(f"Connecting to {host}:{port} ...")
        sock = socket.create_connection((host, port), timeout=5)
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


# ----------------------------------------------------------------- merged meter + volume
class LevelSlider(tk.Canvas):
    """One bar: the bright fill is the live loudness, the hexagon handle is the volume (0-100%)."""

    PAD = 12

    def __init__(self, master, command, value=1.0, width=330, height=40, bg="#f0f0f0"):
        super().__init__(master, width=width, height=height, bg=bg, highlightthickness=0, cursor="hand2")
        self.command = command
        self.value = value
        self.level = 0.0
        self.muted = False
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
        span = w - 2 * self.PAD
        if span <= 0:
            return
        v = round(min(1.0, max(0.0, (e.x - self.PAD) / span)) * 100) / 100
        if v != self.value:
            self.value = v
            self.redraw()
            self.command(v)

    def redraw(self):
        self.delete("all")
        w, h = self._size()
        left, right, cy, th = self.PAD, w - self.PAD, h / 2, 8
        span = right - left
        self.create_rectangle(left, cy - th / 2, right, cy + th / 2, fill="#141C29", outline="#1F2A3B")
        if not self.muted and self.level > 0:
            col = RED if self.level > 0.9 else AMBER if self.level > 0.7 else CYAN
            self.create_rectangle(left, cy - th / 2, left + span * self.level, cy + th / 2, fill=col, outline="")
        x = left + span * self.value
        if not self.muted and self.value > 0:
            self.create_line(left, cy, x, cy, fill=CYAN, width=2)
        body = GREY if self.muted else CYAN
        shine = "#9AA1AC" if self.muted else "#B8F7FF"
        for hw, hh, col in ((9, 14, body), (4, 7, shine)):
            self.create_polygon(x, cy - hh, x + hw, cy - hh * 0.55, x + hw, cy + hh * 0.55,
                                x, cy + hh, x - hw, cy + hh * 0.55, x - hw, cy - hh * 0.55,
                                fill=col, outline="")


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
        self.hk_handle = None

        pad = {"padx": 10, "pady": 4}
        f = ttk.Frame(root, padding=10)
        f.grid()
        ttk.Label(f, text="Phone IP address").grid(row=0, column=0, sticky="w", **pad)
        ttk.Label(f, text="Port").grid(row=0, column=1, sticky="w", **pad)
        self.ip = tk.StringVar(value=cfg.get("ip", ""))
        self.port = tk.StringVar(value=str(cfg.get("port", DEFAULT_PORT)))
        self.ip_e = ttk.Entry(f, textvariable=self.ip, width=26)
        self.port_e = ttk.Entry(f, textvariable=self.port, width=8)
        self.ip_e.grid(row=1, column=0, **pad)
        self.port_e.grid(row=1, column=1, **pad)

        self.btn = tk.Button(f, text="Connect", command=self.toggle, bg=CYAN, fg=INK_ON_CYAN,
                             activebackground="#5CF1FF", activeforeground=INK_ON_CYAN, relief="flat", bd=0,
                             font=("Segoe UI", 11, "bold"), cursor="hand2", pady=8)
        self.btn.grid(row=2, column=0, columnspan=2, sticky="ew", **pad)

        self.nc_var = tk.BooleanVar(value=self.noise_cancel)
        self.nc_chk = ttk.Checkbutton(f, text="Noise cancellation", variable=self.nc_var, command=self.on_noise)
        self.nc_chk.grid(row=3, column=0, columnspan=2, sticky="w", **pad)
        self.mon_var = tk.BooleanVar(value=False)
        self.mon_chk = ttk.Checkbutton(f, text="Play on speaker (test) - use headphones to avoid echo",
                                       variable=self.mon_var, command=self.on_monitor)
        self.mon_chk.grid(row=4, column=0, columnspan=2, sticky="w", **pad)
        self.devs = []
        self.devmap = {}
        self.load_devices()

        self.vol_label = ttk.Label(f, text=f"Volume {round(self.gain * 100)}%")
        self.vol_label.grid(row=5, column=0, columnspan=2, sticky="w", **pad)
        try:
            bg = ttk.Style().lookup("TFrame", "background") or root.cget("bg")
        except Exception:
            bg = "#f0f0f0"
        self.slider = LevelSlider(f, self.on_vol, self.gain, bg=bg)
        self.slider.grid(row=6, column=0, columnspan=2, **pad)

        self.mute_btn = ttk.Button(f, text="Mute", command=self.toggle_mute)
        self.hk_btn = ttk.Button(f, command=self.capture_hotkey)
        self.mute_btn.grid(row=7, column=0, sticky="ew", **pad)
        self.hk_btn.grid(row=7, column=1, sticky="ew", **pad)

        self.status_label = ttk.Label(f, text="Idle", foreground="gray", wraplength=340)
        self.status_label.grid(row=8, column=0, columnspan=2, sticky="w", **pad)

        self.apply_hotkey(self.hotkey)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.poll()
        if self.needs_setup():
            root.after(800, self.first_run_setup)

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
            stream(ip, port, stop, lambda rate: AudioSink(rate, devices, self), self.set_status)
            if stop.wait(2):
                break

    def disconnect(self):
        self.stop.set()
        self.lock(False)
        self.streaming = False
        self.level = 0.0
        self.shown = 0.0
        self.status = "Disconnected"

    def lock(self, on):
        self.active = on
        if on:
            self.btn.config(text="Disconnect", bg=RED, fg="white", activebackground="#FF4D78", activeforeground="white")
        else:
            self.btn.config(text="Connect", bg=CYAN, fg=INK_ON_CYAN, activebackground="#5CF1FF",
                            activeforeground=INK_ON_CYAN)
        for w in (self.ip_e, self.port_e):
            w.config(state="disabled" if on else "normal")

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
        self.status = "Noise cancellation ON" if on else "Noise cancellation OFF"
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
                self.status = ("Speaker test ON - you can hear the received voice" if self.sink is not None
                               else "Speaker test is on - it will play once you connect")
            elif self.sink is not None:
                self.status = CONNECTED_TEXT
        self.save()

    def set_muted(self, m):
        self.muted = m
        self.mute_btn.config(text="Unmute" if m else "Mute")

    def toggle_mute(self):
        self.set_muted(not self.muted)

    def apply_hotkey(self, hk):
        self.hk_btn.config(text=f"Hotkey: {hk}" if keyboard else "Hotkey: unavailable")
        if not keyboard:
            return
        try:
            if self.hk_handle is not None:
                keyboard.remove_hotkey(self.hk_handle)
            self.hk_handle = keyboard.add_hotkey(hk, lambda: self.root.after(0, self.toggle_mute))
            self.hotkey = hk
        except Exception as e:
            self.hk_btn.config(text="Hotkey: invalid")
            self.status = f"Hotkey error: {e}"

    def capture_hotkey(self):
        if not keyboard:
            return
        self.hk_btn.config(text="Press keys...")

        def grab():
            try:
                hk = keyboard.read_hotkey(suppress=False)
            except Exception:
                hk = "esc"
            self.root.after(0, lambda: self.finish_capture(hk))

        threading.Thread(target=grab, daemon=True).start()

    def finish_capture(self, hk):
        self.apply_hotkey(self.hotkey if hk == "esc" else hk)
        self.save()

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
        self.slider.set_state(0.0 if self.muted else self.gain, self.shown, self.muted)
        txt = "Volume 0% (muted)" if self.muted else f"Volume {round(self.gain * 100)}%"
        if self.vol_label.cget("text") != txt:
            self.vol_label.config(text=txt)
        self.status_label.config(text=self.status)
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
        self.save()
        self.root.destroy()


def main():
    if "--selftest" in sys.argv:
        i = sys.argv.index("--selftest")
        path = sys.argv[i + 1] if len(sys.argv) > i + 1 else os.path.join(os.path.dirname(CFG_PATH), "UwUMicSelfTest.txt")
        sys.exit(run_selftest(path))
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
