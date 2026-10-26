"""
LED Strip: a system tray app for the ESP32-C3 LED strip, over Bluetooth LE.

Left-click the tray icon  -> panel: power, Manual (your presets: solid colors and color
                             loops like Rainbow, plus brightness) or Screen sync
Right-click the tray icon -> quick menu: light on/off, screen sync, presets, quit
Right-click a preset      -> edit, duplicate or delete it; the + tile makes a new one

Finds the strip over Bluetooth by itself (no pairing needed). If the strip is plugged
into this PC by USB, that's used instead. The strip stays lit only while this app is
connected: when the app quits, the PC sleeps or goes out of range, it fades off.

Install once:  py -m pip install pystray pillow mss numpy bleak pyserial
Run:           pyw led_screen_sync.pyw

On first run it registers itself to start with Windows (toggle in the right-click menu).
Keep this file where it is; the startup entry points to this location.
Settings and a log are stored in %APPDATA%\\LEDScreenSync.
"""

import asyncio
import concurrent.futures
import copy
import ctypes
import json
import logging
import os
import queue
import struct
import sys
import threading
import time
import tkinter as tk
import uuid
from pathlib import Path
from tkinter import colorchooser, messagebox
from urllib.parse import urlencode

import mss
import mss.exception
import numpy as np
import pystray
import serial
from bleak import BleakClient, BleakScanner
from PIL import Image, ImageDraw
from serial.tools import list_ports

APP_NAME = "LED Strip"
CONFIG_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "LEDScreenSync"
CONFIG_FILE = CONFIG_DIR / "settings.json"
LOG_FILE = CONFIG_DIR / "log.txt"

# Must match ledstrip_ble.ino
SERVICE_UUID = "6e4c0001-7a3b-4c8e-9f21-3d5b8a6c0e11"
CONTROL_UUID = "6e4c0002-7a3b-4c8e-9f21-3d5b8a6c0e11"
STATE_UUID = "6e4c0003-7a3b-4c8e-9f21-3d5b8a6c0e11"
ESPRESSIF_USB_VID = 0x303A

DEFAULTS = {
    "mode": "manual",       # "manual" or "sync"
    "ble_address": None,    # remembered after the first connection, for faster reconnects
    # manual mode
    "on": True,
    "level": 200,           # brightness 1..255
    "current": {"type": "solid", "color": "#ffb070"},   # what manual mode shows right now
    "preset_id": None,      # the preset it came from, if any
    "presets": None,        # filled with DEFAULT_PRESETS on first run
    # screen sync
    "monitor": 1,
    "brightness": 1.0,      # multiplier
    "saturation": 1.3,      # color boost, 1 = none
    "smooth": 0.5,          # responsiveness, 1 = instant
    "fade": 125,            # strip-side fade in ms between updates
    "fps": 20,
}

def _solid(name, color):
    return {"name": name, "type": "solid", "color": color}


def _loop(name, period, blend, *colors, positions=None):
    positions = positions or [i / len(colors) for i in range(len(colors))]
    return {"name": name, "type": "loop", "period": period, "blend": blend,
            "stops": [{"pos": round(p, 4), "color": c} for p, c in zip(positions, colors)]}


# A preset is either a solid color or a color loop: color stops on a ring (pos 0..1) that
# the strip travels around once every `period` seconds, gliding ("smooth") or jumping ("steps").
DEFAULT_PRESETS = [
    _solid("Red", "#ff0000"), _solid("Orange", "#ff6a00"), _solid("Yellow", "#ffcc00"),
    _solid("Green", "#00ff40"), _solid("Cyan", "#00ccff"), _solid("Blue", "#0030ff"),
    _solid("Purple", "#7a00ff"), _solid("Pink", "#ff00a0"), _solid("White", "#ffffff"),
    _solid("Warm white", "#ffb070"), _solid("Sunset", "#ff4010"), _solid("Mint", "#20ffa0"),
    _loop("Rainbow", 12, "smooth", "#ff0000", "#ffff00", "#00ff00", "#00ffff", "#0000ff", "#ff00ff"),
    _loop("Breathe", 5, "smooth", "#ffb070", "#000000"),
    _loop("Sunset drift", 24, "smooth", "#ff4010", "#ff00a0", "#7a00ff", "#ffb070",
          positions=[0, 0.3, 0.55, 0.8]),
    _loop("Ocean", 18, "smooth", "#0030ff", "#00ccff", "#20ffa0", positions=[0, 0.45, 0.7]),
    _loop("Party", 2, "steps", "#ff0000", "#00ff40", "#0030ff", "#ffcc00"),
]

MANUAL_FADE = 250     # ms, color/brightness changes made here
EFFECT_FPS = 30       # updates per second while a color loop runs
STEP_FADE = 150       # ms, how quickly "steps" loops jump to the next color
HANDOFF_FADE = 600    # ms, switching between manual and screen sync
OFF_FADE = 800        # ms, turning off on quit
HEARTBEAT_S = 1.5     # resend at least this often; the strip turns off after 6 s of silence

# Panel colors
BG, PANEL, FG, MUTED, ACCENT = "#1c1d21", "#2a2c31", "#eeeeee", "#8a8d93", "#ff7828"


class LinkLost(OSError):
    pass


class NotFound(OSError):
    pass


LINK_ERRORS = (OSError, ValueError, serial.SerialException)


def hex_to_rgb01(h):
    h = h.lstrip("#")
    return np.array([int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4)], dtype=np.float32)


def hex_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def rgb_hex(rgb):
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(round(v)))) for v in rgb)


def new_id():
    return uuid.uuid4().hex[:10]


def look_of(preset):
    """The part of a preset that decides what the strip shows (no name / id)."""
    keys = ("type", "color") if preset["type"] == "solid" else ("type", "stops", "period", "blend")
    return copy.deepcopy({k: preset[k] for k in keys})


def compile_stops(stops):
    """Sorted (pos, rgb) points with the first stop repeated after 1.0, so the loop closes."""
    pts = sorted(((min(max(float(s["pos"]), 0.0), 0.9999), hex_rgb(s["color"])) for s in stops),
                 key=lambda p: p[0])
    if pts:
        pts.append((pts[0][0] + 1.0, pts[0][1]))
    return pts


def loop_color(pts, phase, blend="smooth"):
    """Color (0..255 floats) at `phase` (0..1) around a compiled loop."""
    if not pts:
        return (0.0, 0.0, 0.0)
    if len(pts) == 2:   # a single stop
        return tuple(float(v) for v in pts[0][1])
    p = phase % 1.0
    if p < pts[0][0]:   # before the first stop: still in the wrap-around segment
        p += 1.0
    for (a, ca), (b, cb) in zip(pts, pts[1:]):
        if a <= p < b:
            if blend == "steps":
                return tuple(float(v) for v in ca)
            t = (p - a) / (b - a)
            return tuple(x + (y - x) * t for x, y in zip(ca, cb))
    return tuple(float(v) for v in pts[-1][1])


def preview_color(look):
    """A representative color for a look (for icons): the solid color or the first stop."""
    if look["type"] == "solid":
        return look["color"]
    return rgb_hex(loop_color(compile_stops(look["stops"]), 0.0))


# ---------------------------------------------------------------- settings
class Settings:
    def __init__(self):
        self.data = copy.deepcopy(DEFAULTS)
        self.first_run = not CONFIG_FILE.exists()
        try:
            loaded = json.loads(CONFIG_FILE.read_text())
            if "mode" not in loaded and "enabled" in loaded:   # settings from an older version
                loaded["mode"] = "sync" if loaded["enabled"] else "manual"
            if "current" not in loaded and "color" in loaded:   # before presets existed
                loaded["current"] = {"type": "solid", "color": loaded["color"]}
            for old in ("enabled", "host", "color"):
                loaded.pop(old, None)
            self.data.update(loaded)
        except (OSError, ValueError):
            pass
        if not self.data.get("presets"):
            self.data["presets"] = copy.deepcopy(DEFAULT_PRESETS)
        for p in self.data["presets"]:
            p.setdefault("id", new_id())
        if self.data.get("preset_id") is None:   # highlight a preset that matches what's showing
            self.data["preset_id"] = next((p["id"] for p in self.data["presets"]
                                           if look_of(p) == self.data["current"]), None)

    def __getitem__(self, key):
        return self.data[key]

    def __setitem__(self, key, value):
        self.data[key] = value

    def save(self):
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            CONFIG_FILE.write_text(json.dumps(self.data, indent=2))
        except OSError:
            logging.exception("Could not save settings")


