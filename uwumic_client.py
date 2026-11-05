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
for _i in range(10):
    _VK[f"num{_i}"] = 0x60 + _i
_VK.update({"nummul": 0x6A, "numadd": 0x6B, "numsub": 0x6D, "numdec": 0x6E, "numdiv": 0x6F,
            "pause": 0x13, "printscreen": 0x2C, "menu": 0x5D})
_CANON += [f"num{i}" for i in range(10)] + ["nummul", "numadd", "numsub", "numdec", "numdiv", "pause", "printscreen", "menu"]
_NAME_OF = {_VK[n]: n for n in reversed(_CANON)}
_PRETTY = {"nummul": "Num *", "numadd": "Num +", "numsub": "Num -", "numdec": "Num .", "numdiv": "Num /",
           "printscreen": "PrtScn", "pageup": "PgUp", "pagedown": "PgDn", "backspace": "Backspace"}
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


# ----------------------------------------------------------------- tray icon (Windows notification area)
import ctypes as _ct

WM_APP_TRAY = 0x8001
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 1, 2, 4
WM_LBUTTONUP, WM_LBUTTONDBLCLK, WM_RBUTTONUP = 0x0202, 0x0203, 0x0205
MF_STRING, MF_GRAYED, MF_SEPARATOR = 0x0, 0x1, 0x800
TPM_RIGHTBUTTON, TPM_RETURNCMD = 0x2, 0x100


class NOTIFYICONDATA(_ct.Structure):
    """NOTIFYICONDATAW with explicit sizes (so its layout is the same wherever it is built)."""
    _fields_ = [("cbSize", _ct.c_uint32), ("hWnd", _ct.c_void_p), ("uID", _ct.c_uint32), ("uFlags", _ct.c_uint32),
                ("uCallbackMessage", _ct.c_uint32), ("hIcon", _ct.c_void_p), ("szTip", _ct.c_uint16 * 128),
                ("dwState", _ct.c_uint32), ("dwStateMask", _ct.c_uint32), ("szInfo", _ct.c_uint16 * 256),
                ("uTimeout", _ct.c_uint32), ("szInfoTitle", _ct.c_uint16 * 64), ("dwInfoFlags", _ct.c_uint32),
                ("guidItem", _ct.c_ubyte * 16), ("hBalloonIcon", _ct.c_void_p)]


class WNDCLASS(_ct.Structure):
    _fields_ = [("style", _ct.c_uint32), ("lpfnWndProc", _ct.c_void_p), ("cbClsExtra", _ct.c_int),
                ("cbWndExtra", _ct.c_int), ("hInstance", _ct.c_void_p), ("hIcon", _ct.c_void_p),
                ("hCursor", _ct.c_void_p), ("hbrBackground", _ct.c_void_p), ("lpszMenuName", _ct.c_wchar_p),
                ("lpszClassName", _ct.c_wchar_p)]


def put_wide(array, text):
    """Fill a fixed-size UTF-16 array (a Windows WCHAR buffer) with text; the rest is zeros, so it always ends in a zero."""
    raw = text.encode("utf-16-le")
    units = [raw[i] | (raw[i + 1] << 8) for i in range(0, len(raw), 2)][: len(array) - 1]
    units += [0] * (len(array) - len(units))
    for i, u in enumerate(units):
        array[i] = u


