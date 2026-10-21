#!/usr/bin/env python3
"""UwU Mic client for Windows (WO Mic style).

Enter the phone's IP and port, press Connect, and the phone's microphone is played
into the output device you choose. Pick "CABLE Input" (VB-Cable) and then select
"CABLE Output" as the microphone in Discord, OBS, games, etc.
"""
import json
import os
import socket
import sys
import struct
import threading
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


class AudioSink:
    """Plays PCM16 mono into a Windows output device, with volume/mute and a level meter."""

    def __init__(self, rate, device, app):
        import numpy as np
        import sounddevice as sd

        self.jit = Jitter(rate)
        self.channels = 2

        def callback(outdata, frames, time_info, status):
            raw = self.jit.pull(frames * 2)
            a = np.frombuffer(raw, dtype="<i2").astype(np.float32)
            g = 0.0 if app.muted else app.gain
            a = np.clip(a * g, -32768, 32767)
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
        last = None
        for ch in (2, 1):
            try:
                self.channels = ch
                self.stream = sd.OutputStream(samplerate=rate, device=device, channels=ch, dtype="int16",
                                              callback=callback, blocksize=0, latency="low",
                                              extra_settings=extra)
                self.stream.start()
                break
            except Exception as e:  # try mono next
                last = e
        else:
            raise last

    def push(self, data):
        self.jit.push(data)

    def close(self):
        try:
            self.stream.stop()
            self.stream.close()
        except Exception:
            pass


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
        status("Connected - streaming")
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


def output_devices():
    import sounddevice as sd

    apis = sd.query_hostapis()
    out = []
    for i, d in enumerate(sd.query_devices()):
        if d["max_output_channels"] > 0:
            out.append((i, f"{d['name']}  [{apis[d['hostapi']]['name']}]"))
    return out


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
        self.gain = float(cfg.get("gain", 1.0))
        self.muted = False
        self.level = 0.0
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

        self.con = ttk.Button(f, text="Connect", command=self.connect)
        self.dis = ttk.Button(f, text="Disconnect", command=self.disconnect, state="disabled")
        self.con.grid(row=2, column=0, sticky="ew", **pad)
        self.dis.grid(row=2, column=1, sticky="ew", **pad)

        ttk.Label(f, text="Output device (choose CABLE Input for a virtual mic)").grid(
            row=3, column=0, columnspan=2, sticky="w", **pad)
        self.dev = ttk.Combobox(f, state="readonly", width=48)
        self.dev.grid(row=4, column=0, columnspan=2, **pad)
        self.devmap = {}
        self.load_devices(cfg.get("device"))

        self.vol_label = ttk.Label(f, text=f"Volume {int(self.gain * 100)}%")
        self.vol_label.grid(row=5, column=0, columnspan=2, sticky="w", **pad)
        self.vol = ttk.Scale(f, from_=0, to=3, value=self.gain, command=self.on_vol, length=330)
        self.vol.grid(row=6, column=0, columnspan=2, **pad)

        self.meter = ttk.Progressbar(f, maximum=100, length=330)
        self.meter.grid(row=7, column=0, columnspan=2, **pad)

        self.mute_btn = ttk.Button(f, text="Mute", command=self.toggle_mute)
        self.hk_btn = ttk.Button(f, command=self.capture_hotkey)
        self.mute_btn.grid(row=8, column=0, sticky="ew", **pad)
        self.hk_btn.grid(row=8, column=1, sticky="ew", **pad)

        self.status_label = ttk.Label(f, text="Idle", foreground="gray", wraplength=340)
        self.status_label.grid(row=9, column=0, columnspan=2, sticky="w", **pad)

        self.apply_hotkey(self.hotkey)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.poll()

    # -- devices
    def load_devices(self, saved):
        try:
            devs = output_devices()
        except Exception as e:
            messagebox.showerror("UwU Mic", f"Audio system unavailable: {e}")
            devs = []
        self.devmap = {name: i for i, name in devs}
        names = [n for _, n in devs]
        self.dev["values"] = names
        pick = saved if saved in self.devmap else None
        if not pick:
            cable = [n for n in names if "CABLE Input" in n]
            cable.sort(key=lambda n: "WASAPI" not in n)
            pick = cable[0] if cable else (names[0] if names else "")
        self.dev.set(pick)

    # -- connection
    def set_status(self, text):
        self.status = text

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
        if self.dev.get() not in self.devmap:
            messagebox.showerror("UwU Mic", "Choose an output device.")
            return
        self.save()
        self.stop = threading.Event()
        self.lock(True)
        threading.Thread(target=self.worker, args=(ip, port, self.devmap[self.dev.get()], self.stop),
                         daemon=True).start()

    def worker(self, ip, port, device, stop):
        while not stop.is_set():
            stream(ip, port, stop, lambda rate: AudioSink(rate, device, self), self.set_status)
            if stop.wait(2):
                break

    def disconnect(self):
        self.stop.set()
        self.lock(False)
        self.level = 0.0
        self.status = "Disconnected"

    def lock(self, on):
        self.con.config(state="disabled" if on else "normal")
        self.dis.config(state="normal" if on else "disabled")
        for w in (self.ip_e, self.port_e):
            w.config(state="disabled" if on else "normal")
        self.dev.config(state="disabled" if on else "readonly")

    # -- controls
    def on_vol(self, v):
        self.gain = float(v)
        self.vol_label.config(text=f"Volume {int(self.gain * 100)}%")

    def toggle_mute(self):
        self.muted = not self.muted
        self.mute_btn.config(text="Unmute" if self.muted else "Mute")

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
        self.meter["value"] = min(100, self.level * 100)
        self.status_label.config(text=self.status)
        self.root.after(60, self.poll)

    def save(self):
        try:
            with open(CFG_PATH, "w", encoding="utf-8") as f:
                json.dump({"ip": self.ip.get().strip(), "port": self.port.get(), "device": self.dev.get(),
                           "gain": self.gain, "hotkey": self.hotkey}, f)
        except Exception:
            pass

    def close(self):
        self.stop.set()
        self.save()
        self.root.destroy()


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