# ---------------------------------------------------------------- start with Windows
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "LEDScreenSync"


def startup_command():
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")   # no console window
    return f'"{pyw if pyw.exists() else exe}" "{Path(__file__).resolve()}"'


def startup_enabled():
    if sys.platform != "win32":
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, RUN_NAME)
            return True
    except OSError:
        return False


def set_startup(enable):
    if sys.platform != "win32":
        return
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enable:
                winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, startup_command())
            else:
                try:
                    winreg.DeleteValue(key, RUN_NAME)
                except FileNotFoundError:
                    pass
    except OSError:
        logging.exception("Could not change startup setting")


# ---------------------------------------------------------------- links to the strip
# Both links offer: kind, lost, label(), state(), send(rgb, brightness, on, fade), close()

class AsyncRunner:
    """A private asyncio loop on its own thread, so the (sync) worker can call bleak."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True, name="bluetooth").start()

    def run(self, coro, timeout):
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            return future.result(timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise LinkLost("Bluetooth timed out") from None


def encode_packet(rgb, brightness, on, fade):
    """10-byte control packet, see the firmware header."""
    r, g, b = (max(0, min(65280, int(round(float(v) * 256)))) for v in rgb)
    return struct.pack("<HHHBBH", r, g, b, max(0, min(255, int(brightness))), 1 if on else 0,
                       max(0, min(10000, int(fade))))


class BleLink:
    kind = "ble"

    def __init__(self, runner, device):
        self.runner = runner
        self.address = device.address
        self.lost = False

        async def make_client():   # create it on the Bluetooth thread, where it will run
            return BleakClient(device, disconnected_callback=self._on_disconnect, timeout=15)
        self.client = runner.run(make_client(), 5)

    def _on_disconnect(self, client):
        self.lost = True

    @classmethod
    def open(cls, runner, address=None):
        async def find():
            if address:   # quick path: the strip we used last time
                device = await BleakScanner.find_device_by_address(address, timeout=4)
                if device:
                    return device
            return await BleakScanner.find_device_by_filter(
                lambda d, adv: SERVICE_UUID in (u.lower() for u in adv.service_uuids), timeout=6)

        try:
            device = runner.run(find(), 15)
        except LinkLost:
            raise
        except Exception as e:   # adapter off or missing, etc.
            raise LinkLost(f"Bluetooth unavailable ({e or type(e).__name__})") from e
        if device is None:
            raise NotFound("Strip not found over Bluetooth. Is it powered and in range?")

        link = cls(runner, device)
        try:
            link._run(link.client.connect(), 20)
            if link.state().get("device") != "ledstrip":
                raise LinkLost("unexpected device")
        except LINK_ERRORS:
            link.close()
            raise
        return link

    def _run(self, coro, timeout):
        try:
            return self.runner.run(coro, timeout)
        except LinkLost:
            raise
        except Exception as e:
            raise LinkLost(str(e) or type(e).__name__) from e

    def label(self):
        return "Bluetooth"

    def state(self):
        data = self._run(self.client.read_gatt_char(STATE_UUID), 5)
        return json.loads(bytes(data).decode("utf-8", "replace"))

    def send(self, rgb, brightness, on, fade):
        if self.lost:
            raise LinkLost("Bluetooth disconnected")
        self._run(self.client.write_gatt_char(CONTROL_UUID, encode_packet(rgb, brightness, on, fade),
                                              response=False), 3)

    def close(self):
        try:
            self.runner.run(self.client.disconnect(), 5)
        except Exception:
            pass


class UsbLink:
    """Line-based commands over the C3's USB port (see the firmware header)."""
    kind = "usb"
    lost = False

    def __init__(self, port):
        self.port = port
        self.buf = b""
        self.ser = serial.Serial()
        self.ser.port = port
        self.ser.baudrate = 115200
        self.ser.timeout = 0.05
        self.ser.write_timeout = 1
        self.ser.dtr = False   # leave the C3's reset/boot lines alone
        self.ser.rts = False
        self.ser.open()

    @staticmethod
    def ports():
        return [p.device for p in list_ports.comports() if p.vid == ESPRESSIF_USB_VID]

    def label(self):
        return f"USB ({self.port})"

    def close(self):
        try:
            self.ser.close()
        except Exception:
            pass

    def _write(self, line):
        try:
            self.ser.write(line.encode() + b"\n")
        except Exception as e:   # unplugged: report it as a lost link whatever the OS raised
            raise LinkLost(str(e)) from e

    def state(self):
        try:
            self.ser.reset_input_buffer()   # drop stale lines (boot messages etc.)
        except Exception as e:
            raise LinkLost(str(e)) from e
        self.buf = b""
        self._write("state")
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            try:
                self.buf += self.ser.read(self.ser.in_waiting or 1)
            except Exception as e:
                raise LinkLost(str(e)) from e
            while b"\n" in self.buf:
                line, self.buf = self.buf.split(b"\n", 1)
                line = line.strip().decode("utf-8", "replace")
                if line.startswith("{"):
                    try:
                        data = json.loads(line)
                    except ValueError:
                        continue
                    if "device" in data:
                        return data
        raise LinkLost(f"no reply on {self.port}")

    def send(self, rgb, brightness, on, fade):
        r, g, b = (round(float(v), 2) for v in rgb)
        self._write("put " + urlencode({"r": r, "g": g, "b": b, "brightness": int(brightness),
                                        "on": int(bool(on)), "fade": int(fade)}))


# ---------------------------------------------------------------- screen capture
class GdiGrabber:
    """Windows screen capture that leaves the mouse cursor alone.

    mss captures with CAPTUREBLT, which makes Windows hide and redraw the cursor on every
    frame, so it flickers in some apps. This uses plain SRCCOPY instead, and lets Windows
    shrink the screen to SAMPLE_WIDTH pixels in the same step (HALFTONE = proper averaging),
    which is also much less work than copying the full-resolution screen.
    """
    SAMPLE_WIDTH = 160

    def __init__(self):
        from ctypes import wintypes as wt

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
                        ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                        ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
                        ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]

        class BITMAPINFO(ctypes.Structure):
            _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]

        self.BITMAPINFO = BITMAPINFO
        u, g = ctypes.WinDLL("user32"), ctypes.WinDLL("gdi32")
        u.GetDC.argtypes, u.GetDC.restype = [wt.HWND], wt.HDC
        u.ReleaseDC.argtypes, u.ReleaseDC.restype = [wt.HWND, wt.HDC], ctypes.c_int
        g.CreateCompatibleDC.argtypes, g.CreateCompatibleDC.restype = [wt.HDC], wt.HDC
        g.DeleteDC.argtypes, g.DeleteDC.restype = [wt.HDC], wt.BOOL
        g.CreateDIBSection.argtypes = [wt.HDC, ctypes.c_void_p, wt.UINT, ctypes.POINTER(ctypes.c_void_p),
                                       wt.HANDLE, wt.DWORD]
        g.CreateDIBSection.restype = wt.HBITMAP
        g.SelectObject.argtypes, g.SelectObject.restype = [wt.HDC, wt.HGDIOBJ], wt.HGDIOBJ
        g.DeleteObject.argtypes, g.DeleteObject.restype = [wt.HGDIOBJ], wt.BOOL
        g.SetStretchBltMode.argtypes, g.SetStretchBltMode.restype = [wt.HDC, ctypes.c_int], ctypes.c_int
        g.SetBrushOrgEx.argtypes, g.SetBrushOrgEx.restype = [wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_void_p], wt.BOOL
        g.StretchBlt.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                 wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.DWORD]
        g.StretchBlt.restype = wt.BOOL
        g.GdiFlush.argtypes, g.GdiFlush.restype = [], wt.BOOL
        self.u, self.g = u, g
        self.screen_dc = self.mem_dc = self.bitmap = None
        self.size = None
        self.bits = ctypes.c_void_p()

    def _open(self):
        self.screen_dc = self.u.GetDC(None)          # whole virtual desktop
        self.mem_dc = self.g.CreateCompatibleDC(self.screen_dc)
        self.g.SetStretchBltMode(self.mem_dc, 4)      # HALFTONE: average, don't skip pixels
        self.g.SetBrushOrgEx(self.mem_dc, 0, 0, None)
        self.size = None

    def _make_bitmap(self, w, h):
        if self.bitmap:
            self.g.DeleteObject(self.bitmap)
        bmi = self.BITMAPINFO()
        hdr = bmi.bmiHeader
        hdr.biSize = ctypes.sizeof(hdr)
        hdr.biWidth, hdr.biHeight = w, -h             # negative: top-down rows
        hdr.biPlanes, hdr.biBitCount = 1, 32          # BGRA
        self.bitmap = self.g.CreateDIBSection(self.mem_dc, ctypes.byref(bmi), 0, ctypes.byref(self.bits), None, 0)
        if not self.bitmap:
            raise mss.exception.ScreenShotError("CreateDIBSection failed")
        self.g.SelectObject(self.mem_dc, self.bitmap)
        self.size = (w, h)

    def grab(self, mon):
        """mon: an mss monitor dict -> small HxWx4 BGRA uint8 array"""
        if self.mem_dc is None:
            self._open()
        w = self.SAMPLE_WIDTH
        h = max(1, round(w * mon["height"] / mon["width"]))
        if self.size != (w, h):
            self._make_bitmap(w, h)
        ok = self.g.StretchBlt(self.mem_dc, 0, 0, w, h, self.screen_dc,
                               mon["left"], mon["top"], mon["width"], mon["height"], 0x00CC0020)  # SRCCOPY only
        if not ok:          # e.g. lock screen or UAC prompt: start fresh next time
            self.close()
            raise mss.exception.ScreenShotError("StretchBlt failed")
        self.g.GdiFlush()
        buf = (ctypes.c_ubyte * (w * h * 4)).from_address(self.bits.value)
        return np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 4).copy()

    def close(self):
        if self.bitmap:
            self.g.DeleteObject(self.bitmap)
        if self.mem_dc:
            self.g.DeleteDC(self.mem_dc)
        if self.screen_dc:
            self.u.ReleaseDC(None, self.screen_dc)
        self.screen_dc = self.mem_dc = self.bitmap = None
        self.size = None