class _WinTray:
    """The Windows calls behind the tray icon: a hidden window that receives the icon's mouse clicks."""

    def __init__(self):
        from ctypes import wintypes
        ct = _ct
        self.wt = wintypes
        self.u = ct.WinDLL("user32", use_last_error=True)
        self.s = ct.WinDLL("shell32", use_last_error=True)
        self.k = ct.WinDLL("kernel32", use_last_error=True)
        LRESULT = ct.c_ssize_t
        self.WNDPROC = ct.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        u, k, s = self.u, self.k, self.s
        k.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        k.GetModuleHandleW.restype = ct.c_void_p
        u.RegisterClassW.argtypes = [ct.POINTER(WNDCLASS)]
        u.RegisterClassW.restype = wintypes.ATOM
        u.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                      ct.c_int, ct.c_int, ct.c_int, ct.c_int, ct.c_void_p, ct.c_void_p,
                                      ct.c_void_p, ct.c_void_p]
        u.CreateWindowExW.restype = ct.c_void_p
        u.DefWindowProcW.argtypes = [ct.c_void_p, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        u.DefWindowProcW.restype = LRESULT
        u.GetMessageW.argtypes = [ct.POINTER(wintypes.MSG), ct.c_void_p, wintypes.UINT, wintypes.UINT]
        u.GetMessageW.restype = ct.c_int
        u.TranslateMessage.argtypes = [ct.POINTER(wintypes.MSG)]
        u.DispatchMessageW.argtypes = [ct.POINTER(wintypes.MSG)]
        u.DispatchMessageW.restype = LRESULT
        u.PostQuitMessage.argtypes = [ct.c_int]
        u.PostMessageW.argtypes = [ct.c_void_p, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        u.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
        u.RegisterWindowMessageW.restype = wintypes.UINT
        u.LoadImageW.argtypes = [ct.c_void_p, wintypes.LPCWSTR, wintypes.UINT, ct.c_int, ct.c_int, wintypes.UINT]
        u.LoadImageW.restype = ct.c_void_p
        u.GetSystemMetrics.argtypes = [ct.c_int]
        u.CreatePopupMenu.restype = ct.c_void_p
        u.AppendMenuW.argtypes = [ct.c_void_p, wintypes.UINT, ct.c_size_t, wintypes.LPCWSTR]
        u.TrackPopupMenu.argtypes = [ct.c_void_p, wintypes.UINT, ct.c_int, ct.c_int, ct.c_int, ct.c_void_p, ct.c_void_p]
        u.TrackPopupMenu.restype = ct.c_int
        u.DestroyMenu.argtypes = [ct.c_void_p]
        u.GetCursorPos.argtypes = [ct.POINTER(wintypes.POINT)]
        u.SetForegroundWindow.argtypes = [ct.c_void_p]
        s.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ct.POINTER(NOTIFYICONDATA)]
        s.Shell_NotifyIconW.restype = wintypes.BOOL
        self.hwnd = self.hicon = None
        self.tip = ""
        self._proc = None

    def create(self, icon_path, tip, on_event):
        ct, u = _ct, self.u
        self.on_event, self.tip = on_event, tip
        self._proc = self.WNDPROC(self._wndproc)  # kept on self so it is not garbage collected
        hinst = self.k.GetModuleHandleW(None)
        cls = WNDCLASS()
        cls.lpfnWndProc = ct.cast(self._proc, ct.c_void_p).value
        cls.hInstance = hinst
        cls.lpszClassName = "UwUMicTrayWindow"
        if not u.RegisterClassW(ct.byref(cls)) and ct.get_last_error() != 1410:  # 1410 = class already exists
            raise OSError(f"RegisterClass failed ({ct.get_last_error()})")
        self.hwnd = u.CreateWindowExW(0, "UwUMicTrayWindow", "UwU Mic", 0, 0, 0, 0, 0, -3, None, hinst, None)  # -3 = message-only
        if not self.hwnd:
            raise OSError(f"CreateWindow failed ({ct.get_last_error()})")
        self.taskbar_msg = u.RegisterWindowMessageW("TaskbarCreated")  # sent again if Explorer restarts
        size = u.GetSystemMetrics(49) or 16  # SM_CXSMICON
        self.hicon = u.LoadImageW(None, icon_path, 1, size, size, 0x10)  # IMAGE_ICON, LR_LOADFROMFILE
        if not self.hicon:
            raise OSError(f"could not load the icon ({ct.get_last_error()})")
        if not self._notify(NIM_ADD):
            raise OSError("Shell_NotifyIcon refused to add the icon")

    def _data(self):
        nid = NOTIFYICONDATA()
        nid.cbSize = _ct.sizeof(NOTIFYICONDATA)
        nid.hWnd = self.hwnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_APP_TRAY
        nid.hIcon = self.hicon
        put_wide(nid.szTip, self.tip)
        return nid

    def _notify(self, message):
        return bool(self.s.Shell_NotifyIconW(message, _ct.byref(self._data())))

    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_APP_TRAY:
                ev = lparam & 0xFFFF
                if ev in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                    self.on_event("left")
                elif ev == WM_RBUTTONUP:
                    self.on_event("right")
                return 0
            if msg == self.taskbar_msg and self.taskbar_msg:
                self._notify(NIM_ADD)
                return 0
            if msg == 0x0002:  # WM_DESTROY
                self._notify(NIM_DELETE)
                self.u.PostQuitMessage(0)
                return 0
        except Exception:
            log("tray: " + traceback.format_exc())
        return self.u.DefWindowProcW(hwnd, msg, wparam, lparam)

    def popup(self, items):
        """items = [(id, text, enabled)], id 0 = separator. Shows the menu at the mouse; returns the chosen id or 0."""
        u, wt = self.u, self.wt
        menu = u.CreatePopupMenu()
        try:
            for ident, text, enabled in items:
                if ident == 0:
                    u.AppendMenuW(menu, MF_SEPARATOR, 0, None)
                else:
                    u.AppendMenuW(menu, MF_STRING | (0 if enabled else MF_GRAYED), ident, text)
            pt = wt.POINT()
            u.GetCursorPos(_ct.byref(pt))
            u.SetForegroundWindow(self.hwnd)  # without this the menu would not close when you click elsewhere
            cmd = u.TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_RETURNCMD, pt.x, pt.y, 0, self.hwnd, None)
            u.PostMessageW(self.hwnd, 0, 0, 0)  # WM_NULL
            return cmd
        finally:
            u.DestroyMenu(menu)

    def loop(self):
        msg = self.wt.MSG()
        while self.u.GetMessageW(_ct.byref(msg), None, 0, 0) > 0:
            self.u.TranslateMessage(_ct.byref(msg))
            self.u.DispatchMessageW(_ct.byref(msg))

    def update_tip(self, text):
        self.tip = text
        self._notify(NIM_MODIFY)

    def remove(self):
        if self.hwnd:
            self._notify(NIM_DELETE)  # take the icon away right now so no ghost icon is left behind

    def stop(self):
        if self.hwnd:
            self.u.PostMessageW(self.hwnd, 0x0010, 0, 0)  # WM_CLOSE -> the hidden window is destroyed


class TrayIcon:
    """The icon by the clock. Left click shows the window; right click: Connect, Disconnect, Mute/Unmute, Exit."""

    def __init__(self, app, api=None):
        self.app, self.api = app, api
        self.ok = False
        self.thread = None
        self.tip = ""

    def start(self, icon_path):
        try:
            if self.api is None:
                self.api = _WinTray()
        except Exception:
            log("tray unavailable: " + traceback.format_exc())
            return False
        ready, res = threading.Event(), {}

        def run():
            try:
                self.api.create(icon_path, "UwU Mic", self._event)
                res["ok"] = True
            except Exception:
                res["ok"] = False
                log("tray could not be created: " + traceback.format_exc())
            ready.set()
            if res["ok"]:
                try:
                    self.api.loop()
                except Exception:
                    log("tray loop: " + traceback.format_exc())
                self.ok = False

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        ready.wait(3)
        self.ok = bool(res.get("ok"))
        return self.ok

    def menu(self):
        a = self.app
        return [(1, "Connect", not a.active), (2, "Disconnect", bool(a.active)),
                (3, "Unmute" if a.muted else "Mute", True), (0, "", False), (4, "Exit", True)]

    def _event(self, kind):  # runs on the tray thread, so hand real work to the window's thread
        a = self.app
        if kind == "left":
            a.root.after(0, a.show_window)
        elif kind == "right":
            choice = self.api.popup(self.menu())
            action = {1: a.tray_connect, 2: a.tray_disconnect, 3: a.toggle_mute, 4: a.quit_app}.get(choice)
            if action:
                a.root.after(0, action)

    def set_tip(self, text):
        if self.ok and text != self.tip:
            self.tip = text
            try:
                self.api.update_tip(text)
            except Exception:
                pass

    def stop(self):
        if self.ok:
            try:
                self.api.remove()
                self.api.stop()
            except Exception:
                pass
        self.ok = False


def already_running():
    """True if another UwU Mic Client is already running (so closing to the tray never leaves two copies)."""
    if sys.platform != "win32":
        return False
    try:
        k = _ct.WinDLL("kernel32", use_last_error=True)
        k.CreateMutexW.argtypes = [_ct.c_void_p, _ct.c_int, _ct.c_wchar_p]
        k.CreateMutexW.restype = _ct.c_void_p
        handle = k.CreateMutexW(None, 0, "Local\\UwUMicClientSingleInstance")
        already = _ct.get_last_error() == 183  # ERROR_ALREADY_EXISTS
        already_running.handle = handle  # keep it open for as long as we run
        return already
    except Exception:
        return False


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


# ----------------------------------------------------------------- look: 90s audio-rack, dark and light themes
APP_VERSION = "1.1.0"
BITRATE_KBPS = 768  # 48 kHz x 16 bit x mono
F_UI, F_MONO, F_DISP = "Segoe UI", "Consolas", "Bahnschrift"