class MssGrabber:
    """Fallback for other systems: full capture, then sample every few pixels."""

    def __init__(self, sct):
        self.sct = sct

    def grab(self, mon):
        step = max(1, mon["width"] // 160)
        return np.asarray(self.sct.grab(mon))[::step, ::step]

    def close(self):
        pass


# ---------------------------------------------------------------- screen color
def pick_color(frame, saturation_boost=1.3, brightness=1.0, min_level=0.04):
    """frame: HxWx4 BGRA uint8 array -> (r, g, b) floats 0..1"""
    px = frame[..., 2::-1].reshape(-1, 3).astype(np.float32) / 255.0  # BGRA -> RGB

    mx = px.max(axis=1)
    mn = px.min(axis=1)
    sat = (mx - mn) / (mx + 1e-6)

    # Bright, saturated pixels decide the hue; grey/black areas count much less.
    weight = mx ** 2 * (0.1 + sat)
    if weight.sum() < 1e-6:
        return np.zeros(3, dtype=np.float32)
    color = (px * weight[:, None]).sum(axis=0) / weight.sum()

    grey = color.mean()
    color = np.clip(grey + (color - grey) * saturation_boost, 0, None)
    if color.max() < 1e-6:
        return np.zeros(3, dtype=np.float32)
    color /= color.max()

    level = mx.mean() * 1.5   # strip level follows how bright the screen is
    if level < min_level:
        return np.zeros(3, dtype=np.float32)
    level = float(np.clip(level * brightness, 0.0, 1.0))
    return color * level


# ---------------------------------------------------------------- worker
class Worker(threading.Thread):
    """Owns the connection to the strip. The UI only changes settings and wakes it."""

    def __init__(self, cfg, notify):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.notify = notify                 # runs a callable on the UI thread
        self.stop_event = threading.Event()
        self.wake = threading.Event()
        self.runner = AsyncRunner()
        self.link = None
        self.status = "Looking for the strip…"
        self.sync_color = None               # current screen-sync color, for the tray icon
        self.monitor_count = 1
        self.manual_version = 0              # bumped by the UI on every manual change
        self.pushed_version = -1
        self.handoff = False                 # next change is a mode switch: fade slower
        self.usb_failed = {}                 # port -> time of last failed attempt
        self.current = np.zeros(3, dtype=np.float32)
        self.last_values = None
        self.last_send = 0.0
        self.last_mode = None
        self.effect_color = None             # current manual color 0..1 (moves during loops), for the icon
        self.phase = 0.0                     # position around the running color loop, 0..1
        self.last_effect_t = None
        self.restart_effect = False
        self._stops_obj, self._pts = None, None

    # --- called from the UI thread
    def manual_changed(self, handoff=False, restart=False):
        self.manual_version += 1
        if handoff:
            self.handoff = True
        if restart:
            self.restart_effect = True
        self.wake.set()

    def describe(self):
        return self.status if self.link is None else f"Connected over {self.link.label()}"

    # --- connection
    def connect(self):
        usb_note = None
        for port in UsbLink.ports():   # a USB cable wins: faster and needs no radio
            if time.monotonic() - self.usb_failed.get(port, -1e9) < 60:
                continue
            try:
                link = UsbLink(port)
            except serial.SerialException:
                self.usb_failed[port] = time.monotonic()
                usb_note = f"{port} is in use (Arduino Serial Monitor?), using Bluetooth."
                continue
            deadline = time.monotonic() + 3   # the board may still be booting
            while not self.stop_event.is_set():
                try:
                    link.state()
                    self.use(link)
                    return True
                except LINK_ERRORS:
                    if time.monotonic() > deadline:
                        break
            link.close()
            self.usb_failed[port] = time.monotonic()
            usb_note = f"The board on {port} isn't answering over USB, using Bluetooth."

        self.status = usb_note or "Looking for the strip over Bluetooth…"
        try:
            link = BleLink.open(self.runner, self.cfg["ble_address"])
        except LINK_ERRORS as e:
            self.status = str(e)
            return False
        if link.address != self.cfg["ble_address"]:
            self.cfg["ble_address"] = link.address
            self.notify(self.cfg.save)
        self.use(link)
        return True

    def use(self, link):
        logging.info("Connected over %s", link.label())
        self.link = link
        self.pushed_version = -1    # send our setting right away
        self.last_values = None

    def drop_link(self, status):
        if self.link is not None:
            self.link.close()
        self.link = None
        self.status = status

    # --- sending
    def send(self, rgb, brightness, on, fade):
        self.link.send(rgb, brightness, on, fade)
        self.last_send = time.monotonic()
        self.last_values = (tuple(round(float(v), 2) for v in rgb), int(brightness), bool(on))

    def compiled(self, stops):
        if stops is not self._stops_obj:   # the UI swaps in a new look on every change
            self._stops_obj, self._pts = stops, compile_stops(stops)
        return self._pts

    def manual_step(self):
        """Show the manual look: a solid color, or one frame of a running color loop."""
        cfg, version, now = self.cfg, self.manual_version, time.monotonic()
        look, on = cfg["current"], bool(cfg["on"])
        if look["type"] == "loop":
            if self.restart_effect:
                self.phase, self.restart_effect = 0.0, False
            elif on and self.last_effect_t is not None:   # advance by elapsed time, so changing
                dt = min(now - self.last_effect_t, 0.25)  # the loop time never makes it jump
                self.phase = (self.phase + dt / max(0.5, float(look["period"]))) % 1.0
            self.last_effect_t = now if on else None
            rgb = loop_color(self.compiled(look["stops"]), self.phase, look["blend"])
            fade = STEP_FADE if look["blend"] == "steps" else int(2500 / EFFECT_FPS)
            wait = 1.0 / EFFECT_FPS if on else 0.5
        else:
            self.last_effect_t = None
            rgb, fade, wait = hex_rgb(look["color"]), MANUAL_FADE, 0.5
        if self.handoff:
            fade = HANDOFF_FADE
        self.effect_color = tuple(v / 255.0 for v in rgb) if on else None

        values = (tuple(round(float(v), 2) for v in rgb), int(cfg["level"]), on)
        last = self.last_values
        differs = last is None or last[1:] != values[1:] or \
            max(abs(a - b) for a, b in zip(values[0], last[0])) >= 0.1
        if version != self.pushed_version or differs or now - self.last_send > HEARTBEAT_S:
            self.send(rgb, cfg["level"], on, fade)
            self.pushed_version = version
            self.handoff = False
        return wait

    def sync_step(self, sct, grabber):
        cfg = self.cfg
        t0 = time.perf_counter()
        mon = sct.monitors[min(max(1, int(cfg["monitor"])), self.monitor_count)]
        try:
            frame = grabber.grab(mon)   # ~160 px across is plenty for an average
            target = pick_color(frame, cfg["saturation"], cfg["brightness"])
            self.current += (target - self.current) * cfg["smooth"]
        except mss.exception.ScreenShotError:   # e.g. lock screen: keep the last color
            pass
        self.sync_color = tuple(float(v) for v in self.current)

        rgb = tuple(round(float(v) * 255, 2) for v in self.current)
        changed = self.last_values is None or self.last_values[1:] != (255, True) or \
            max(abs(a - b) for a, b in zip(rgb, self.last_values[0])) >= 0.1
        if changed or time.monotonic() - self.last_send > HEARTBEAT_S:
            self.send(rgb, 255, True, HANDOFF_FADE if self.handoff else int(cfg["fade"]))
            self.handoff = False
        return max(0.0, 1.0 / max(1, cfg["fps"]) - (time.perf_counter() - t0))

    # --- main loop
    def run(self):
        try:
            self._run()
        except Exception:
            logging.exception("Worker crashed")
            self.status = f"Error, see {LOG_FILE}"

    def _run(self):
        with mss.mss() as sct:   # used for the monitor list (and capture outside Windows)
            self.monitor_count = len(sct.monitors) - 1
            grabber = GdiGrabber() if sys.platform == "win32" else MssGrabber(sct)
            while not self.stop_event.is_set():
                self.wake.clear()
                if self.link is not None and self.link.lost:
                    self.drop_link("Connection lost, reconnecting…")
                if self.link is None:
                    if not self.connect():
                        self.wake.wait(2)
                    continue

                mode = self.cfg["mode"]
                if mode != self.last_mode:
                    if mode == "sync":   # start sync from the current manual color, not black
                        c = self.cfg
                        base = self.effect_color or tuple(hex_to_rgb01(preview_color(c["current"])))
                        self.current = np.array(base, dtype=np.float32) * (c["level"] / 255.0) \
                            if c["on"] else np.zeros(3, dtype=np.float32)
                    self.last_values = None
                    self.last_mode = mode

                try:
                    if mode == "sync":
                        timeout = self.sync_step(sct, grabber)
                    else:
                        self.sync_color = None
                        timeout = self.manual_step()
                except LINK_ERRORS as e:
                    logging.info("Connection lost: %s", e)
                    self.drop_link("Connection lost, reconnecting…")
                    timeout = 1.0
                except Exception:   # never let one bad moment stop the app for good
                    logging.exception("Unexpected error, reconnecting")
                    self.drop_link("Something went wrong, reconnecting…")
                    timeout = 2.0
                self.wake.wait(timeout)
            grabber.close()

        if self.link is not None:   # quitting: fade the strip off and let go of it
            try:
                rgb = self.last_values[0] if self.last_values else (0, 0, 0)
                self.link.send(rgb, 255, False, OFF_FADE)
            except LINK_ERRORS:
                pass
            self.link.close()


# ---------------------------------------------------------------- tray icon image
def make_icon(color):
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if color is None:   # off or not connected
        fill = (110, 110, 110, 255)
    else:
        c = np.array(color, dtype=np.float32)
        m = c.max()
        c = c / m if m > 0.02 else np.array([0.3, 0.3, 0.3])   # show the hue at full brightness
        fill = tuple(int(v * 255) for v in c) + (255,)
    d.ellipse((4, 4, 60, 60), fill=fill, outline=(255, 255, 255, 235), width=5)
    return img


def work_area():
    """(left, top, right, bottom) of the screen area not covered by the taskbar."""
    if sys.platform == "win32":
        class RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]
        r = RECT()
        if ctypes.windll.user32.SystemParametersInfoW(0x30, 0, ctypes.byref(r), 0):  # SPI_GETWORKAREA
            return r.left, r.top, r.right, r.bottom
    return None


# ---------------------------------------------------------------- preset editor
def flat_button(parent, text, command, kind="normal"):
    bg, fg, hover = {"normal": (PANEL, FG, "#34363c"), "primary": (ACCENT, "#141414", "#ff9a5c"),
                     "danger": (BG, "#ff7b72", PANEL)}[kind]
    b = tk.Label(parent, text=text, bg=bg, fg=fg, font=("Segoe UI", 9), padx=14, pady=5, cursor="hand2")
    b.bind("<Button-1>", lambda e: command())
    b.bind("<Enter>", lambda e: b.config(bg=hover))
    b.bind("<Leave>", lambda e: b.config(bg=bg))
    return b


def segmented(parent, options, command, px):
    """A row of tab-like choices. Returns {value: label} for styling with style_segmented()."""
    frame = tk.Frame(parent, bg=PANEL)
    labels = {}
    for value, text in options:
        label = tk.Label(frame, text=text, font=("Segoe UI", 9), pady=px(4), padx=px(10), cursor="hand2")
        label.pack(side="left", fill="x", expand=True, padx=px(2), pady=px(2))
        label.bind("<Button-1>", lambda e, v=value: command(v))
        labels[value] = label
    return frame, labels


def style_segmented(labels, selected):
    for value, label in labels.items():
        sel = value == selected
        label.config(bg=ACCENT if sel else PANEL, fg="#141414" if sel else MUTED)


# Loop time slider: 0..100 maps to 0.5 s .. 5 min on a log scale (fine control for short loops)
def period_from_slider(v):
    return round(0.5 * 600 ** (float(v) / 100), 1)


def slider_from_period(seconds):
    import math
    return max(0.0, min(100.0, 100 * math.log(max(0.5, seconds) / 0.5) / math.log(600)))


def format_period(seconds):
    if seconds < 10:
        return f"{seconds:.1f} s"
    if seconds < 120:
        return f"{seconds:.0f} s"
    return f"{seconds / 60:.1f} min"