DARK = dict(
    name="dark", hard=False, glow=True,
    desk="#05080c", grid="#0b1620", chassis="#0c121b", edge="#1c293c", edge_hi="#2a3a52", trim="#17202d", trim_edge="#243044",
    screw="#354359", head="#0a0f17", panel="#0c131e", panel_edge="#1a2638", card="#0a0f16", card_edge="#1b2738",
    txt="#e2e8f0", txt2="#94a3b8", dim="#64748b", dimmer="#475569", accent="#00f0ff", green="#00ff88", coral="#ff3366",
    amber="#f59e0b", chip_bg="#002b36", chip_edge="#00808c", chip_fg="#00f0ff",
    green_bg="#081f14", green_edge="#0a8a50", coral_bg="#2a0714", coral_edge="#a3153f", amber_bg="#1c1606", amber_edge="#7a5410",
    lcd_ip="#040e0c", lcd_ip_edge="#0b4a35", lcd_port="#040c14", lcd_port_edge="#0b3d4a", scan="#02080a",
    lcd_ip_off="#00f2d2", lcd_ip_on="#00ff88", lcd_port_fg="#00f0ff", lcd_tag="#64748b",
    btn_on="#00f2d2", btn_on2="#00c9b0", btn_on_text="#04121a", btn_on_shadow="#02060a",
    btn_off="#ff4d7a", btn_off2="#ff265c", btn_off_text="#ffffff", btn_off_lip="#991336",
    field="#080d14", check_off="#060b12", callout="#080d14", callout_edge="#203147",
    track1="#515c6b", track2="#2f3844", track3="#1c222b", track_edge="#2b3542", cap_edge="#050b12",
    mute_bg="#0b131e", mute_edge="#203147", mute_text="#e2e8f0", kbd_bg="#002633", kbd_edge="#00808c",
    foot="#060a10", foot_edge="#141d2b", seg_off="#101820",
)
LIGHT = dict(
    name="light", hard=True, glow=False,
    desk="#d9d4c3", grid="#e8e4d6", chassis="#f7f5ee", edge="#1c2635", edge_hi="#1c2635", trim="#e5e1d3", trim_edge="#1c2635",
    screw="#8c96a3", head="#f7f5ee", panel="#ffffff", panel_edge="#1c2635", card="#ffffff", card_edge="#1c2635",
    txt="#111827", txt2="#4b5563", dim="#6b7280", dimmer="#9ca3af", accent="#0e7490", green="#10b981", coral="#ff2e63",
    amber="#b45309", chip_bg="#ccfbf1", chip_edge="#0f766e", chip_fg="#0f766e",
    green_bg="#d1fae5", green_edge="#047857", coral_bg="#ffe4e6", coral_edge="#ff2e63", amber_bg="#fef3c7", amber_edge="#b45309",
    lcd_ip="#dbeadf", lcd_ip_edge="#1c2635", lcd_port="#dbeadf", lcd_port_edge="#1c2635", scan="#dbeadf",
    lcd_ip_off="#064e3b", lcd_ip_on="#064e3b", lcd_port_fg="#064e3b", lcd_tag="#4b5563",
    btn_on="#00d2b4", btn_on2="#00d2b4", btn_on_text="#041215", btn_on_shadow="#1c2635",
    btn_off="#ff2e63", btn_off2="#ff2e63", btn_off_text="#ffffff", btn_off_lip="#1c2635",
    field="#f4f5f2", check_off="#f4f5f2", callout="#ede8d8", callout_edge="#1c2635",
    track1="#c9c4b0", track2="#d8d3c0", track3="#b7b29e", track_edge="#1c2635", cap_edge="#1c2635",
    mute_bg="#ede8d8", mute_edge="#1c2635", mute_text="#111827", kbd_bg="#ccfbf1", kbd_edge="#0f766e",
    foot="#ede8d8", foot_edge="#1c2635", seg_off="#bdb7a2",
)
THEMES = {"dark": DARK, "light": LIGHT}


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


# ----------------------------------------------------------------- the fader (volume)
class LevelSlider(tk.Canvas):
    """Hardware-style fader: metal track, coloured fill up to the volume, rectangular cap with a notch (0-100%)."""

    PAD = 14

    def __init__(self, master, command, value=1.0, width=330, height=40, bg="#0c131e", scale=1.0, theme=None):
        super().__init__(master, width=width, height=height, bg=bg, highlightthickness=0, cursor="hand2")
        self.command = command
        self.value = value
        self.level = 0.0
        self.muted = False
        self.k = scale
        self.t = theme or DARK
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

    def set_theme(self, theme, bg):
        self.t = theme
        self.config(bg=bg)
        self.redraw()

    def set_state(self, value, level, muted):
        # the loudness (level) is shown by the segmented meter above, so only the volume and mute change the picture
        if (value, muted) != (self.value, self.muted):
            self.value, self.muted = value, muted
            self.redraw()
        self.level = level

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
        k, t = self.k, self.t
        w, h = self._size()
        left, right, cy = self.PAD * k, w - self.PAD * k, h / 2
        span = right - left
        th = 10 * k
        top = cy - th / 2
        for i, col in enumerate((t["track1"], t["track2"], t["track3"])):  # metal groove: three bands
            self.create_rectangle(left, top + th * i / 3, right, top + th * (i + 1) / 3, fill=col, outline="")
        self.create_rectangle(left, top, right, cy + th / 2, fill="", outline=t["track_edge"], width=max(1, round(k)))
        x = left + span * self.value
        if self.value > 0:  # coloured fill up to the volume
            for i in range(3):
                self.create_rectangle(left, top + 1 + (th - 2) * i / 3, x, top + 1 + (th - 2) * (i + 1) / 3,
                                      fill=mix(t["green"], "#000000", 0.22 * i), outline="")
        cw, ch, sh = 7 * k, 12 * k, 2 * k  # cap with hard shadow and a notch
        self.create_rectangle(x - cw + sh, cy - ch + sh, x + cw + sh, cy + ch + sh, fill=t["cap_edge"], outline="")
        self.create_rectangle(x - cw, cy - ch, x + cw, cy + ch, fill=t["green"], outline=t["cap_edge"], width=max(1, round(2 * k)))
        self.create_rectangle(x - 4 * k, cy - k, x + 4 * k, cy + k, fill=t["cap_edge"], outline="")


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

    def fit(self, s, font, max_w):
        """The text, cut with an ellipsis if it would be wider than max_w (logical units)."""
        if self.tw(s, font) <= max_w:
            return s
        while len(s) > 1 and self.tw(s + "\u2026", font) > max_w:
            s = s[:-1]
        return s.rstrip() + "\u2026"

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

    def chip(self, x, cy, s, fg, bg, edge, anchor="w", dot=None, tags=(), font=(F_MONO, 8, "bold"), track=0.8, h=20):
        """Small label box. x is its left edge (anchor 'w') or right edge ('e'). Returns its width."""
        pad = 8
        tw = self.tw(s, font, track)
        w = tw + 2 * pad + (11 if dot else 0)
        x0 = x - w if anchor == "e" else x
        self.rect(x0, cy - h / 2, x0 + w, cy + h / 2, fill=bg, outline=edge, tags=tags)
        tx = x0 + pad
        if dot:
            self.oval(tx, cy - 3, tx + 6, cy + 3, fill=dot, tags=tags)
            tx += 11
        self.text(tx, cy, s, fg, font, tags=tags, track=track)
        return w


class Ui:
    """The window: a 90s audio-rack receiver (dark or light) drawn on one canvas, driven by the state in App."""

    W, H = 620, 594
    LAYERS = ("hdr", "chips", "cards", "btn", "opts", "vol", "mute", "foot")

    def __init__(self, root, app):
        import tkinter.font as tkfont

        self.root, self.app = root, app
        self.name = app.theme if app.theme in THEMES else "dark"
        self.t = THEMES[self.name]
        self.S = max(1.0, float(root.winfo_fpixels("1i")) / 96.0)
        S = self.S
        self._fonts = {}

        def measure(font, text):
            f = self._fonts.get(font)
            if f is None:
                weight = font[2] if len(font) > 2 else "normal"
                f = self._fonts[font] = tkfont.Font(root=root, family=font[0], size=font[1], weight=weight)
            return f.measure(text)

        root.geometry(f"{round(self.W * S)}x{round(self.H * S)}")
        self.c = tk.Canvas(root, width=round(self.W * S), height=round(self.H * S), bg=self.t["desk"], highlightthickness=0, bd=0)
        root.configure(bg=self.t["desk"])
        self.c.pack()
        self.p = Painter(self.c, S, measure)
        self.keys = {}
        self.focus = {"ip": False, "port": False}
        self.blink = False
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
            dark = 1 if self.name == "dark" else 0
            for attr in (20, 19):  # dark title bar (Windows 10/11)
                ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(ctypes.c_int(dark)), 4)
            col = self.t["trim"]
            rgb = int(col[5:7], 16) << 16 | int(col[3:5], 16) << 8 | int(col[1:3], 16)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(ctypes.c_int(rgb)), 4)  # Windows 11
        except Exception:
            pass

    def set_theme(self, name):
        if name not in THEMES or name == self.name:
            return
        self.name, self.t = name, THEMES[name]
        for tag in ("static",) + self.LAYERS:
            self.c.delete(tag)
        self.c.config(bg=self.t["desk"])
        self.root.configure(bg=self.t["desk"])
        self.keys.clear()
        self._static()
        self.slider.set_theme(self.t, self.t["panel"])
        self.dark_title_bar()
        self._style_entries()
        self.c.tag_raise("win")  # the entries and the fader must stay on top of the freshly drawn shapes

    def panel(self, x1, y1, x2, y2, fill, edge, tags, off=3):
        """A rectangle with this theme's edge: thin line (dark) or thick outline + hard offset shadow (light)."""
        p, t = self.p, self.t
        if t["hard"]:
            p.rect(x1 + off, y1 + off, x2 + off, y2 + off, fill=t["edge"], tags=tags)
            p.rect(x1, y1, x2, y2, fill=fill, outline=edge, width=2, tags=tags)
        else:
            p.rect(x1, y1, x2, y2, fill=fill, outline=edge, tags=tags)

    def glow(self, x, y, s, fg, font, glow_col, anchor="w", tags=(), track=0):
        p = self.p
        if self.t["glow"]:
            for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                p.text(x + dx, y + dy, s, glow_col, font, anchor, tags, track)
        return p.text(x, y, s, fg, font, anchor, tags, track)

    # -- fixed parts
    def _static(self):
        p, t, W, H = self.p, self.t, self.W, self.H
        ST = ("static",)
        p.rect(0, 0, W, H, fill=t["chassis"], tags=ST)
        for x in range(0, W + 1, 24):
            p.line([(x, 10), (x, H)], t["grid"], tags=ST)
        for y in range(10, H + 1, 24):
            p.line([(0, y), (W, y)], t["grid"], tags=ST)
        # top trim with two screws
        p.rect(0, 0, W, 9, fill=t["trim"], tags=ST)
        p.line([(0, 9), (W, 9)], t["trim_edge"], width=2 if t["hard"] else 1, tags=ST)
        for sx in (16, W - 16):
            p.oval(sx - 3, 1.5, sx + 3, 7.5, fill=t["screw"], outline=t["edge"] if t["hard"] else "", tags=ST)
        # header
        p.rect(0, 10, W, 60, fill=t["head"], tags=ST)
        p.line([(0, 60), (W, 60)], t["edge"], width=2 if t["hard"] else 1, tags=ST)
        cx, cy = 36, 35  # logo: diamond
        if t["hard"]:
            p.poly([(cx + 1, cy - 11), (cx + 12, cy), (cx + 1, cy + 11), (cx - 10, cy)], fill=t["edge"], tags=ST)
        p.poly([(cx, cy - 11), (cx + 11, cy), (cx, cy + 11), (cx - 11, cy)], fill="#00c49f" if t["hard"] else t["head"],
               outline=t["edge"] if t["hard"] else t["accent"], width=2, tags=ST)
        p.poly([(cx, cy - 5), (cx + 5, cy), (cx, cy + 5), (cx - 5, cy)], fill=t["edge"] if t["hard"] else t["accent"], tags=ST)
        x = 56
        x += self.glow(x, 35, "UwU", t["txt"], (F_DISP, 15, "bold"), t["txt"], tags=ST) + 5
        x += p.text(x, 35, "Mic", t["coral"], (F_DISP, 15, "bold"), tags=ST) + 10
        x += p.text(x, 36, "// PC CLIENT", t["accent"], (F_MONO, 8, "bold"), tags=ST, track=1.0) + 10
        self.panel(x, 25, x + 46, 45, t["trim"] if t["hard"] else t["card"], t["edge"] if t["hard"] else t["edge_hi"], ST, off=1)
        p.text(x + 23, 35, f"v{APP_VERSION}", t["dim"] if not t["hard"] else t["txt"], (F_MONO, 8, "bold"), anchor="c", tags=ST)
        # subhead
        p.text(20, 76, "AUDIO STREAM RECEIVER", t["txt2"] if not t["hard"] else t["txt"], (F_MONO, 9, "bold"), tags=ST, track=1.4)
        parts = [("SAMPLE RATE:", t["txt2"], "normal"), (" 48.0 kHz", t["txt"], "bold"), ("  |  ", t["dimmer"], "normal"),
                 ("FORMAT:", t["txt2"], "normal"), (" PCM 16-bit", t["txt"], "bold")]
        fnt = lambda wt: (F_MONO, 8, wt)
        total = sum(p.tw(s, fnt(wt)) for s, _, wt in parts)
        xx = W - 20 - total
        for s, col, wt in parts:
            xx += p.text(xx, 76, s, col, fnt(wt), tags=ST)
        p.line([(20, 118), (W - 20, 118)], t["edge"] if t["hard"] else t["card_edge"], width=2 if t["hard"] else 1, tags=ST)
        # network cards
        for x1, x2, label in ((20, 404, "PHONE IP ADDRESS"), (416, 600, "PORT")):
            self.panel(x1, 130, x2, 226, t["card"], t["card_edge"], ST)
            lab_col = t["txt2"] if not t["hard"] else t["txt"]
            if label == "PORT":
                p.text(x1 + 14, 148, label, lab_col, (F_MONO, 8, "bold"), tags=ST, track=1.2)
            else:  # little phone icon + label
                p.rect(x1 + 14, 140, x1 + 22, 156, outline=t["accent"], tags=ST)
                p.line([(x1 + 17, 153), (x1 + 19, 153)], t["accent"], tags=ST)
                p.text(x1 + 30, 148, label, lab_col, (F_MONO, 8, "bold"), tags=ST, track=1.2)
        # controls panel frame + divider + label
        self.panel(20, 322, 600, 550, t["panel"], t["panel_edge"], ST)
        p.line([(34, 410), (586, 410)], t["card_edge"] if not t["hard"] else t["edge"], width=1, tags=ST)
        p.text(36, 428, "VOLUME OUTPUT", t["txt2"] if not t["hard"] else t["txt"], (F_MONO, 8, "bold"), tags=ST, track=1.2)
        # footer + bezel
        p.rect(0, 558, W, H, fill=t["foot"], tags=ST)
        p.line([(0, 558), (W, 558)], t["foot_edge"], width=2 if t["hard"] else 1, tags=ST)
        p.rect(0, H - 4, W, H, fill=t["trim"], tags=ST)
        for sx in (16, W - 16):
            p.oval(sx - 3, H - 8, sx + 3, H - 2, fill=t["screw"], tags=ST)

    # -- real widgets
    def _widgets(self):
        a, S, c, t = self.app, self.S, self.c, self.t
        digits = (self.root.register(lambda s: s == "" or (s.isdigit() and len(s) <= 5)), "%P")
        common = dict(relief="flat", bd=0, highlightthickness=0, font=(F_MONO, 17, "bold"))
        self.ip_e = tk.Entry(self.root, textvariable=a.ip, **common)
        self.port_e = tk.Entry(self.root, textvariable=a.port, justify="center", validate="key", validatecommand=digits, **common)
        c.create_window(self.d(84), self.d(194), window=self.ip_e, anchor="w", width=self.d(244), height=self.d(30), tags=("win",))
        c.create_window(self.d(508), self.d(194), window=self.port_e, anchor="center", width=self.d(140), height=self.d(30), tags=("win",))
        for key, e in (("ip", self.ip_e), ("port", self.port_e)):
            e.bind("<FocusIn>", lambda ev, k=key: self._focus(k, True))
            e.bind("<FocusOut>", lambda ev, k=key: self._focus(k, False))
        self._style_entries()
        self.slider = LevelSlider(self.root, a.on_vol, a.gain, width=self.d(552), height=self.d(40), bg=t["panel"], scale=S, theme=t)
        c.create_window(self.d(34), self.d(440), window=self.slider, anchor="nw", tags=("win",))

    def _style_entries(self):
        t, live = self.t, bool(self.app.streaming)
        ipfg = t["lcd_ip_on"] if live else t["lcd_ip_off"]
        self.ip_e.config(bg=t["lcd_ip"], fg=ipfg, insertbackground=ipfg, disabledbackground=t["lcd_ip"], disabledforeground=ipfg)
        self.port_e.config(bg=t["lcd_port"], fg=t["lcd_port_fg"], insertbackground=t["lcd_port_fg"],
                           disabledbackground=t["lcd_port"], disabledforeground=t["lcd_port_fg"])

    def _focus(self, key, on):
        self.focus[key] = on
        self.keys.pop("cards", None)

    def set_locked(self, on):
        for e in (self.ip_e, self.port_e):
            e.config(state="disabled" if on else "normal")
        self.keys.clear()

    def _actions(self):
        c, a = self.c, self.app
        acts = {"act_connect": a.toggle, "act_nc": lambda: self._flip(a.nc_var, a.on_noise),
                "act_mon": lambda: self._flip(a.mon_var, a.on_monitor), "act_mute": a.toggle_mute,
                "act_hotkey": a.capture_hotkey, "act_light": lambda: a.set_theme("light"), "act_dark": lambda: a.set_theme("dark")}
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
        self.blink = int(time.monotonic() / 0.9) % 2 == 0
        pulse = self.blink if mode == "live" else None
        layers = {
            "hdr": (mode, st["muted"], pulse, self.name),
            "chips": (mode, st["muted"], a.ping_ms is not None, self.name),
            "cards": (mode, self.focus["ip"], self.focus["port"], self.name),
            "btn": (st["active"], mode, self.name),
            "opts": (st["nc"], st["mon"], self.name),
            "vol": (st["muted"], st["pct"], self._db_text(), self._segments(), self.name),
            "mute": (st["muted"], a.hotkey_label(), self.name),
            "foot": (mode, st["muted"], a.status, a.pps, a.ping_ms, pulse, self.name),
        }
        for name, key in layers.items():
            if self.keys.get(name) != key:
                self.keys[name] = key
                self.c.delete(name)
                getattr(self, "_draw_" + name)(st, mode)
                if name == "cards":
                    self._style_entries()
        self.slider.set_state(0.0 if st["muted"] else a.gain, a.shown, st["muted"])
        self.c.tag_raise("win")

    def _db(self):
        s = self.app.shown
        return None if s < 0.003 else 20 * math.log10(min(1.0, s))

    def _db_text(self):
        db = self._db()
        return "-\u221e dBFS" if db is None else f"{db:.1f} dBFS"

    def _segments(self):
        """How many of the 10 meter segments are lit (-48 dBFS .. -3 dBFS in 5 dB steps)."""
        db = self._db()
        if self.app.muted or db is None:
            return 0
        return sum(1 for i in range(10) if db >= -48 + 5 * i)

    # header: theme switch, connection pill, muted pill
    def _draw_hdr(self, st, mode):
        p, t, W = self.p, self.t, self.W
        x = W - 20
        if st["muted"] and mode != "off":
            w = p.chip(x, 35, "MIC MUTED", t["coral"], t["coral_bg"], t["coral_edge"], anchor="e", tags=("hdr",),
                       font=(F_MONO, 8, "bold"), track=0.6, h=22)
            x -= w + 8
        txt, fg, bg, edge, dot = {
            "live": ("CONNECTED", t["green"] if not t["hard"] else t["green_edge"], t["green_bg"], t["green_edge"], t["green"]),
            "connecting": ("CONNECTING", t["amber"], t["amber_bg"], t["amber_edge"], t["amber"]),
            "off": ("DISCONNECTED", t["amber"] if t["hard"] else t["dim"], t["amber_bg"] if t["hard"] else t["head"],
                    t["amber_edge"] if t["hard"] else t["edge_hi"], t["amber"]),
        }[mode]
        if mode == "live" and self.blink:
            dot = mix(dot, t["green_bg"], 0.55)
        w = p.chip(x, 35, txt, fg, bg, edge, anchor="e", dot=dot, tags=("hdr",), font=(F_MONO, 8, "bold"), track=0.8, h=22)
        x -= w + 10
        font = (F_MONO, 8, "bold")  # LIGHT | DARK switch
        light, dark = self.name == "light", self.name == "dark"
        wl, wd = p.tw("LIGHT", font, 0.6) + 18, p.tw("DARK", font, 0.6) + 18
        x0 = x - (wl + wd + 14)
        self.panel(x0, 24, x, 46, "#070b10" if dark else t["trim"], "#1e2d42" if dark else t["edge"], ("hdr",), off=1)
        xs = x0 + 6
        p.rect(xs - 4, 26, xs + wl - 4, 44, fill=t["panel"] if light else "", tags=("hdr", "act_light"))
        if light:
            p.oval(xs, 32, xs + 6, 38, fill=t["green"], tags=("hdr", "act_light"))
        p.text(xs + (9 if light else 2), 35, "LIGHT", t["txt"] if light else t["dim"], font, tags=("hdr", "act_light"), track=0.6)
        sx = xs + wl - 4
        p.text(sx, 35, "|", t["dimmer"], font, tags=("hdr",))
        xd = sx + 14
        p.rect(xd - 4, 26, xd + wd - 2, 44, fill="", tags=("hdr", "act_dark"))
        if dark:
            p.oval(xd, 32, xd + 6, 38, fill=t["accent"], tags=("hdr", "act_dark"))
        p.text(xd + (9 if dark else 2), 35, "DARK", t["txt"] if dark else t["dim"], font, tags=("hdr", "act_dark"), track=0.6)

    def _draw_chips(self, st, mode):
        p, t = self.p, self.t
        live = mode == "live"
        x = 20
        if live and not st["muted"]:
            x += p.chip(x, 98, "LIVE AUDIO", t["green"] if not t["hard"] else t["green_edge"], t["green_bg"], t["green_edge"],
                        dot=t["green"], tags=("chips",)) + 8
        elif st["muted"]:
            x += p.chip(x, 98, "AUDIO MUTED", t["coral"], t["coral_bg"], t["coral_edge"], dot=t["coral"], tags=("chips",)) + 8
        p.chip(x, 98, "LOW-LATENCY // ACTIVE" if live else "LOW-LATENCY", t["chip_fg"], t["chip_bg"], t["chip_edge"], tags=("chips",))
        if live:
            kb, fb = f"{BITRATE_KBPS} kbps", (F_MONO, 8, "bold")
            wv = p.tw(kb, fb)
            p.text(600 - wv, 98, kb, t["accent"], fb, tags=("chips",))
            p.text(600 - wv - 8, 98, "BITRATE:", t["txt2"], (F_MONO, 8, "normal"), anchor="e", tags=("chips",))

    def _draw_cards(self, st, mode):
        p, t = self.p, self.t
        live = mode == "live"
        tag = ("cards",)
        x = 392
        x -= p.chip(x, 148, "IPv4", t["chip_fg"], t["chip_bg"], t["chip_edge"], anchor="e", tags=tag,
                    font=(F_MONO, 7, "bold"), track=0.6, h=16) + 6
        p.chip(584, 148, "TCP", t["chip_fg"], t["chip_bg"], t["chip_edge"], anchor="e", tags=tag,
               font=(F_MONO, 7, "bold"), track=0.6, h=16)
        for key, x1, x2, bg, edge in (("ip", 30, 394, t["lcd_ip"], t["lcd_ip_edge"]), ("port", 426, 590, t["lcd_port"], t["lcd_port_edge"])):
            col = t["accent"] if self.focus[key] else edge
            self.panel(x1, 166, x2, 218, bg, col, tag, off=2)
            if not t["hard"]:
                for yy in range(168, 217, 3):  # scan lines
                    p.line([(x1 + 1, yy), (x2 - 1, yy)], t["scan"], tags=tag)
        gc = t["lcd_ip_on"] if live else t["lcd_ip_off"]  # globe icon
        p.oval(40, 184, 58, 202, outline=gc, tags=tag)
        p.oval(45, 184, 53, 202, outline=gc, tags=tag)
        p.line([(40, 193), (58, 193)], gc, tags=tag)
        tg, col, bgc, ec = (("ONLINE", t["green"] if not t["hard"] else t["green_edge"], t["green_bg"], t["green_edge"]) if live
                            else ("STANDBY", t["lcd_tag"], t["lcd_ip"] if not t["hard"] else "#f3f4f6", t["dimmer"]))
        p.chip(386, 192, tg, col, bgc, ec, anchor="e", tags=tag, font=(F_MONO, 7, "bold"), track=0.8, h=16)

    def _draw_btn(self, st, mode):
        p, t, on = self.p, self.t, st["active"]
        x, y, w, h, cut = 20, 240, 580, 54, 15
        tag = ("btn", "act_connect")
        shape = [(x, y), (x + w - cut, y), (x + w, y + cut), (x + w, y + h), (x + cut, y + h), (x, y + h - cut)]
        c1, c2 = (t["btn_off"], t["btn_off2"]) if on else (t["btn_on"], t["btn_on2"])
        sh = t["btn_off_lip"] if on else t["btn_on_shadow"]
        p.poly([(px + 4, py + 4) for px, py in shape], fill=sh, tags=tag)  # hard shadow / lower lip
        n = 28
        for i in range(n):
            pts = clip_x(shape, x + w * i / n - (0.6 if i else 0), x + w * (i + 1) / n + 0.6)
            if len(pts) >= 3:
                p.poly(pts, fill=mix(c1, c2, i / (n - 1)), tags=tag)
        if t["hard"]:
            p.poly(shape, outline=t["edge"], width=2, tags=tag)
        label = "DISCONNECT" if on else "CONNECT"
        fg = t["btn_off_text"] if on else t["btn_on_text"]
        font = (F_DISP, 14, "bold")
        tw = p.tw(label, font, 3)
        tag_txt = "LIVE RX ACTIVE" if on and mode == "live" else ""
        tagw = (p.tw(tag_txt, (F_MONO, 7, "bold"), 0.5) + 16 + 10) if tag_txt else 0
        gx = x + w / 2 - (14 + 12 + tw + tagw) / 2
        cy = y + h / 2
        if on:
            p.rect(gx, cy - 6, gx + 12, cy + 6, fill="#ffffff", tags=tag)
        else:
            p.poly([(gx, cy - 7), (gx + 12, cy), (gx, cy + 7)], fill=fg, tags=tag)
        p.text(gx + 26, cy, label, fg, font, tags=tag, track=3)
        if tag_txt:
            bx = gx + 26 + tw + 10
            p.rect(bx, cy - 9, bx + tagw - 10, cy + 9, fill="#7a0f2c" if not t["hard"] else "#c4123f", outline="#ffd0dc", tags=tag)
            p.text(bx + 8, cy, tag_txt, "#ffe4ea", (F_MONO, 7, "bold"), tags=tag, track=0.5)

    def _check(self, y, checked, label, tag):
        p, t = self.p, self.t
        p.rect(34, y - 12, 280, y + 12, fill=t["panel"], tags=("opts", tag))  # click area
        edge = t["green"] if (checked and not t["hard"]) else t["edge"] if t["hard"] else t["edge_hi"]
        p.rect(36, y - 8, 52, y + 8, fill=t["green"] if checked else t["check_off"], outline=edge,
               width=2 if t["hard"] else 1, tags=("opts", tag))
        if checked:
            p.line([(39, y), (43, y + 4), (50, y - 4)], "#041b10" if not t["hard"] else "#ffffff", width=2, tags=("opts", tag))
        p.text(64, y, label, t["txt"] if checked or t["hard"] else t["txt2"], (F_MONO, 9, "bold"), tags=("opts", tag))

    def _draw_opts(self, st, mode):
        p, t = self.p, self.t
        self._check(348, st["nc"], "Noise suppression", "act_nc")
        self._check(382, st["mon"], "Play on speaker", "act_mon")
        self.panel(310, 336, 586, 396, t["callout"], t["callout_edge"], ("opts",), off=2)  # routing hint
        p.oval(322, 358, 338, 374, outline=t["txt2"] if not t["hard"] else t["edge"], tags=("opts",))
        p.text(330, 366, "i", t["txt2"] if not t["hard"] else t["edge"], (F_MONO, 8, "bold"), anchor="c", tags=("opts",))
        p.text(348, 366, 'Choose "CABLE Output" as the microphone in your app or game', t["txt2"], (F_UI, 9, "normal"),
               anchor="w", tags=("opts",), wrap=226)

    def _draw_vol(self, st, mode):
        p, t = self.p, self.t
        tag = ("vol",)
        cy = 428
        if st["muted"]:
            p.chip(170, cy, "MUTED (0%)", t["coral"], t["coral_bg"], t["coral_edge"], tags=tag, font=(F_MONO, 8, "bold"), track=0.6)
        else:
            p.chip(170, cy, f"Volume {st['pct']}%", t["chip_fg"], t["chip_bg"], t["chip_edge"], tags=tag,
                   font=(F_MONO, 8, "bold"), track=0.4)
        lit = self._segments()  # PEAK readout + 10-segment meter on the right
        x, bar_w = 584, 10 * 6 + 9 * 2 + 10
        p.rect(x - bar_w, cy - 8, x, cy + 8, fill="#000000" if not t["hard"] else t["track2"], outline=t["track_edge"], tags=tag)
        for i in range(10):
            col = t["green"] if i < 6 else "#facc15" if i < 8 else t["coral"]
            fill = col if i < lit else (mix(col, "#000000", 0.78) if not t["hard"] else t["seg_off"])
            sx = x - bar_w + 5 + i * 8
            p.rect(sx, cy - 5, sx + 6, cy + 5, fill=fill, tags=tag)
        x -= bar_w + 10
        vcol = t["coral"] if st["muted"] else (t["green"] if not t["hard"] else t["green_edge"])
        wv = self.glow(x, cy, self._db_text(), vcol, (F_MONO, 8, "bold"), mix(vcol, t["panel"], 0.75), anchor="e", tags=tag)
        p.text(x - wv - 6, cy, "PEAK:", t["dim"], (F_MONO, 8, "normal"), anchor="e", tags=tag)
        x0, span = 48, 524  # scale under the fader
        for pct in (0, 25, 50, 75, 100):
            lab, colr = f"{pct}%", (t["dimmer"] if not t["hard"] else t["txt2"])
            if st["muted"] and pct == 0:
                lab, colr = "0% [MUTED]", t["coral"]
            elif st["muted"] and pct == 100:
                lab = "100% [MAX]"
            p.text(x0 + span * pct / 100, 490, lab, colr, (F_MONO, 8, "normal"),
                   anchor="w" if pct == 0 else "e" if pct == 100 else "c", tags=tag)

    def _draw_mute(self, st, mode):
        p, t, muted = self.p, self.t, st["muted"]
        y1, y2 = 504, 538
        tag = ("mute", "act_mute")
        wide = 366 if muted else 288
        coral = t["coral"]
        if muted:
            self.panel(34, y1, 34 + wide - 10, y2, t["coral_bg"] if not t["hard"] else "#ffe4e6", coral, tag, off=2)
        else:
            self.panel(34, y1, 34 + wide - 10, y2, t["mute_bg"], t["mute_edge"], tag, off=2)
        col = coral if muted else t["mute_text"]
        cy = (y1 + y2) / 2
        label = "UNMUTE MICROPHONE" if muted else "MUTE"
        font = (F_MONO, 9, "bold")
        gx = 34 + (wide - 10) / 2 - (24 + p.tw(label, font, 1.5)) / 2
        p.rect(gx + 4, cy - 8, gx + 12, cy + 2, outline=coral, width=2, tags=tag)  # microphone with a slash
        p.line([(gx + 1, cy - 1), (gx + 1, cy + 3), (gx + 8, cy + 8), (gx + 15, cy + 3), (gx + 15, cy - 1)], coral, tags=tag)
        p.line([(gx + 8, cy + 8), (gx + 8, cy + 11)], coral, tags=tag)
        p.line([(gx - 1, cy - 11), (gx + 17, cy + 11)], coral, width=2, tags=tag)
        p.text(gx + 26, cy, label, col, font, tags=tag, track=1.5)
        hx1, hx2, hk_tag = 34 + wide + 2, 586, ("mute", "act_hotkey")  # hotkey box
        self.panel(hx1, y1, hx2, y2, t["field"], t["card_edge"] if not t["hard"] else t["edge"], hk_tag, off=2)
        key = self.app.hotkey_label()
        kf = (F_MONO, 9, "bold")
        kw = max(24, p.tw(key, kf, 0.4) + 18)
        lab = "HOTKEY:"
        lw = p.tw(lab, (F_MONO, 8, "normal"), 1.0)
        gx = (hx1 + hx2) / 2 - (lw + 10 + kw) / 2
        p.text(gx, cy, lab, t["txt2"], (F_MONO, 8, "normal"), tags=hk_tag, track=1.0)
        kx = gx + lw + 10
        p.rect(kx, cy - 11, kx + kw, cy + 11, fill=t["kbd_bg"], outline=t["kbd_edge"], tags=hk_tag)
        p.text(kx + kw / 2, cy, key, t["accent"], kf, anchor="c", tags=hk_tag, track=0.4)

    def _draw_foot(self, st, mode):
        p, a, t = self.p, self.app, self.t
        tag = ("foot",)
        col = {"live": t["green"], "connecting": t["amber"], "off": t["dimmer"]}[mode]
        if mode == "live" and self.blink:
            col = mix(col, t["foot"], 0.5)
        p.oval(22, 570, 34, 582, fill=col if mode != "off" else "", outline=t["dimmer"] if mode == "off" else "", tags=tag)
        ping = f"{a.ping_ms}" if (mode == "live" and a.ping_ms is not None) else "\u2014"
        pk = f"{a.pps}" if mode == "live" else "0"
        parts = [("PACKETS: ", t["dim"], "normal"), (pk, t["accent"], "bold"), (" pkts/s", t["dim"], "normal"),
                 ("   PING: ", t["dim"], "normal"), (ping, t["green"] if not t["hard"] else t["green_edge"], "bold"), (" ms", t["dim"], "normal")]
        f = lambda wt: (F_MONO, 8, wt)
        total = sum(p.tw(s, f(wt)) for s, _, wt in parts)
        room = 600 - total - 16 - 46
        fnt = (F_MONO, 9, "bold")
        if mode == "live":
            msg = "Connected " + ("(Audio Muted)" if st["muted"] else "(Streaming)")
            p.text(46, 576, p.fit(msg, fnt, room), t["txt"], fnt, anchor="w", tags=tag)
        else:
            msg, colr = shorten(a.status, 92), t["txt2"] if mode == "connecting" else t["dim"]
            if p.tw(msg, fnt) <= room:
                p.text(46, 576, msg, colr, fnt, anchor="w", tags=tag)
            else:  # two smaller lines that sit inside the footer
                small = (F_MONO, 8, "bold")
                lines, cur = [], ""
                for word in msg.split(" "):
                    tt = (cur + " " + word).strip()
                    if p.tw(tt, small) <= room or not cur:
                        cur = tt
                    else:
                        lines.append(cur)
                        cur = word
                lines.append(cur)
                lines = lines[:2]
                lines[-1] = p.fit(lines[-1], small, room)
                for line, yy in zip(lines, (576,) if len(lines) == 1 else (570, 582)):
                    p.text(46, yy, line, colr, small, anchor="w", tags=tag)
        x = 600 - total
        for s_, colr, wt in parts:
            x += p.text(x, 576, s_, colr, f(wt), tags=tag)


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
        self.theme = cfg.get("theme", "dark") if cfg.get("theme") in ("dark", "light") else "dark"
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
        root.protocol("WM_DELETE_WINDOW", self.on_close_button)
        self.tray = TrayIcon(self)
        if sys.platform == "win32":
            self.tray.start(resource("uwumic.ico"))
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
        self.status = "Virtual microphone installed"

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

        def pretty(x):
            m = re.fullmatch(r"vk(\d{1,3})", x)
            if m:  # older saved hotkeys: show the key's real name when we have one
                x = vk_to_name(int(m.group(1))) or x
            if x in _PRETTY:
                return _PRETTY[x]
            m = re.fullmatch(r"num(\d)", x)
            if m:
                return f"Num {m.group(1)}"
            return x.capitalize() if len(x) > 1 else x.upper()

        return "+".join(pretty(x) for x in hk.split("+"))

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
        self.tray.set_tip("UwU Mic - " + ("Muted" if self.muted and self.streaming else "Connected" if self.streaming
                                           else "Connecting..." if self.active else "Disconnected"))
        self.root.after(40, self.poll)

    def save(self):
        try:
            with open(CFG_PATH, "w", encoding="utf-8") as f:
                json.dump({"ip": self.ip.get().strip(), "port": self.port.get(), "setup_tries": self.setup_tries, "noise_cancel": self.noise_cancel, "theme": self.theme,
                           "gain": self.gain, "hotkey": self.hotkey}, f)
        except Exception:
            pass

    def set_theme(self, name):
        if name in ("dark", "light") and name != self.theme:
            self.theme = name
            self.ui.set_theme(name)
            self.save()

    # -- tray
    def on_close_button(self):
        """X in the title bar: hide to the tray (or really close if there is no tray icon to come back from)."""
        if self.tray.ok:
            self.root.withdraw()
        else:
            self.close()

    def show_window(self):
        self.root.deiconify()
        try:
            self.root.state("normal")
        except Exception:
            pass
        self.root.lift()
        self.root.focus_force()

    def tray_connect(self):
        if self.active:
            return
        if not self.ip.get().strip():  # nothing to connect to yet: bring the window back so the address can be typed
            self.show_window()
            return
        self.connect()

    def tray_disconnect(self):
        if self.active:
            self.disconnect()

    def quit_app(self):
        self.close()

    def close(self):
        self.tray.stop()
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
    if already_running():
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo("UwU Mic", "UwU Mic is already running.\n\nLook for its icon next to the clock (click the ^ arrow "
                                       "if you do not see it) and click it to open the window.")
        return
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