class PresetEditor:
    """Edit or create a preset: a solid color, or a looping gradient like Photoshop's gradient
    editor whose end wraps around to its start. Changes preview live on the strip."""

    BAR_W = 420

    def __init__(self, app, preset=None):
        self.app = app
        px = app.px
        self.snapshot = app.snapshot()          # restored on Cancel
        self.existing = preset is not None
        if preset:
            data = copy.deepcopy(preset)
        else:   # new preset: start from whatever the strip shows now
            n = 1 + sum(1 for p in app.cfg["presets"] if p["name"].startswith("My preset"))
            data = dict(copy.deepcopy(app.cfg["current"]), name=f"My preset {n}", id=None)
        self.pid = data.get("id")
        self.type = data["type"]
        # keep both kinds around, so flipping between Solid and Loop doesn't lose work
        self.solid_color = data.get("color") or preview_color(data)
        rainbow = next(p for p in DEFAULT_PRESETS if p["name"] == "Rainbow")
        self.loop = {"stops": copy.deepcopy(data.get("stops") or rainbow["stops"]),
                     "period": float(data.get("period") or rainbow["period"]),
                     "blend": data.get("blend") or "smooth"}
        self.selected = 0
        self.dragging = self.removing = False
        self.drag_dx = 0
        self.margin = px(9)                     # room for stop markers at the bar's ends
        self.bw, self.bh, self.sh = px(self.BAR_W), px(40), px(26)

        w = self.win = tk.Toplevel(app.root, bg=BG)
        w.title("Edit preset" if self.existing else "New preset")
        w.resizable(False, False)
        w.attributes("-topmost", True)
        w.protocol("WM_DELETE_WINDOW", self.cancel)
        w.bind("<Escape>", lambda e: self.cancel())

        body = tk.Frame(w, bg=BG, padx=px(18), pady=px(16))
        body.pack(fill="both", expand=True)
        label = {"bg": BG, "fg": MUTED, "font": ("Segoe UI", 9)}
        tk.Label(body, text="Name", **label).pack(anchor="w")
        self.name = tk.Entry(body, bg=PANEL, fg=FG, insertbackground=FG, relief="flat", font=("Segoe UI", 10))
        self.name.insert(0, data["name"])
        self.name.pack(fill="x", ipady=px(4), pady=(px(4), px(12)))

        tabs, self.type_tabs = segmented(body, (("solid", "Solid color"), ("loop", "Color loop")),
                                         self.set_type, px)
        tabs.pack(fill="x")
        self.pages = tk.Frame(body, bg=BG)
        self.pages.pack(fill="x")
        self.solid_page = self.build_solid(self.pages)
        self.loop_page = self.build_loop(self.pages)

        self.preview_var = tk.BooleanVar(value=True)
        tk.Checkbutton(body, text="Preview on the strip while editing", variable=self.preview_var,
                       command=self.on_preview_toggle, bg=BG, fg=FG, selectcolor=PANEL, activebackground=BG,
                       activeforeground=FG, font=("Segoe UI", 9), highlightthickness=0, bd=0) \
            .pack(anchor="w", pady=(px(14), 0))

        buttons = tk.Frame(body, bg=BG)
        buttons.pack(fill="x", pady=(px(16), 0))
        if self.existing:
            flat_button(buttons, "Delete", self.delete, "danger").pack(side="left")
        flat_button(buttons, "Save", self.save, "primary").pack(side="right")
        if self.existing:
            flat_button(buttons, "Save as new", lambda: self.save(as_new=True)).pack(side="right", padx=(0, px(8)))
        flat_button(buttons, "Cancel", self.cancel).pack(side="right", padx=(0, px(8)))

        self.set_type(self.type)
        w.update_idletasks()
        x = (w.winfo_screenwidth() - w.winfo_reqwidth()) // 2
        y = (w.winfo_screenheight() - w.winfo_reqheight()) // 3
        w.geometry(f"+{x}+{y}")
        w.focus_force()

    # --- solid
    def build_solid(self, parent):
        px = self.app.px
        page = tk.Frame(parent, bg=BG)
        tk.Label(page, text="Color", bg=BG, fg=MUTED, font=("Segoe UI", 9)).pack(anchor="w", pady=(px(14), px(6)))
        self.solid_swatch = tk.Canvas(page, width=self.bw + 2 * self.margin, height=px(56), bg=BG,
                                      highlightthickness=0, cursor="hand2")
        self.solid_swatch.pack(anchor="w")
        self.solid_swatch.bind("<Button-1>", lambda e: self.choose_solid())
        tk.Label(page, text="Click to choose a color", bg=BG, fg=MUTED, font=("Segoe UI", 8)) \
            .pack(anchor="w", pady=(px(4), 0))
        return page

    def choose_solid(self):
        color = self.ask_color(self.solid_color)
        if color:
            self.solid_color = color
            self.changed()

    # --- loop
    def build_loop(self, parent):
        px = self.app.px
        label = {"bg": BG, "fg": MUTED, "font": ("Segoe UI", 9)}
        page = tk.Frame(parent, bg=BG)
        tk.Label(page, text="Colors (the end loops back to the start)", **label) \
            .pack(anchor="w", pady=(px(14), px(6)))
        self.grad = tk.Canvas(page, width=self.bw + 2 * self.margin, height=self.bh + self.sh, bg=BG,
                              highlightthickness=0, cursor="hand2", takefocus=1)
        self.grad.pack(anchor="w")
        self.bar_img = tk.PhotoImage(width=self.bw, height=self.bh)
        self.grad.create_image(self.margin, 0, anchor="nw", image=self.bar_img)
        self.grad.bind("<Button-1>", self.on_press)
        self.grad.bind("<B1-Motion>", self.on_drag)
        self.grad.bind("<ButtonRelease-1>", self.on_release)
        self.grad.bind("<Double-Button-1>", lambda e: self.choose_stop_color())
        self.grad.bind("<Delete>", lambda e: self.remove_selected())
        self.grad.bind("<BackSpace>", lambda e: self.remove_selected())
        tk.Label(page, text="Click the bar to add a color · drag to move · drag down to remove",
                 bg=BG, fg=MUTED, font=("Segoe UI", 8)).pack(anchor="w", pady=(px(2), 0))

        row = tk.Frame(page, bg=BG)
        row.pack(fill="x", pady=(px(12), 0))
        tk.Label(row, text="Selected", **label).pack(side="left")
        self.stop_swatch = tk.Canvas(row, width=px(34), height=px(20), bg=BG, highlightthickness=1,
                                     highlightbackground="#3a3c42", cursor="hand2")
        self.stop_swatch.pack(side="left", padx=(px(8), px(10)))
        self.stop_swatch.bind("<Button-1>", lambda e: self.choose_stop_color())
        tk.Label(row, text="at", **label).pack(side="left")
        self.pos_var = tk.StringVar()
        self.pos_box = tk.Spinbox(row, from_=0, to=100, increment=1, width=4, textvariable=self.pos_var,
                                  command=self.on_pos_entry, relief="flat", bg=PANEL, fg=FG,
                                  buttonbackground=PANEL, insertbackground=FG, font=("Segoe UI", 9))
        self.pos_box.pack(side="left", padx=(px(6), px(2)))
        self.pos_box.bind("<Return>", lambda e: self.on_pos_entry())
        self.pos_box.bind("<FocusOut>", lambda e: self.on_pos_entry())
        tk.Label(row, text="%", **label).pack(side="left")
        self.remove_btn = flat_button(row, "Remove", self.remove_selected, "danger")
        self.remove_btn.pack(side="right")

        row = tk.Frame(page, bg=BG)
        row.pack(fill="x", pady=(px(14), 0))
        tk.Label(row, text="Loop time", **label).pack(side="left")
        self.period_label = tk.Label(row, text="", bg=BG, fg=FG, font=("Segoe UI", 9))
        self.period_label.pack(side="right")
        self.period_var = tk.DoubleVar(value=slider_from_period(self.loop["period"]))
        tk.Scale(page, from_=0, to=100, resolution=0.5, orient="horizontal", variable=self.period_var,
                 command=self.on_period, showvalue=0, bg=ACCENT, troughcolor=PANEL, activebackground="#ff9a5c",
                 highlightthickness=0, bd=0, sliderrelief="flat", width=px(12), sliderlength=px(20)) \
            .pack(fill="x", pady=(px(4), 0))

        row = tk.Frame(page, bg=BG)
        row.pack(fill="x", pady=(px(14), 0))
        tk.Label(row, text="Blend", **label).pack(side="left")
        blend, self.blend_tabs = segmented(row, (("smooth", "Smooth"), ("steps", "Steps")), self.set_blend, px)
        blend.pack(side="right")
        return page

    def stop_x(self, stop):
        return self.margin + stop["pos"] * self.bw

    def visible_stops(self):
        return [s for i, s in enumerate(self.loop["stops"]) if not (self.removing and i == self.selected)]

    def hit_stop(self, x, y):
        if y < self.bh - self.app.px(2):   # on the bar itself: add a new stop
            return None
        best, best_d = None, self.app.px(9)
        for i, s in enumerate(self.loop["stops"]):
            d = abs(self.stop_x(s) - x)
            if d <= best_d:
                best, best_d = i, d
        return best

    def on_press(self, event):
        self.grad.focus_set()
        stops = self.loop["stops"]
        i = self.hit_stop(event.x, event.y)
        if i is None:   # new stop, colored like the gradient at that spot
            pos = min(max((event.x - self.margin) / self.bw, 0.0), 0.9999)
            color = rgb_hex(loop_color(compile_stops(stops), pos, "smooth"))
            stops.append({"pos": round(pos, 4), "color": color})
            i = len(stops) - 1
        self.selected = i
        self.dragging, self.removing = True, False
        self.drag_dx = self.stop_x(stops[i]) - event.x
        self.changed()

    def on_drag(self, event):
        if not self.dragging:
            return
        stops = self.loop["stops"]
        stop = stops[self.selected]
        stop["pos"] = round(min(max((event.x + self.drag_dx - self.margin) / self.bw, 0.0), 0.9999), 4)
        self.removing = len(stops) > 2 and event.y > self.bh + self.sh + self.app.px(30)
        self.changed()

    def on_release(self, event):
        if not self.dragging:
            return
        self.dragging = False
        if self.removing:
            self.removing = False
            self.remove_selected()
        else:
            self.changed()

    def remove_selected(self):
        stops = self.loop["stops"]
        if len(stops) <= 2 or self.type != "loop":
            return
        del stops[self.selected]
        self.selected = min(self.selected, len(stops) - 1)
        self.changed()

    def choose_stop_color(self):
        stop = self.loop["stops"][self.selected]
        color = self.ask_color(stop["color"])
        if color:
            stop["color"] = color
            self.changed()

    def on_pos_entry(self):
        try:
            pct = float(self.pos_var.get())
        except ValueError:
            return
        stop = self.loop["stops"][self.selected]
        pos = round(min(max(pct / 100, 0.0), 0.9999), 4)
        if pos != stop["pos"]:
            stop["pos"] = pos
            self.changed()

    def on_period(self, _=None):
        self.loop["period"] = period_from_slider(self.period_var.get())
        self.period_label.config(text=format_period(self.loop["period"]))
        self.preview()

    def set_blend(self, blend):
        self.loop["blend"] = blend
        self.changed()

    # --- shared
    def ask_color(self, initial):
        _, color = colorchooser.askcolor(color=initial, title="Choose a color", parent=self.win)
        self.win.focus_force()
        return color.lower() if color else None

    def set_type(self, t):
        self.type = t
        style_segmented(self.type_tabs, t)
        self.solid_page.pack_forget()
        self.loop_page.pack_forget()
        (self.solid_page if t == "solid" else self.loop_page).pack(fill="x")
        self.changed()

    def look(self):
        if self.type == "solid":
            return {"type": "solid", "color": self.solid_color}
        stops = sorted(copy.deepcopy(self.visible_stops()), key=lambda s: s["pos"])
        return {"type": "loop", "stops": stops, "period": self.loop["period"], "blend": self.loop["blend"]}

    def changed(self):
        if self.type == "solid":
            c, h = self.solid_swatch, self.app.px(56)
            c.delete("all")
            c.create_rectangle(self.margin, 0, self.margin + self.bw, h, fill=self.solid_color, outline="")
            c.create_text(self.margin + self.app.px(10), h - self.app.px(10), anchor="sw",
                          text=self.solid_color.upper(), font=("Segoe UI Semibold", 10),
                          fill="#141414" if sum(hex_rgb(self.solid_color)) > 380 else "#ffffff")
        else:
            self.render_loop()
        self.preview()

    def render_loop(self):
        px, stops = self.app.px, self.loop["stops"]
        pts = compile_stops(self.visible_stops())
        blend = self.loop["blend"]
        row = " ".join(rgb_hex(loop_color(pts, (x + 0.5) / self.bw, blend)) for x in range(self.bw))
        self.bar_img.put("{" + row + "}", to=(0, 0, self.bw, self.bh))   # one row, tiled down the bar

        self.grad.delete("marker")
        y0 = self.bh + px(3)
        for i, s in enumerate(stops):
            x, sel = self.stop_x(s), i == self.selected
            points = [x, y0, x + px(7), y0 + px(8), x + px(7), y0 + px(20), x - px(7), y0 + px(20),
                      x - px(7), y0 + px(8)]
            if self.removing and sel:   # about to be removed: show it as an outline
                self.grad.create_polygon(points, fill="", outline=MUTED, width=1, dash=(2, 2), tags="marker")
                continue
            self.grad.create_polygon(points, fill=s["color"], outline=FG if sel else "#5a5d63",
                                     width=px(2) if sel else 1, tags="marker")
            if sel:
                self.grad.create_line(x, 0, x, self.bh, fill="#ffffff", width=1, tags="marker")

        stop = stops[self.selected]
        self.stop_swatch.config(bg=stop["color"])
        if self.win.focus_get() is not self.pos_box:
            self.pos_var.set(f"{stop['pos'] * 100:.0f}")
        self.remove_btn.config(fg="#ff7b72" if len(stops) > 2 else "#55575c")
        self.period_label.config(text=format_period(self.loop["period"]))
        style_segmented(self.blend_tabs, blend)

    def preview(self):
        if self.preview_var.get() if hasattr(self, "preview_var") else False:
            self.app.preview(self.look(), self.pid)

    def on_preview_toggle(self):
        if self.preview_var.get():
            self.preview()
        else:
            self.app.restore(self.snapshot)

    # --- buttons
    def save(self, as_new=False):
        name = self.name.get().strip() or "Untitled"
        preset = dict(self.look(), name=name, id=self.pid)
        self.close()
        self.app.save_preset(preset, as_new=as_new or not self.existing)

    def delete(self):
        if self.app.confirm_delete(self.pid, parent=self.win):
            snap = self.snapshot
            if snap["preset_id"] == self.pid:
                snap["preset_id"] = None
            self.close()
            self.app.restore(snap)

    def cancel(self):
        self.close()
        self.app.restore(self.snapshot)

    def close(self):
        self.win.destroy()
        self.app.editor = None


# ---------------------------------------------------------------- app
class App:
    def __init__(self):
        self.cfg = Settings()
        if self.cfg.first_run:
            set_startup(True)
            self.cfg.save()

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.report_callback_exception = lambda *a: logging.error("Tk error", exc_info=a)
        self.scale = self.root.winfo_fpixels("1i") / 96.0   # HiDPI factor for pixel sizes

        self.calls = queue.Queue()        # tray / worker callbacks, run on the Tk thread
        self.save_job = None
        self.hidden_at = 0.0
        self.icon_key = "unset"
        self.connected = None
        self.closing = False
        self.updating = False             # set while controls are updated from code
        self.page = None
        self.sliders = {}
        self.modal = False                # a dialog/menu is open over the panel: don't auto-hide
        self.editor = None
        self.presets_version = 0          # bumped when presets are added/changed/removed
        self.tiles_drawn = None

        self.worker = Worker(self.cfg, self.calls.put)
        self.worker.start()

        self.popup = self.build_popup()
        self.icon = pystray.Icon("led_strip", make_icon(None), APP_NAME, menu=self.build_menu())
        self.icon.run_detached()
        self.refresh()
        self.root.after(100, self.poll)

    # --- plumbing
    def on_ui(self, fn):
        return lambda icon, item: self.calls.put(fn)

    def px(self, n):
        return int(n * self.scale)

    def schedule_save(self):
        if self.save_job:
            self.root.after_cancel(self.save_job)
        self.save_job = self.root.after(800, self.cfg.save)

    def poll(self):
        while not self.calls.empty():
            self.calls.get_nowait()()
            if self.closing:
                return
        self.update_icon()
        connected = self.worker.link is not None
        if connected != self.connected:
            self.connected = connected
            self.icon.title = f"{APP_NAME}: {'connected' if connected else 'not connected'}"
        if self.popup.winfo_viewable():
            self.status_label.config(text=self.worker.describe())
        self.root.after(150, self.poll)

    # --- state changes
    def is_on(self):
        return self.cfg["mode"] == "sync" or self.cfg["on"]

    def manual(self, save=True, restart=False, **changes):
        was_sync = self.cfg["mode"] == "sync"
        self.cfg["mode"] = "manual"
        for key, value in changes.items():
            self.cfg[key] = value
        self.worker.manual_changed(handoff=was_sync, restart=restart)
        if save:
            self.schedule_save()
        self.refresh()

    def find_preset(self, pid):
        return next((p for p in self.cfg["presets"] if p["id"] == pid), None)

    def apply_preset(self, pid):
        preset = self.find_preset(pid)
        if preset:
            self.manual(current=look_of(preset), preset_id=pid, on=True, restart=True)

    def set_color(self, hex_color):
        look = {"type": "solid", "color": hex_color.lower()}
        pid = next((p["id"] for p in self.cfg["presets"] if look_of(p) == look), None)
        self.manual(current=look, preset_id=pid, on=True)

    # --- presets
    def presets_changed(self):
        self.presets_version += 1
        self.cfg.save()
        self.refresh()

    def save_preset(self, preset, as_new=False):
        preset = copy.deepcopy(preset)
        presets = list(self.cfg["presets"])
        if as_new or not self.find_preset(preset.get("id")):
            preset["id"] = new_id()
            presets.append(preset)
        else:
            presets = [preset if p["id"] == preset["id"] else p for p in presets]
        self.cfg["presets"] = presets
        self.presets_changed()
        self.apply_preset(preset["id"])

    def duplicate_preset(self, pid):
        preset = self.find_preset(pid)
        if not preset:
            return
        dup = dict(copy.deepcopy(preset), id=new_id(), name=f"{preset['name']} copy")
        presets = list(self.cfg["presets"])
        presets.insert(presets.index(preset) + 1, dup)
        self.cfg["presets"] = presets
        self.presets_changed()

    def delete_preset(self, pid):
        self.cfg["presets"] = [p for p in self.cfg["presets"] if p["id"] != pid]
        if self.cfg["preset_id"] == pid:   # keep showing it, it's just not a saved preset any more
            self.cfg["preset_id"] = None
        self.presets_changed()

    def confirm_delete(self, pid, parent=None):
        preset = self.find_preset(pid)
        if not preset:
            return False
        self.modal = True
        try:
            ok = messagebox.askyesno("Delete preset", f"Delete \u201c{preset['name']}\u201d?",
                                     parent=parent or self.popup)
        finally:
            self.modal = False
        if ok:
            self.delete_preset(pid)
        return ok

    def edit_preset(self, pid=None):
        self.hide()
        if self.editor:
            self.editor.win.deiconify()
            self.editor.win.lift()
            self.editor.win.focus_force()
            return
        self.editor = PresetEditor(self, self.find_preset(pid) if pid else None)

    def snapshot(self):
        return {k: copy.deepcopy(self.cfg[k]) for k in ("mode", "current", "preset_id", "on")}

    def restore(self, snap):
        self.cfg["current"], self.cfg["preset_id"], self.cfg["on"] = \
            copy.deepcopy(snap["current"]), snap["preset_id"], snap["on"]
        if snap["mode"] == "sync":
            self.set_mode("sync")
        else:
            self.manual()

    def preview(self, look, pid=None):
        """Show a look on the strip without saving it (used by the editor)."""
        self.manual(save=False, current=look, preset_id=pid, on=True)

    def set_level(self, level):
        self.manual(level=level, on=True)

    def toggle_power(self):
        self.manual(on=not self.is_on())

    def set_mode(self, mode):
        if mode == self.cfg["mode"]:
            return
        if mode == "sync":
            self.cfg["mode"] = "sync"
            self.worker.handoff = True
            self.worker.wake.set()
            self.schedule_save()
            self.refresh()
        else:
            self.manual()

    def toggle_sync(self):
        self.set_mode("manual" if self.cfg["mode"] == "sync" else "sync")

    # --- tray menu
    def build_menu(self):
        def preset(p):
            pid = p["id"]
            return pystray.MenuItem(
                p["name"], self.on_ui(lambda: self.apply_preset(pid)), radio=True,
                checked=lambda item: self.cfg["mode"] == "manual" and self.cfg["on"]
                and self.cfg["preset_id"] == pid)

        def preset_items():   # rebuilt from the saved presets every time the menu opens
            yield from (preset(p) for p in self.cfg["presets"])
            yield pystray.Menu.SEPARATOR
            yield pystray.MenuItem("New preset…", self.on_ui(lambda: self.edit_preset(None)))

        return pystray.Menu(
            pystray.MenuItem("Open panel", self.on_ui(self.toggle_popup), default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Light on", self.on_ui(self.toggle_power), checked=lambda item: self.is_on()),
            pystray.MenuItem("Screen sync", self.on_ui(self.toggle_sync),
                             checked=lambda item: self.cfg["mode"] == "sync"),
            pystray.MenuItem("Presets", pystray.Menu(preset_items)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Start with Windows", self.on_ui(self.toggle_startup),
                             checked=lambda item: startup_enabled()),
            pystray.MenuItem("Quit", self.on_ui(self.quit)),
        )

    def toggle_startup(self):
        set_startup(not startup_enabled())
        self.icon.update_menu()

    def quit(self):
        self.closing = True
        self.cfg.save()
        self.worker.stop_event.set()
        self.worker.wake.set()
        self.worker.join(timeout=8)   # fades the strip off and disconnects
        self.icon.stop()
        self.root.destroy()

    # --- panel
    def build_popup(self):
        px = self.px
        w = tk.Toplevel(self.root, bg=BG, highlightthickness=1, highlightbackground="#3a3c42")
        w.withdraw()
        w.overrideredirect(True)
        w.attributes("-topmost", True)

        body = tk.Frame(w, bg=BG, padx=px(16), pady=px(14))
        body.pack(fill="both", expand=True)

        top = tk.Frame(body, bg=BG)
        top.pack(fill="x")
        tk.Label(top, text=APP_NAME, bg=BG, fg=FG, font=("Segoe UI Semibold", 11)).pack(side="left")
        self.power = tk.Canvas(top, width=px(44), height=px(24), bg=BG, highlightthickness=0, cursor="hand2")
        self.power.pack(side="right")
        self.power.bind("<Button-1>", lambda e: self.toggle_power())

        tabs = tk.Frame(body, bg=PANEL)
        tabs.pack(fill="x", pady=(px(12), 0))
        self.tabs = {}
        for mode, text in (("manual", "Manual"), ("sync", "Screen sync")):
            label = tk.Label(tabs, text=text, font=("Segoe UI", 9), pady=px(4), cursor="hand2")
            label.pack(side="left", fill="x", expand=True, padx=px(2), pady=px(2))
            label.bind("<Button-1>", lambda e, m=mode: self.set_mode(m))
            self.tabs[mode] = label

        pages = tk.Frame(body, bg=BG)
        pages.pack(fill="x")
        self.pages = {"manual": self.build_manual(pages), "sync": self.build_sync(pages)}

        self.status_label = tk.Label(body, text="", bg=BG, fg=MUTED, font=("Segoe UI", 8),
                                     anchor="w", justify="left", wraplength=px(270))
        self.status_label.pack(fill="x", pady=(px(14), 0))

        w.bind("<Escape>", lambda e: self.hide())
        w.bind("<FocusOut>", lambda e: w.after(80, self.hide_if_unfocused), add="+")
        return w

    def build_manual(self, parent):
        px = self.px
        page = tk.Frame(parent, bg=BG)
        head = tk.Frame(page, bg=BG)
        head.pack(fill="x", pady=(px(12), px(6)))
        tk.Label(head, text="Presets", bg=BG, fg=MUTED, font=("Segoe UI", 9)).pack(side="left")
        self.hover_label = tk.Label(head, text="", bg=BG, fg=FG, font=("Segoe UI", 9))
        self.hover_label.pack(side="right")

        self.tile_size, self.tile_gap, self.tile_cols = px(34), px(11), 6
        self.tiles = tk.Canvas(page, width=self.tile_cols * self.tile_size + (self.tile_cols - 1) * self.tile_gap,
                               height=self.tile_size, bg=BG, highlightthickness=0, cursor="hand2")
        self.tiles.pack(anchor="w")
        self.tile_rings = {}

        row = tk.Frame(page, bg=BG)
        row.pack(fill="x", pady=(px(10), 0))
        custom = tk.Label(row, text="Custom color…", bg=BG, fg=ACCENT, font=("Segoe UI", 9), cursor="hand2")
        custom.pack(side="left")
        custom.bind("<Button-1>", lambda e: self.pick_custom())
        self.color_value = tk.Label(row, text="", bg=BG, fg=MUTED, font=("Segoe UI", 9))
        self.color_value.pack(side="right")
        self.color_chip = tk.Frame(row, width=px(12), height=px(12), bg=BG)
        self.color_chip.pack(side="right", padx=(0, px(6)))

        self.slider(page, "level", "Brightness", 1, 255, 1, lambda v: f"{v / 2.55:.0f}%",
                    integer=True, on_change=self.set_level)
        tk.Label(page, text="Double-click a preset to edit it, right-click for more.", bg=BG, fg=MUTED,
                 font=("Segoe UI", 8)).pack(anchor="w", pady=(px(10), 0))
        return page

    def draw_tiles(self):
        """Preset tiles: solid colors as dots, color loops as little color wheels, then a + tile."""
        c, px = self.tiles, self.px
        size, gap, cols = self.tile_size, self.tile_gap, self.tile_cols
        c.delete("all")
        self.tile_rings = {}
        presets = self.cfg["presets"]
        rows = len(presets) // cols + 1
        c.config(height=rows * size + (rows - 1) * gap)
        inset = px(4)
        for i, p in enumerate(presets + [None]):
            x, y = (i % cols) * (size + gap), (i // cols) * (size + gap)
            box = (x + inset, y + inset, x + size - inset, y + size - inset)
            tag = f"tile{i}"
            if p is None:
                c.create_oval(*box, outline=MUTED, width=1, dash=(3, 3), tags=tag)
                c.create_text(x + size / 2, y + size / 2 - 1, text="+", fill=MUTED,
                              font=("Segoe UI", 13), tags=tag)
                c.create_oval(x + 1, y + 1, x + size - 1, y + size - 1, outline="", fill="", tags=tag)
                c.tag_bind(tag, "<Button-1>", lambda e: self.edit_preset(None))
                name = "New preset…"
            else:
                if p["type"] == "solid":
                    c.create_oval(*box, fill=p["color"], outline="", tags=tag)
                else:
                    pts, n = compile_stops(p["stops"]), 36
                    for k in range(n):
                        col = rgb_hex(loop_color(pts, (k + 0.5) / n, p["blend"]))
                        c.create_arc(*box, start=90 - (k + 1) * 360 / n, extent=360 / n,
                                     fill=col, outline=col, style="pieslice", tags=tag)
                self.tile_rings[p["id"]] = c.create_oval(x + 1, y + 1, x + size - 1, y + size - 1,
                                                         outline="", width=px(2), tags=tag)
                c.tag_bind(tag, "<Button-1>", lambda e, pid=p["id"]: self.apply_preset(pid))
                c.tag_bind(tag, "<Double-Button-1>", lambda e, pid=p["id"]: self.edit_preset(pid))
                c.tag_bind(tag, "<Button-3>", lambda e, pid=p["id"]: self.tile_menu(e, pid))
                name = p["name"]
            c.tag_bind(tag, "<Enter>", lambda e, t=name: self.hover_label.config(text=t))
            c.tag_bind(tag, "<Leave>", lambda e: self.hover_label.config(text=""))

    def tile_menu(self, event, pid):
        menu = tk.Menu(self.popup, tearoff=0)
        menu.add_command(label="Edit…", command=lambda: self.edit_preset(pid))
        menu.add_command(label="Duplicate", command=lambda: self.duplicate_preset(pid))
        menu.add_separator()
        menu.add_command(label="Delete", command=lambda: self.root.after(1, lambda: self.confirm_delete(pid)))
        self.modal = True
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
            self.root.after(300, self.end_modal)

    def end_modal(self):
        self.modal = False
        if self.popup.winfo_viewable():   # take focus back, so clicking elsewhere closes the panel
            self.popup.focus_force()

    def build_sync(self, parent):
        px = self.px
        page = tk.Frame(parent, bg=BG)
        self.slider(page, "brightness", "Brightness", 0.1, 3.0, 0.05, lambda v: f"×{v:.2f}")
        self.slider(page, "saturation", "Color boost", 0.5, 2.5, 0.05, lambda v: f"×{v:.2f}")
        self.slider(page, "smooth", "Responsiveness", 0.05, 1.0, 0.05, lambda v: f"{v * 100:.0f}%")
        self.slider(page, "fade", "Strip fade", 0, 1000, 10, lambda v: f"{v:.0f} ms", integer=True)
        self.slider(page, "fps", "Update rate", 5, 60, 1, lambda v: f"{v:.0f} fps", integer=True)

        row = tk.Frame(page, bg=BG)
        row.pack(fill="x", pady=(px(12), 0))
        tk.Label(row, text="Monitor", bg=BG, fg=MUTED, font=("Segoe UI", 9)).pack(side="left")
        self.monitor_var = tk.StringVar(value=str(self.cfg["monitor"]))
        self.monitor_box = tk.Spinbox(row, from_=1, to=1, width=3, textvariable=self.monitor_var,
                                      command=self.on_monitor, state="readonly", relief="flat",
                                      readonlybackground=PANEL, fg=FG, buttonbackground=PANEL,
                                      font=("Segoe UI", 9))
        self.monitor_box.pack(side="right")
        return page

    def slider(self, parent, key, label, lo, hi, res, fmt, integer=False, on_change=None):
        px = self.px
        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x", pady=(px(12), 0))
        tk.Label(row, text=label, bg=BG, fg=MUTED, font=("Segoe UI", 9)).pack(side="left")
        value = tk.Label(row, text=fmt(self.cfg[key]), bg=BG, fg=FG, font=("Segoe UI", 9))
        value.pack(side="right")
        var = tk.DoubleVar(value=self.cfg[key])

        def changed(_=None):
            if self.updating:
                return
            v = int(round(var.get())) if integer else round(var.get(), 3)
            value.config(text=fmt(v))
            if on_change:
                on_change(v)
            else:
                self.cfg[key] = v
                self.schedule_save()

        tk.Scale(parent, from_=lo, to=hi, resolution=res, orient="horizontal", variable=var,
                 command=changed, showvalue=0, bg=ACCENT, fg=FG, troughcolor=PANEL,
                 activebackground="#ff9a5c", highlightthickness=0, bd=0, sliderrelief="flat",
                 length=px(270), width=px(12), sliderlength=px(20)).pack(fill="x", pady=(px(4), 0))
        self.sliders[key] = (var, value, fmt)

    def draw_power(self, on):
        c, px = self.power, self.px
        c.delete("all")
        w, h = px(44), px(24)
        track = ACCENT if on else "#3a3c42"
        c.create_oval(0, 0, h, h, fill=track, outline="")
        c.create_oval(w - h, 0, w, h, fill=track, outline="")
        c.create_rectangle(h / 2, 0, w - h / 2, h, fill=track, outline="")
        x = w - h if on else 0
        c.create_oval(x + px(3), px(3), x + h - px(3), h - px(3), fill="#ffffff", outline="")

    def refresh(self):
        """Bring the panel, tray icon and menu in line with the settings."""
        cfg = self.cfg
        self.updating = True
        try:
            mode = cfg["mode"]
            for m, label in self.tabs.items():
                sel = m == mode
                label.config(bg=ACCENT if sel else PANEL, fg="#141414" if sel else MUTED)
            if self.page != mode:
                for page in self.pages.values():
                    page.pack_forget()
                self.pages[mode].pack(fill="x")
                self.page = mode
            self.draw_power(self.is_on())

            if self.tiles_drawn != self.presets_version:
                self.draw_tiles()
                self.tiles_drawn = self.presets_version
            active = cfg["preset_id"] if mode == "manual" and cfg["on"] else None
            for pid, ring in self.tile_rings.items():
                self.tiles.itemconfig(ring, outline=FG if pid == active else "")
            look, preset = cfg["current"], self.find_preset(cfg["preset_id"])
            if look["type"] == "solid":
                text = f"{preset['name']} · {look['color'].upper()}" if preset else look["color"].upper()
            else:
                text = preset["name"] if preset else "Unsaved color loop"
            self.color_value.config(text=text)
            self.color_chip.config(bg=preview_color(look))

            for key, (var, value, fmt) in self.sliders.items():
                if var.get() != cfg[key]:
                    var.set(cfg[key])
                    value.config(text=fmt(cfg[key]))
        finally:
            self.updating = False
        if self.popup.winfo_viewable():
            self.place_popup()
        self.update_icon()
        self.icon.update_menu()

    def update_icon(self):
        if self.worker.link is None:
            color = None
        elif self.cfg["mode"] == "sync":
            color = self.worker.sync_color
        elif not self.cfg["on"]:
            color = None
        else:
            color = self.worker.effect_color or tuple(hex_to_rgb01(preview_color(self.cfg["current"])))
        key = None if color is None else tuple(round(float(v), 1) for v in color)
        if key != self.icon_key:
            self.icon_key = key
            self.icon.icon = make_icon(color)

    def pick_custom(self):
        self.modal = True
        try:
            _, hex_color = colorchooser.askcolor(color=preview_color(self.cfg["current"]),
                                                 title="Strip color", parent=self.popup)
        finally:
            self.modal = False
        if hex_color:
            self.set_color(hex_color)
        if self.popup.winfo_viewable():
            self.popup.focus_force()

    def on_monitor(self):
        self.cfg["monitor"] = int(self.monitor_var.get())
        self.schedule_save()

    def place_popup(self):
        self.popup.update_idletasks()
        pw, ph = self.popup.winfo_reqwidth(), self.popup.winfo_reqheight()
        area = work_area() or (0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight())
        margin = self.px(12)
        self.popup.geometry(f"+{area[2] - pw - margin}+{area[3] - ph - margin}")

    def toggle_popup(self):
        if self.popup.winfo_viewable():
            self.hide()
            return
        if time.monotonic() - self.hidden_at < 0.4:   # this click is the one that just closed it
            return
        count = max(1, self.worker.monitor_count)
        self.monitor_box.config(to=count)
        self.monitor_var.set(str(min(self.cfg["monitor"], count)))
        self.status_label.config(text=self.worker.describe())
        self.refresh()
        self.place_popup()
        self.popup.deiconify()
        self.popup.lift()
        self.popup.focus_force()

    def hide_if_unfocused(self):
        if self.modal:
            return
        if self.popup.winfo_viewable() and self.popup.focus_get() is None:
            self.hide()

    def hide(self):
        if self.popup.winfo_viewable():
            self.popup.withdraw()
            self.hidden_at = time.monotonic()


# ---------------------------------------------------------------- main
def main():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=LOG_FILE, level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    sys.excepthook = lambda *a: logging.critical("Unhandled error", exc_info=a)

    if sys.platform == "win32":
        # only one copy at a time (e.g. started at login and again by hand)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW(None, False, "LEDScreenSync_single_instance")
        if ctypes.get_last_error() == 183:   # ERROR_ALREADY_EXISTS
            return
        try:   # sharp text on HiDPI screens
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()

    logging.info("Starting %s", APP_NAME)
    app = App()
    app.root.mainloop()


if __name__ == "__main__":
    main()
