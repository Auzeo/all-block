#!/usr/bin/env python3
"""
All Block for Windows — Website & App Blocker
────────────────────────────────────────────
Install:  pip install customtkinter psutil pillow
Run:      python AllBlock.py   (right-click → Run as Administrator for website blocking)
"""

import customtkinter as ctk
import tkinter as tk
import psutil
import threading
import time
import json
import os
import sys
import ctypes
from ctypes import wintypes
import winreg
import subprocess
import math
import random
import io
import wave
import struct
from datetime import datetime, timedelta
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageTk, ImageGrab
import threading

try:
    # winsound.PlaySound opens and tears down the audio device on every single
    # call — for short, frequent UI ticks (fast wheel scrolling) that open/close
    # transient itself is audible as a click/pop, regardless of the waveform.
    # pygame's mixer opens the device once and stays open, so playback is just
    # a buffer swap — no per-call device churn, no pop.
    os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
    import pygame
    # buffer: 1024 frames (~23ms). 256 was tight enough that any stall on Tk's
    # main thread starved the mixer, and an underrun is itself audible as a click.
    pygame.mixer.pre_init(frequency=44100, size=-16, channels=1, buffer=1024)
    pygame.mixer.init()
except Exception:
    pygame = None

# ─── Constants ────────────────────────────────────────────────────────────────

APP_NAME   = "All Block"
VERSION    = "7.0.0"
APP_AUMID  = "IFM.AllBlock.Focus"   # stable taskbar identity (shared icon grouping)
DEV_FORCE_SETUP_WIZARD = False   # TEMP dev convenience: shows the wizard every launch instead of once. Set False when done testing.
HOSTS_FILE = r"C:\Windows\System32\drivers\etc\hosts"
REDIRECT   = "127.0.0.1"
MARKER     = "# All Block"
DATA_FILE  = Path.home() / ".allblock" / "data.json"
SESSION_STATE_FILE = Path.home() / ".allblock" / "session_state.json"

def set_dpi_awareness():
    """Tell Windows we render at native resolution. Without this the OS bitmap-
    stretches the whole window on high-DPI / scaled displays, which is exactly
    why the full-screen lock text looked soft and jagged. Must run before any
    Tk window exists."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # PROCESS_PER_MONITOR_DPI_AWARE
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()        # older Windows fallback
    except Exception:
        pass

def raise_timer_resolution():
    """Windows' default scheduler tick is ~15.6ms, so Tk's after(8, ...) calls
    used by the wheel's ease loop actually fire in irregular ~15-31ms bursts
    instead of a steady 8ms — that's what reads as scroll jitter. Requesting
    a 1ms multimedia timer resolution makes after() land on schedule."""
    try:
        ctypes.windll.winmm.timeBeginPeriod(1)
    except Exception:
        pass

def primary_screen_size():
    """Return the PRIMARY monitor's (width, height) via GetSystemMetrics,
    which — unlike Tk's winfo_screenwidth/height — always refers to the
    OS-designated primary display regardless of which monitor the window
    currently sits on, so window centering is reliable on multi-monitor
    setups."""
    try:
        SM_CXSCREEN, SM_CYSCREEN = 0, 1
        user32 = ctypes.windll.user32
        return user32.GetSystemMetrics(SM_CXSCREEN), user32.GetSystemMetrics(SM_CYSCREEN)
    except Exception:
        return None


def sync_titlebar_dark_mode(win, is_dark):
    """Set the native Windows titlebar's dark/light color directly via DWM,
    without customtkinter's own _windows_set_titlebar_color(), which wraps
    the same DWM call in a withdraw()/deiconify() cycle (a real window
    unmap+remap) to force older Windows builds to redraw. On any Windows
    version new enough to run this app that hide/show is unnecessary and
    was the actual source of the post-transition flash — DWM applies the
    attribute live."""
    try:
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        value = ctypes.c_int(1 if is_dark else 0)
        DWMWA_USE_IMMERSIVE_DARK_MODE = 20
        DWMWA_USE_IMMERSIVE_DARK_MODE_BEFORE_20H1 = 19
        if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, ctypes.byref(value), ctypes.sizeof(value)) != 0:
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE_BEFORE_20H1, ctypes.byref(value), ctypes.sizeof(value))
    except Exception:
        pass

# ─── Theme palettes ───────────────────────────────────────────────────────────
# LIGHT = the original WOVE-inspired near-monochrome. DARK ≈ the reactbits
# OptionWheel demo (near-black stage, white active text, muted-grey resting).
# C_INK is a *text/ink* colour (near-white in dark); primary buttons use C_BTN
# so they stay a dark surface with a white glyph in both themes.
_PALETTES = {
    "light": dict(
        C_BG="#fafafa", C_CARD="#ffffff", C_CARD2="#ece8de",
        C_INK="#18181a", C_INK2="#47454a", C_BTN="#18181a", C_BTN2="#47454a",
        C_GHOST="#ddd7c9", C_LINE="#e6e1d4", C_MUTED="#68646a", C_FAINT="#9a9590",
        C_GREEN="#1f9d55", C_AMBER="#b8790a", C_RED="#cf3b2e", C_BLUE="#3568c9", C_BORDER="#e3ddcf",
    ),
    "dark": dict(
        C_BG="#0b0b0d", C_CARD="#151518", C_CARD2="#1e1e22",
        C_INK="#f5f5f7", C_INK2="#d6d6da", C_BTN="#26262b", C_BTN2="#313138",
        C_GHOST="#3a3a40", C_LINE="#2a2a30", C_MUTED="#a6a6ad", C_FAINT="#75757e",
        C_GREEN="#4cc479", C_AMBER="#d8a13a", C_RED="#e05a4d", C_BLUE="#5b8ff0", C_BORDER="#2a2a30",
    ),
}
THEME = "light"

def apply_palette(name: str):
    """Swap every C_* colour global in place so call-time colour look-ups pick up
    the new theme; widgets are then rebuilt to repaint with it."""
    global THEME
    THEME = name if name in _PALETTES else "light"
    globals().update(_PALETTES[THEME])

def detect_system_theme() -> str:
    """Read the current Windows light/dark app theme setting. Falls back to
    'light' if the key is missing or unreadable (e.g. non-Windows, older OS)."""
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        winreg.CloseKey(key)
        return "light" if value else "dark"
    except Exception:
        return "light"

apply_palette("light")   # seed the C_* globals before anything below reads them

MIN_MIN, MAX_MIN = 1, 180          # duration range, 1-minute floor
DEFAULT_MIN      = 1     # testing: starts at the 1-minute floor (raise to 45 for normal use)
MAJOR_TICKS      = [1, 15, 30, 45, 60, 90, 120, 150, 180]
A_TOP, A_BOT     = 34.0, -34.0     # arc angular span (degrees) — legacy dial
APEX_X, BIG_X    = 0.37, 0.63      # horizontal anchors (fraction of width) — legacy dial

# Duration presets for the OptionWheel picker: (label, seconds)
WHEEL_PRESETS = [
    ("10 sec",  10),
    ("1 min",   60),
    ("5 min",   300),
    ("10 min",  600),
    ("15 min",  900),
    ("20 min",  1200),
    ("25 min",  1500),
    ("30 min",  1800),
    ("45 min",  2700),
    ("1 hour",  3600),
    ("1½ hour", 5400),
    ("2 hours", 7200),
    ("3 hours", 10800),
]
WHEEL_DEFAULT_IDX = 3   # "10 min" — default duration

# ─── Scheduler / plan gating ──────────────────────────────────────────────────

DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]   # index == datetime.weekday()
# sub-minute presets make no sense for an unattended recurring block
SCHEDULE_DURATIONS = [(lbl, secs) for lbl, secs in WHEEL_PRESETS if secs >= 300]
SCHEDULE_DEFAULT_IDX = 5          # "30 min"
SCHEDULE_GRACE_S = 180            # only auto-fire within 3 min of the start time, so
                                  # opening the app hours later can't spring a stale block
SCHEDULE_LIMIT = 3                # hard cap on how many schedules a user can keep
MAX_VISIBLE_SCHEDULE_ROWS = 3     # hard ceiling on rendered rows — the card is centred and
                                  # grows with its content, so beyond this it runs off the
                                  # window. Matches SCHEDULE_LIMIT so every schedule a user
                                  # owns is always visible (and deletable); the "+N more"
                                  # line should never actually come up.
# OptionWheel look/feel (ported from the reactbits.dev component)
WHEEL_TILT   = 6.0      # degrees between neighbouring options
WHEEL_CURVE  = 1.0      # depth of the curve (0 = flat list)
WHEEL_FADE   = 0.30     # colour/opacity lost per step from the middle
WHEEL_TAU    = 0.06     # easing time constant (s) — lower = snappier, higher = heavier
WHEEL_BLUR   = 1.0      # px of gaussian blur per step from the middle

# ─── Easing & color math ────────────────────────────────────────────────────────

def _ease_out_cubic(t: float) -> float:
    return 1 - (1 - t) ** 3

def _ease_in_out(t: float) -> float:
    return 4 * t * t * t if t < 0.5 else 1 - (-2 * t + 2) ** 3 / 2

def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t

def _hex(c: str):
    c = c.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)

def _lerp_color(c1: str, c2: str, t: float) -> str:
    t = max(0.0, min(1.0, t))
    r1, g1, b1 = _hex(c1)
    r2, g2, b2 = _hex(c2)
    return f"#{int(_lerp(r1,r2,t)):02x}{int(_lerp(g1,g2,t)):02x}{int(_lerp(b1,b2,t)):02x}"

def animate(widget, ms, on_frame, on_done=None):
    """Run on_frame(t) with raw t in [0,1] at ~60fps using perf_counter for smoothness."""
    start = time.perf_counter()
    def frame():
        try:
            if not widget.winfo_exists():
                return
        except Exception:
            return
        t = min(1.0, (time.perf_counter() - start) * 1000.0 / ms)
        on_frame(t)
        if t < 1.0:
            widget.after(15, frame)
        elif on_done:
            on_done()
    frame()

_MODAL_SLIDE = 0.035   # how far below center a modal starts, as a rely fraction

def _animate_modal_in(card):
    """Slide a modal card up into place instead of snapping onto the screen
    at once. Resizing a CTkFrame via pack_propagate(False) + configure(width=,
    height=) was tried first, but fights CTk's own DPI-scaling pass on the
    frame and blows its size up to something screen-sized — so this animates
    position only (a few percent below center, easing up to center), which
    doesn't touch size at all and reads just as smooth.

    The card must already be placed at this same start offset (see
    _MODAL_SLIDE) by the caller before its contents are built — building all
    the child widgets (icons, switches, etc.) takes several event-loop turns
    and can force an early partial redraw at wherever the card is *currently*
    placed. If that placement were the final centered spot, the modal would
    flash fully-formed at rest, then jump down to the start offset and slide
    back up — the "glitchy" snap the animation was supposed to avoid."""
    def frame(t):
        if not card.winfo_exists():
            return
        e = _ease_out_cubic(t)
        rely = 0.5 + _MODAL_SLIDE * (1 - e)
        card.place(relx=0.5, rely=rely, anchor="center")
    def done():
        if card.winfo_exists():
            card.place(relx=0.5, rely=0.5, anchor="center")
    animate(card, 140, frame, done)

def _hover_track(widget, on_true, on_false):
    """Fire on_true once when the pointer genuinely enters `widget` and on_false
    once when it genuinely leaves. CTkButton is really three stacked sub-widgets
    (canvas + text label + image label), each with its own <Enter>/<Leave>
    events, so a naive bind re-fires every time the cursor crosses one of their
    internal seams while gliding across the button — this debounces that down
    to a single call per real entry/exit, checking who's actually under the
    pointer before treating a <Leave> as final."""
    state = {"hover": False}
    def on_enter(_e=None):
        if not state["hover"]:
            state["hover"] = True
            on_true()
    def on_leave(_e=None):
        def recheck():
            try:
                x, y = widget.winfo_pointerxy()
                under = widget.winfo_containing(x, y)
            except Exception:
                under = None
            still_inside = under is not None and str(under).startswith(str(widget))
            if not still_inside:
                state["hover"] = False
                on_false()
        widget.after(1, recheck)
    widget.bind("<Enter>", on_enter, add="+")
    widget.bind("<Leave>", on_leave, add="+")

def smooth_hover(btn, base, hover, ms=140):
    """Animate a CTkButton's fill smoothly on hover instead of snapping."""
    def to(target):
        frm = base if target == hover else hover
        animate(btn, ms, lambda t: btn.configure(fg_color=_lerp_color(frm, target, _ease_out_cubic(t))))
    _hover_track(btn, lambda: to(hover), lambda: to(base))

def bind_hover_sfx(btn, sound):
    """Play a sound once per genuine hover (see _hover_track)."""
    _hover_track(btn, lambda: play_sfx(sound), lambda: None)

def bind_press_sfx(btn, sound=None):
    """Play the click 'thock' the moment the button is pressed.

    A CTkButton is a frame wrapping a canvas + label — but CTkButton already
    forwards <Button-1> from those children up to the frame itself so the
    whole thing acts as one clickable unit. Binding on every sub-widget (as
    this used to) double-fired: once from the child that was actually under
    the cursor, once more from that same click bubbling to the frame. One
    binding, on the frame alone, is all that's needed."""
    def fire(_e=None):
        try:
            if str(btn.cget("state")) == "disabled":
                return
        except Exception:
            pass
        play_sfx(_SFX_BTN_CLICK if sound is None else sound)

    try:
        btn.bind("<Button-1>", fire, add="+")
    except Exception:
        pass

def _make_focus_button(fill: str, glyph: str, text: str, kind: str = "play"):
    """Simple focus button — a clean inverted-ink pill (C_INK fill, C_BG glyph).
    No glow, sheen, or shadow effects. Regenerated on theme switch."""
    bw, bh = 300, 76
    radius = bh // 2
    pad = 14
    iw, ih = bw + pad * 2, bh + pad * 2
    fpath = _font_file("bold")
    font = ImageFont.truetype(fpath, 20) if fpath else ImageFont.load_default()
    s = 22.0
    dark = THEME != "light"

    def build(f, border, hl):
        img = Image.new("RGBA", (iw, ih), (0, 0, 0, 0))
        body = Image.new("RGBA", (iw, ih), (0, 0, 0, 0))
        ImageDraw.Draw(body).rounded_rectangle(
            [pad, pad, pad + bw, pad + bh], radius=radius,
            fill=f, outline=border, width=2)
        # subtle top highlight
        if hl:
            sheen = Image.new("RGBA", (iw, ih), (0, 0, 0, 0))
            ImageDraw.Draw(sheen).rounded_rectangle(
                [pad + 1, pad + 1, pad + bw - 1, pad + int(bh * 0.45)], radius=radius,
                fill=(255, 255, 255, hl))
            sheen = sheen.filter(ImageFilter.GaussianBlur(2))
            m = Image.new("L", (iw, ih), 0)
            ImageDraw.Draw(m).rounded_rectangle(
                [pad, pad, pad + bw, pad + bh], radius=radius, fill=255)
            sheen = Image.composite(sheen, Image.new("RGBA", (iw, ih), (0, 0, 0, 0)), m)
            body = Image.alpha_composite(body, sheen)
        img = Image.alpha_composite(img, body)
        # glyphs
        d = ImageDraw.Draw(img)
        if kind == "play":
            icon_w = s * 0.88
        else:
            icon_w = s * 0.84
        bbox = d.textbbox((0, 0), text, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        gap = 14
        total = icon_w + gap + tw
        cx0 = (iw - total) / 2
        gy = ih / 2
        icx = cx0 + icon_w / 2
        if kind == "play":
            d.polygon([(icx - s * 0.38, gy - s * 0.5), (icx - s * 0.38, gy + s * 0.5),
                       (icx + s * 0.5, gy)], fill=glyph)
        else:
            d.rounded_rectangle([icx - s * 0.42, gy - s * 0.42, icx + s * 0.42, gy + s * 0.42],
                                radius=s * 0.22, fill=glyph)
        ty = gy - th / 2 - bbox[1]
        d.text((cx0 + icon_w + gap, ty), text, font=font, fill=glyph)
        return img

    if dark:
        normal  = build(fill, _lerp_color(fill, "#000000", 0.25), 20)
        hover   = build(_lerp_color(fill, "#ffffff", 0.08), _lerp_color(fill, "#000000", 0.15), 28)
        pressed = build(_lerp_color(fill, "#000000", 0.10), _lerp_color(fill, "#000000", 0.30), 10)
    else:
        normal  = build(fill, _lerp_color(fill, "#ffffff", 0.30), 16)
        hover   = build(_lerp_color(fill, "#ffffff", 0.06), _lerp_color(fill, "#ffffff", 0.35), 24)
        pressed = build(_lerp_color(fill, "#000000", 0.10), _lerp_color(fill, "#000000", 0.06), 8)
    return {"normal": normal, "hover": hover, "pressed": pressed}

# ─── Sound (synthesised soft "tick") ────────────────────────────────────────────

def _make_click(freq=1500, ms=16, vol=0.32, rate=44100) -> bytes:
    n = int(rate * ms / 1000)
    frames = bytearray()
    for i in range(n):
        env = (1 - i / n) ** 2                       # fast percussive decay
        s = math.sin(2 * math.pi * freq * i / rate) * env * vol
        frames += struct.pack("<h", int(s * 32767))
    buf = io.BytesIO()
    wv = wave.open(buf, "wb")
    wv.setnchannels(1); wv.setsampwidth(2); wv.setframerate(rate)
    wv.writeframes(bytes(frames))
    wv.close()
    return buf.getvalue()

_CLICK = _make_click()
_CLICK_SOFT = _make_click(freq=1150, ms=14, vol=0.22)

def _make_wheel_tick(pitch=1.0, ms=18, vol=0.30, rate=44100) -> bytes:
    """Soft felt detent — a warm, round tap with no metallic ring.

    Both ends are pinned to true silence: a ~2ms fade-in and a ~3ms fade-out
    on top of the decay. A waveform that starts or stops at non-zero amplitude
    is a step discontinuity, and that is what a speaker reproduces as a click,
    so the envelope matters more here than the tone does."""
    n = int(rate * ms / 1000)
    freq = 620 * pitch
    attack_n = max(1, int(rate * 0.002))
    release_n = max(1, int(rate * 0.003))
    frames = bytearray()
    for i in range(n):
        ti = i / rate
        s = math.sin(2 * math.pi * freq * ti) * 0.85
        s += math.sin(2 * math.pi * freq * 2.0 * ti) * 0.15   # slight body, no ring
        s *= math.exp(-ti * 260)                              # gentle percussive decay
        s *= min(1.0, i / attack_n)                           # fade in from zero
        if i > n - release_n:                                 # fade out to zero
            s *= (n - i) / release_n
        s *= vol
        frames += struct.pack("<h", int(max(-1.0, min(1.0, s)) * 32767))
    buf = io.BytesIO()
    wv = wave.open(buf, "wb")
    wv.setnchannels(1); wv.setsampwidth(2); wv.setframerate(rate)
    wv.writeframes(bytes(frames))
    wv.close()
    return buf.getvalue()

def _make_sfx_cancel(freqs=(700, 440), ms=90, rate=44100) -> bytes:
    """Nintendo-style two-tone descending blip — for stopping/interrupting a
    session before it finishes."""
    half = ms // 2
    frames = bytearray()
    for f in freqs:
        n = int(rate * half / 1000)
        for i in range(n):
            env = 1.0 - (i / max(1, n))
            sq = 1.0 if (i * f / rate) % 1.0 < 0.5 else -1.0
            frames += struct.pack("<h", int(sq * env * 0.20 * 32767))
    buf = io.BytesIO()
    wv = wave.open(buf, "wb")
    wv.setnchannels(1); wv.setsampwidth(2); wv.setframerate(rate)
    wv.writeframes(bytes(frames))
    wv.close()
    return buf.getvalue()

def _make_wood_tap(f0=190, ms=32, vol=0.34, noise_amt=0.28, decay_rate=8.0, rate=44100) -> bytes:
    """Soft felt-mallet-style knock: a low sine body (with a hint of its own
    downward pitch bend, like a real strike) plus a brief noise transient at
    the very start for the 'knock' attack — organic rather than a pure tone."""
    n = int(rate * ms / 1000)
    frames = bytearray()
    for i in range(n):
        t = i / n
        freq = f0 * (1 - 0.15 * t)
        body = math.sin(2 * math.pi * freq * i / rate)
        body += math.sin(2 * math.pi * freq * 2.4 * i / rate) * 0.22   # wood-like partial
        noise = (random.random() * 2 - 1) * math.exp(-t * 42) * noise_amt
        attack = min(1.0, i / max(1, n * 0.02))
        decay = math.exp(-t * decay_rate)
        env = attack * decay * vol
        s = (body * 0.72 + noise) * env
        frames += struct.pack("<h", int(max(-1.0, min(1.0, s)) * 32767))
    buf = io.BytesIO()
    wv = wave.open(buf, "wb")
    wv.setnchannels(1); wv.setsampwidth(2); wv.setframerate(rate)
    wv.writeframes(bytes(frames))
    wv.close()
    return buf.getvalue()

def _make_button_click(ms=58, vol=0.44, rate=44100) -> bytes:
    """Satisfying mechanical 'thock' for a real button press — a bright noise
    transient and a short upper tick riding on a low body thump that gives it
    weight. Deliberately deeper and punchier than the hover tap so press reads
    as a distinct, heavier event. Short enough that fast clicking never lags."""
    n = int(rate * ms / 1000)
    frames = bytearray()
    for i in range(n):
        t = i / n
        ti = i / rate
        transient = (random.random() * 2 - 1) * math.exp(-ti * 820) * 0.50
        top  = math.sin(2 * math.pi * (2050 * (1 - 0.35 * t)) * ti) * math.exp(-ti * 210) * 0.26
        body = math.sin(2 * math.pi * (165 * (1 - 0.18 * t)) * ti) * math.exp(-ti * 36) * 0.66
        body += math.sin(2 * math.pi * 165 * 2.7 * ti) * math.exp(-ti * 72) * 0.13
        attack = min(1.0, i / max(1, n * 0.004))
        s = (transient + top + body) * attack * vol
        frames += struct.pack("<h", int(max(-1.0, min(1.0, s)) * 32767))
    buf = io.BytesIO()
    wv = wave.open(buf, "wb")
    wv.setnchannels(1); wv.setsampwidth(2); wv.setframerate(rate)
    wv.writeframes(bytes(frames))
    wv.close()
    return buf.getvalue()

_BLIP_UP   = _make_wheel_tick(pitch=1.12)
_BLIP_DN   = _make_wheel_tick(pitch=0.86)
_SFX_CANCEL = _make_sfx_cancel()
_SFX_BTN_HOVER = _make_wood_tap()
_SFX_BTN_CLICK = _make_button_click()

def _make_bowl_strike(f0=440, ms=900, vol=0.35, rate=44100) -> bytes:
    """Single soft resonant strike with two very close detuned frequencies
    beating against each other — mimics the natural acoustic complexity of
    a real struck object (singing bowl / tuning fork) rather than a clean
    synthesized tone."""
    n = int(rate * ms / 1000)
    frames = bytearray()
    for i in range(n):
        t = i / n
        body = math.sin(2 * math.pi * f0 * i / rate)
        body += math.sin(2 * math.pi * f0 * 1.003 * i / rate)
        body += math.sin(2 * math.pi * f0 * 2.0 * i / rate) * 0.18
        body += math.sin(2 * math.pi * f0 * 2.006 * i / rate) * 0.18
        body /= 2.36
        attack = min(1.0, i / max(1, n * 0.05))
        decay = math.exp(-t * 2.2)
        env = attack * decay * vol
        s = body * env
        frames += struct.pack("<h", int(max(-1.0, min(1.0, s)) * 32767))
    buf = io.BytesIO()
    wv = wave.open(buf, "wb")
    wv.setnchannels(1); wv.setsampwidth(2); wv.setframerate(rate)
    wv.writeframes(bytes(frames))
    wv.close()
    return buf.getvalue()

_SFX_START = _make_bowl_strike(f0=440, ms=900, vol=0.20)
_SFX_COMPLETE = _make_bowl_strike(f0=330, ms=1100, vol=0.18)

def play_tick(soft=False):
    sound = _CLICK_SOFT if soft else _CLICK
    play_sfx(sound)

_PG_SOUND_CACHE: dict = {}

def _pg_sound(data: bytes):
    """Wrap a raw wav-bytes constant in a pygame Sound once and cache it —
    these bytes objects are fixed module-level constants, so id() is a stable,
    cheap cache key (no need to hash the contents)."""
    key = id(data)
    snd = _PG_SOUND_CACHE.get(key)
    if snd is None:
        snd = pygame.mixer.Sound(io.BytesIO(data))
        _PG_SOUND_CACHE[key] = snd
    return snd

# Plenty of channels so overlapping ticks can each ring out on their own.
# Routing every tick through ONE channel was the real source of the "pop":
# starting a sound on a busy channel stops the current one instantly, and
# cutting a waveform at non-zero amplitude is a step discontinuity — which
# is heard as a click regardless of the sample data. Ticks are ~10ms, so
# letting them overlap and mix is both harmless and what real detents do.
if pygame is not None:
    try:
        pygame.mixer.set_num_channels(16)
    except Exception:
        pass

SFX_ENABLED = True

def play_sfx(sound):
    if not SFX_ENABLED or pygame is None or sound is None:
        return
    try:
        # .play() on the Sound (not a fixed Channel) lets the mixer pick a free
        # channel, so a new tick never truncates the one still ringing.
        _pg_sound(sound).play()
    except Exception:
        pass

def cancel_pending_sfx():
    pass  # nothing is queued — each sound plays on its own channel

def stop_sfx():
    """Silence whatever is actively playing right now.

    fadeout() rather than stop(): ramping to zero over a few ms avoids the
    discontinuity an instant cut would leave. Short enough to still read as
    immediate, long enough that the stop itself makes no sound."""
    if pygame is None:
        return
    try:
        pygame.mixer.fadeout(15)
    except Exception:
        pass

# ─── Admin helpers ─────────────────────────────────────────────────────────────

def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False

def elevate():
    ctypes.windll.shell32.ShellExecuteW(
        None, "runas", sys.executable, f'"{sys.argv[0]}"', None, 1
    )
    sys.exit()

_STARTUP_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_STARTUP_VALUE   = "AllBlockResume"

def _startup_launch_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    return f'"{sys.executable}" "{os.path.abspath(__file__)}"'

def set_resume_on_login(enabled: bool):
    """Opt-in toggle (set from the first-run setup screen / Settings) so an
    active focus session survives a reboot instead of a restart being a free
    escape hatch."""
    try:
        key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, _STARTUP_RUN_KEY, 0, winreg.KEY_SET_VALUE)
        if enabled:
            winreg.SetValueEx(key, _STARTUP_VALUE, 0, winreg.REG_SZ, _startup_launch_command())
        else:
            try:
                winreg.DeleteValue(key, _STARTUP_VALUE)
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
    except Exception:
        pass


def create_desktop_shortcut():
    """Best-effort Desktop\\All Block.lnk via WScript.Shell over PowerShell —
    avoids pulling in pywin32 just for this one shortcut call."""
    try:
        desktop = os.path.join(os.environ["USERPROFILE"], "Desktop")
        link_path = os.path.join(desktop, "All Block.lnk")
        if getattr(sys, "frozen", False):
            target, workdir, args = sys.executable, os.path.dirname(sys.executable), ""
        else:
            target = sys.executable
            workdir = os.path.dirname(os.path.abspath(__file__))
            args = os.path.abspath(__file__)
        icon = _logo_ico_path()

        def _q(s: str) -> str:
            return s.replace("'", "''")

        ps = (
            f"$s = New-Object -ComObject WScript.Shell; "
            f"$sc = $s.CreateShortcut('{_q(link_path)}'); "
            f"$sc.TargetPath = '{_q(target)}'; "
            f"$sc.Arguments = '{_q(args)}'; "
            f"$sc.WorkingDirectory = '{_q(workdir)}'; "
            f"$sc.IconLocation = '{_q(icon)}'; "
            f"$sc.Save()"
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW, check=False)
    except Exception:
        pass


def ensure_admin():
    """Relaunch elevated at startup and require it — BlockInput() and hosts-file
    blocking both silently no-op without admin, so the app refuses to run at all
    without it rather than falling back to a weaker unelevated mode."""
    if is_admin():
        return
    try:
        params = " ".join(f'"{a}"' for a in sys.argv)
        ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)
    except Exception:
        pass
    sys.exit()               # either an elevated child is launching, or the user
                              # declined UAC — this unelevated instance never runs

# ─── Fonts (bundled Montserrat, loaded privately for crisp HD text) ─────────────

FONT = "Segoe UI"   # swapped to "Montserrat" once the bundled TTFs load

def _asset_dir() -> str:
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "assets", "fonts")

def _font_file(weight: str = "regular") -> str | None:
    """Path to a bundled Montserrat TTF (for PIL text rendering / blur)."""
    d = _asset_dir()
    name = "Montserrat-Bold.ttf" if weight == "bold" else "Montserrat-Medium.ttf"
    p = os.path.join(d, name)
    return p if os.path.isfile(p) else None

def load_fonts():
    """Register the bundled Montserrat TTFs for this process only, then switch the
    global FONT so every widget/canvas draws in Montserrat. Falls back silently."""
    global FONT
    d = _asset_dir()
    if not os.path.isdir(d):
        return
    FR_PRIVATE = 0x10
    ok = False
    try:
        for fn in os.listdir(d):
            if fn.lower().endswith(".ttf"):
                if ctypes.windll.gdi32.AddFontResourceExW(os.path.join(d, fn), FR_PRIVATE, 0):
                    ok = True
    except Exception:
        pass
    if ok:
        FONT = "Montserrat"

# ─── Logo / taskbar identity ──────────────────────────────────────────────────

def _assets_root() -> str:
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "assets")

def _logo_ico_path() -> str:
    return os.path.join(_assets_root(), "logo.ico")

def _logo_png_path() -> str:
    return os.path.join(_assets_root(), "logo.png")

def make_logo(size: int = 512) -> "Image.Image":
    """The All Block mark: a dark rounded square with a bold white 'block' ring
    (circle + diagonal bar) — matches the in-app block icon, reads at any size."""
    s = size
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # rounded-square tile, near-black with a subtle vertical lift
    rad = int(s * 0.22)
    d.rounded_rectangle([0, 0, s - 1, s - 1], radius=rad, fill=(19, 19, 22, 255))
    d.rounded_rectangle([0, 0, s - 1, int(s * 0.5)], radius=rad, fill=(30, 30, 35, 255))
    d.rounded_rectangle([0, int(s * 0.28), s - 1, s - 1], radius=rad, fill=(19, 19, 22, 255))
    # white "no-entry" ring
    pad = int(s * 0.24)
    w = max(3, int(s * 0.075))
    d.ellipse([pad, pad, s - pad, s - pad], outline=(245, 245, 247, 255), width=w)
    import math as _m
    cx = cy = s / 2
    r = (s - 2 * pad) / 2 - w * 0.5
    ang = _m.radians(45)
    dx, dy = r * _m.cos(ang), r * _m.sin(ang)
    d.line([cx - dx, cy + dy, cx + dx, cy - dy], fill=(245, 245, 247, 255), width=w)
    return img

def ensure_logo_files():
    """Write assets/logo.png + assets/logo.ico next to the script (dev runs only;
    a frozen build ships them, so skip if the dir isn't writable)."""
    try:
        os.makedirs(_assets_root(), exist_ok=True)
        png, ico = _logo_png_path(), _logo_ico_path()
        if not os.path.exists(png) or not os.path.exists(ico):
            base = make_logo(512)
            base.save(png)
            base.save(ico, sizes=[(256, 256), (128, 128), (64, 64),
                                  (48, 48), (32, 32), (16, 16)])
    except Exception:
        pass

def load_logo_image():
    """Small CTkImage of the logo for the header wordmark, or None."""
    try:
        p = _logo_png_path()
        im = Image.open(p) if os.path.exists(p) else make_logo(256)
        return ctk.CTkImage(light_image=im, dark_image=im, size=(26, 26))
    except Exception:
        return None

def set_app_user_model_id():
    """Give the process a stable AppUserModelID so Windows uses OUR icon on the
    taskbar (not python's) and groups windows correctly. Call before any window."""
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_AUMID)
    except Exception:
        pass

def set_window_icon(win):
    """Point the title-bar / taskbar icon at logo.ico."""
    try:
        ico = _logo_ico_path()
        if os.path.exists(ico):
            win.iconbitmap(default=ico)
    except Exception:
        pass
    try:
        im = load_logo_image()
        if im is not None:
            # iconphoto backs the icon when iconbitmap isn't honoured
            _photo = tk.PhotoImage(file=_logo_png_path()) if os.path.exists(_logo_png_path()) else None
            if _photo is not None:
                win._icon_photo = _photo
                win.iconphoto(True, _photo)
    except Exception:
        pass

# ─── System tray ──────────────────────────────────────────────────────────────

_TRAY_MSG = 0x0400 + 17          # WM_APP + 17, our private notification message
_TRAY_ID_SHOW, _TRAY_ID_QUIT = 1001, 1002


def _tray_ico_path(badged: bool) -> str:
    """Write (once) the two tray icons: the plain mark, and the same mark with a
    green dot in the corner meaning 'a schedule is armed'. Kept beside the data
    file rather than in assets/ so a frozen, read-only install still works."""
    d = DATA_FILE.parent / "tray"
    d.mkdir(parents=True, exist_ok=True)
    p = d / ("tray-sched.ico" if badged else "tray.ico")
    if p.exists():
        return str(p)
    img = make_logo(256).convert("RGBA")
    if badged:
        s = 256
        dr = ImageDraw.Draw(img)
        r = s * 0.135
        cx, cy = s - r - s * 0.035, s - r - s * 0.035
        # ring of background colour first, so the dot stays readable against
        # the logo underneath at 16px as well as at full size
        dr.ellipse([cx - r * 1.34, cy - r * 1.34, cx + r * 1.34, cy + r * 1.34],
                   fill=(19, 19, 22, 255))
        dr.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(31, 157, 85, 255))
    img.save(str(p), format="ICO",
             sizes=[(16, 16), (20, 20), (24, 24), (32, 32), (48, 48), (64, 64), (256, 256)])
    return str(p)


class _TrayIcon:
    """A Windows notification-area icon, driven by Shell_NotifyIcon through
    ctypes (no third-party dependency, so nothing new to bundle).

    Win32 needs a window and a message pump to deliver tray clicks, so both
    live on a private thread. Tkinter is not thread-safe, so clicks are handed
    back as callables on a queue that the app drains from its own `after` loop.
    """

    def __init__(self):
        self.actions = []                 # click results, drained by the app
        self._lock = threading.Lock()
        self._hwnd = None
        self._thread = None
        self._alive = False
        self._badged = False
        self._tip = "All Block"

    # ---- public API (safe to call from the Tk thread) -----------------------

    def start(self):
        if self._alive or os.name != "nt":
            return
        self._alive = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def update(self, badged: bool, tip: str):
        """Swap between the plain and schedule-armed icon, and retitle it."""
        if not self._alive or (badged == self._badged and tip == self._tip):
            return
        self._badged, self._tip = badged, tip
        if self._hwnd:
            try:
                self._notify(1)           # NIM_MODIFY
            except Exception:
                pass

    def stop(self):
        if not self._alive:
            return
        self._alive = False
        try:
            if self._hwnd:
                self._notify(2)           # NIM_DELETE
                ctypes.windll.user32.PostMessageW(self._hwnd, 0x0010, 0, 0)  # WM_CLOSE
        except Exception:
            pass

    def take_actions(self):
        with self._lock:
            out, self.actions = self.actions, []
        return out

    # ---- Win32 internals (private thread) -----------------------------------

    def _notify(self, op):
        user32 = ctypes.windll.user32
        hicon = user32.LoadImageW(None, _tray_ico_path(self._badged), 1, 0, 0,
                                  0x00000010 | 0x00008000)   # LR_LOADFROMFILE|LR_SHARED
        # NOTIFYICONDATAW, laid out by hand — the struct is stable across the
        # versions of Windows this app supports.
        buf = ctypes.create_string_buffer(976)
        ctypes.memset(buf, 0, 976)
        ctypes.c_uint32.from_buffer(buf, 0).value = 976
        ctypes.c_void_p.from_buffer(buf, 8).value = self._hwnd
        ctypes.c_uint32.from_buffer(buf, 16).value = 1                 # uID
        ctypes.c_uint32.from_buffer(buf, 20).value = 0x01 | 0x02 | 0x04  # MESSAGE|ICON|TIP
        ctypes.c_uint32.from_buffer(buf, 24).value = _TRAY_MSG
        ctypes.c_void_p.from_buffer(buf, 32).value = hicon
        tip = (self._tip or "All Block")[:127]
        ctypes.memmove(ctypes.addressof(buf) + 40, ctypes.create_unicode_buffer(tip),
                       (len(tip) + 1) * 2)
        return ctypes.windll.shell32.Shell_NotifyIconW(op, ctypes.byref(buf))

    def _queue(self, fn_name):
        with self._lock:
            self.actions.append(fn_name)

    def _menu(self):
        user32 = ctypes.windll.user32
        menu = user32.CreatePopupMenu()
        user32.AppendMenuW(menu, 0, _TRAY_ID_SHOW, "Open All Block")
        user32.AppendMenuW(menu, 0x800, 0, None)          # MF_SEPARATOR
        user32.AppendMenuW(menu, 0, _TRAY_ID_QUIT, "Quit")
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        # the foreground dance Shell_NotifyIcon menus require, or the menu
        # sticks open until you click elsewhere
        user32.SetForegroundWindow(self._hwnd)
        cmd = user32.TrackPopupMenu(menu, 0x0100 | 0x0002,  # RETURNCMD | RIGHTALIGN
                                    pt.x, pt.y, 0, self._hwnd, None)
        user32.PostMessageW(self._hwnd, 0, 0, 0)
        user32.DestroyMenu(menu)
        if cmd == _TRAY_ID_SHOW:
            self._queue("show")
        elif cmd == _TRAY_ID_QUIT:
            self._queue("quit")

    def _run(self):
        try:
            user32 = ctypes.windll.user32
            LRESULT = ctypes.c_ssize_t
            WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, ctypes.c_uint,
                                          wintypes.WPARAM, wintypes.LPARAM)
            user32.DefWindowProcW.restype = LRESULT
            user32.DefWindowProcW.argtypes = [wintypes.HWND, ctypes.c_uint,
                                               wintypes.WPARAM, wintypes.LPARAM]

            def proc(hwnd, msg, wp, lp):
                if msg == _TRAY_MSG:
                    ev = lp & 0xFFFF
                    if ev in (0x0202, 0x0203):        # left click / double click
                        self._queue("show")
                    elif ev == 0x0205:                # right button up
                        self._menu()
                    return 0
                return user32.DefWindowProcW(hwnd, msg, wp, lp)

            self._proc = WNDPROC(proc)                # keep a ref alive

            class WNDCLASS(ctypes.Structure):
                _fields_ = [("style", ctypes.c_uint), ("lpfnWndProc", WNDPROC),
                            ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                            ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                            ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HANDLE),
                            ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]

            # ctypes defaults every return to a 32-bit int, which truncates
            # 64-bit handles into garbage — declare the ones we pass around.
            kernel32 = ctypes.windll.kernel32
            kernel32.GetModuleHandleW.restype = wintypes.HMODULE
            kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
            user32.CreateWindowExW.restype = wintypes.HWND
            user32.CreateWindowExW.argtypes = [
                wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
            user32.LoadImageW.restype = wintypes.HANDLE

            wc = WNDCLASS()
            wc.lpfnWndProc = self._proc
            wc.hInstance = kernel32.GetModuleHandleW(None)
            wc.lpszClassName = "AllBlockTrayWnd"
            user32.RegisterClassW(ctypes.byref(wc))
            self._hwnd = user32.CreateWindowExW(0, "AllBlockTrayWnd", "All Block",
                                                 0, 0, 0, 0, 0, None, None,
                                                 wc.hInstance, None)
            self._notify(0)                            # NIM_ADD

            msg = wintypes.MSG()
            while self._alive and user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception as e:
            # A missing tray icon must never take the app down with it; keep the
            # reason around so a dev run can see why it gave up.
            self._error = "{}: {}".format(type(e).__name__, e)
            self._alive = False


# ─── Persistence ──────────────────────────────────────────────────────────────

DEFAULT_DATA = {"websites": [], "apps": [], "sessions": [], "schedules": [],
                 "setup_complete": False, "auto_resume": True, "sfx_enabled": True}

def load_data() -> dict:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    if DATA_FILE.exists():
        try:
            with open(DATA_FILE, encoding="utf-8") as f:
                return {**DEFAULT_DATA, **json.load(f)}
        except Exception:
            pass
    return dict(DEFAULT_DATA)

def save_data(data: dict):
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def save_session_state(end: datetime, total: int):
    """Persist the active lock's end time so a resumed launch (e.g. after a
    reboot) can re-arm the same session instead of losing it outright."""
    try:
        SESSION_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(SESSION_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"end": end.isoformat(), "total": total}, f)
    except Exception:
        pass

def load_session_state() -> dict | None:
    try:
        if SESSION_STATE_FILE.exists():
            with open(SESSION_STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return None

def clear_session_state():
    try:
        SESSION_STATE_FILE.unlink(missing_ok=True)
    except Exception:
        pass

# ─── Website Blocking (hosts file) ────────────────────────────────────────────

def _clean_domain(raw: str) -> str:
    d = raw.strip().lower()
    for prefix in ("https://www.", "http://www.", "https://", "http://", "www."):
        if d.startswith(prefix):
            d = d[len(prefix):]
    return d.rstrip("/").rstrip("\\")

def block_site(raw: str):
    domain = _clean_domain(raw)
    try:
        with open(HOSTS_FILE, "r", encoding="utf-8") as f:
            content = f.read()
        lines_to_add = []
        for sub in (domain, f"www.{domain}"):
            if f" {sub} " not in content and f" {sub}\n" not in content and not content.endswith(f" {sub}"):
                lines_to_add.append(f"{REDIRECT} {sub} {MARKER}")
        if lines_to_add:
            with open(HOSTS_FILE, "a", encoding="utf-8") as f:
                f.write("\n" + "\n".join(lines_to_add))
        return True, domain
    except PermissionError:
        return False, "Run All Block as Administrator to block websites."
    except Exception as e:
        return False, str(e)

def unblock_site(raw: str):
    domain = _clean_domain(raw)
    try:
        with open(HOSTS_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
        kept = [l for l in lines if not (MARKER in l and (f" {domain} " in l or f" www.{domain} " in l or l.strip().endswith(domain)))]
        with open(HOSTS_FILE, "w", encoding="utf-8") as f:
            f.writelines(kept)
        subprocess.run(["ipconfig", "/flushdns"], capture_output=True)
        return True
    except Exception:
        return False

# ─── App Blocking (process monitor) ───────────────────────────────────────────

class AppBlocker:
    def __init__(self):
        self._blocked: set[str] = set()
        self._running = False
        self._thread: threading.Thread | None = None

    def set_list(self, apps: list[str]):
        self._blocked = {a.lower() for a in apps}
        if self._blocked and not self._running:
            self._start()
        elif not self._blocked:
            self._stop()

    def _start(self):
        self._running = True
        self._thread = threading.Thread(target=self._monitor, daemon=True)
        self._thread.start()

    def _stop(self):
        self._running = False

    def _monitor(self):
        while self._running:
            try:
                for proc in psutil.process_iter(["name", "pid"]):
                    try:
                        name = (proc.info["name"] or "").lower()
                        if name in self._blocked:
                            proc.kill()
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
            except Exception:
                pass
            time.sleep(1.5)

# ─── Taskbar hide / restore (best-effort kiosk lock) ───────────────────────────

def set_taskbar_visible(visible: bool):
    try:
        user32 = ctypes.windll.user32
        SW_HIDE, SW_SHOW = 0, 5
        flag = SW_SHOW if visible else SW_HIDE
        for cls, title in (("Shell_TrayWnd", None), ("Shell_SecondaryTrayWnd", None), ("Button", "Start")):
            hwnd = user32.FindWindowW(cls, title)
            if hwnd:
                user32.ShowWindow(hwnd, flag)
    except Exception:
        pass

# ─── DWM ghost cleaner ─────────────────────────────────────────────────────────
# Destroying a top-most borderless *layered* (alpha) window can leave its pixels
# cached in the Desktop Window Manager compositor — a "white box" that lingers over
# everything even though no window exists. Forcing every visible window (plus the
# shell + desktop) to repaint flushes that stale composition so nothing survives.
def clear_screen_ghosts():
    try:
        user32 = ctypes.windll.user32
        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        # INVALIDATE | ERASE | ALLCHILDREN | UPDATENOW | FRAME
        RDW = 0x0001 | 0x0004 | 0x0080 | 0x0100 | 0x0400
        def _cb(hwnd, _):
            if user32.IsWindowVisible(hwnd):
                user32.RedrawWindow(hwnd, None, None, RDW)
            return True
        user32.EnumWindows(WNDENUMPROC(_cb), 0)
        user32.RedrawWindow(user32.GetShellWindow(), None, None, RDW)
        user32.RedrawWindow(user32.GetDesktopWindow(), None, None, RDW)
    except Exception:
        pass

# ─── Hard input lock (mouse + keyboard) ─────────────────────────────────────────
# Low-level hooks (WH_KEYBOARD_LL / WH_MOUSE_LL) intercept every input event before
# it reaches any app and return "handled", dropping it. This swallows all mouse and
# keyboard input system-wide and does not require admin. BlockInput() is added as a
# best-effort second layer. The ONLY thing neither can touch is Ctrl+Alt+Del — the
# OS-enforced secure attention sequence — so the user can never be permanently
# trapped: Ctrl+Alt+Del → Task Manager → end All Block always frees the machine.

_WH_KEYBOARD_LL = 13
_WH_MOUSE_LL    = 14
_HC_ACTION      = 0
_WM_KEYDOWN     = 0x0100
_WM_KEYUP       = 0x0101
_WM_SYSKEYDOWN  = 0x0104
_WM_SYSKEYUP    = 0x0105
_VK_CONTROL     = 0x11
_VK_LCONTROL    = 0xA2
_VK_RCONTROL    = 0xA3
_VK_P           = 0x50
_VK_L           = 0x4C
_VK_M           = 0x4D
_LRESULT        = ctypes.c_ssize_t
_HOOKPROC       = ctypes.WINFUNCTYPE(_LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

_PTP_PATH       = r"Software\Microsoft\Windows\CurrentVersion\PrecisionTouchPad"
# every gesture the precision-touchpad driver can consume on its own (bypassing
# WH_MOUSE_LL): 2-finger scroll/zoom, taps, and all 3/4-finger slides + taps.
_GESTURE_VALUES = ["PanEnabled",              # 2-finger scroll (up/down/left/right)
                   "ZoomEnabled",             # 2-finger pinch-zoom
                   "TapsEnabled",             # tap-to-click
                   "TapAndDrag",              # tap-drag
                   "TwoFingerTapEnabled",     # 2-finger tap (right-click)
                   "RightClickZoneEnabled",   # corner right-click
                   "ThreeFingerSlideEnabled", "FourFingerSlideEnabled",
                   "ThreeFingerTapEnabled",   "FourFingerTapEnabled"]

def _broadcast_setting_change():
    try:
        HWND_BROADCAST, WM_SETTINGCHANGE = 0xFFFF, 0x001A
        ctypes.windll.user32.SendMessageTimeoutW(
            HWND_BROADCAST, WM_SETTINGCHANGE, 0, "PrecisionTouchPad", 0, 100, None)
    except Exception:
        pass

class _KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

class _InputBlocker:
    """Kiosk input lock via low-level hooks. start()/stop() are idempotent."""
    def __init__(self):
        self._kb = None
        self._ms = None
        self._kb_proc = None
        self._ms_proc = None
        self.panic = False
        self._ctrl_down = False
        self._p_down = False
        self._l_down = False
        self._m_down = False
        self._gesture_saved = None
        self._u = ctypes.windll.user32
        self._k = ctypes.windll.kernel32
        self._k.GetModuleHandleW.restype  = wintypes.HMODULE   # else 64-bit handle is truncated
        self._k.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        self._u.SetWindowsHookExW.restype  = wintypes.HHOOK
        self._u.SetWindowsHookExW.argtypes = [ctypes.c_int, _HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
        self._u.CallNextHookEx.restype     = _LRESULT
        self._u.CallNextHookEx.argtypes    = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
        self._u.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]

    def _kb_cb(self, nCode, wParam, lParam):
        if nCode == _HC_ACTION:
            try:
                vk = ctypes.cast(lParam, ctypes.POINTER(_KBDLLHOOKSTRUCT)).contents.vkCode
            except Exception:
                vk = 0
            down = wParam in (_WM_KEYDOWN, _WM_SYSKEYDOWN)
            up   = wParam in (_WM_KEYUP, _WM_SYSKEYUP)
            if vk in (_VK_CONTROL, _VK_LCONTROL, _VK_RCONTROL):
                if down:
                    self._ctrl_down = True
                elif up:
                    self._ctrl_down = False
            elif vk == _VK_P:
                self._p_down = down
            elif vk == _VK_L:
                self._l_down = down
            elif vk == _VK_M:
                self._m_down = down
            if down and self._ctrl_down and self._p_down and self._l_down and self._m_down:
                self.panic = True
        if nCode == _HC_ACTION:
            return 1  # eat every key — nothing reaches any window
        return self._u.CallNextHookEx(None, nCode, wParam, lParam)

    def _ms_cb(self, nCode, wParam, lParam):
        if nCode == _HC_ACTION:
            return 1  # eat every mouse event
        return self._u.CallNextHookEx(None, nCode, wParam, lParam)

    # ─ precision-touchpad multi-finger gestures bypass WH_MOUSE_LL (the driver
    #   consumes them), so temporarily switch them off at the registry and restore later ─
    def _disable_gestures(self):
        if self._gesture_saved is not None:
            return
        saved = {}
        try:
            key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, _PTP_PATH, 0,
                                     winreg.KEY_READ | winreg.KEY_SET_VALUE)
            for v in _GESTURE_VALUES:
                try:
                    saved[v] = winreg.QueryValueEx(key, v)[0]
                except FileNotFoundError:
                    saved[v] = None
                winreg.SetValueEx(key, v, 0, winreg.REG_DWORD, 0)
            winreg.CloseKey(key)
            self._gesture_saved = saved
            _broadcast_setting_change()
        except Exception:
            self._gesture_saved = saved or {}

    def _restore_gestures(self):
        if self._gesture_saved is None:
            return
        try:
            key = winreg.OpenKeyEx(winreg.HKEY_CURRENT_USER, _PTP_PATH, 0, winreg.KEY_SET_VALUE)
            for v, val in self._gesture_saved.items():
                if val is None:
                    try:
                        winreg.DeleteValue(key, v)
                    except FileNotFoundError:
                        pass
                else:
                    winreg.SetValueEx(key, v, 0, winreg.REG_DWORD, int(val))
            winreg.CloseKey(key)
            _broadcast_setting_change()
        except Exception:
            pass
        self._gesture_saved = None

    def start(self) -> bool:
        if self._kb or self._ms:
            return True
        self.panic = False
        self._ctrl_down = False
        self._p_down = False
        self._l_down = False
        self._m_down = False
        try:
            hmod = self._k.GetModuleHandleW(None)
            self._kb_proc = _HOOKPROC(self._kb_cb)
            self._ms_proc = _HOOKPROC(self._ms_cb)
            self._kb = self._u.SetWindowsHookExW(_WH_KEYBOARD_LL, self._kb_proc, hmod, 0)
            self._ms = self._u.SetWindowsHookExW(_WH_MOUSE_LL, self._ms_proc, hmod, 0)
        except Exception:
            pass
        try:
            self._u.BlockInput(True)   # best-effort second layer (needs admin)
        except Exception:
            pass
        self._disable_gestures()
        return bool(self._kb or self._ms)

    def stop(self):
        try:
            self._u.BlockInput(False)
        except Exception:
            pass
        for h in (self._kb, self._ms):
            if h:
                try:
                    self._u.UnhookWindowsHookEx(h)
                except Exception:
                    pass
        self._kb = self._ms = None
        self._kb_proc = self._ms_proc = None
        self.panic = False
        self._ctrl_down = False
        self._restore_gestures()

_INPUT_BLOCKER = _InputBlocker()

def block_user_input(block: bool) -> bool:
    if block:
        return _INPUT_BLOCKER.start()
    _INPUT_BLOCKER.stop()
    return True

# ─── Duration formatting ────────────────────────────────────────────────────────

def fmt_clock(minutes: int) -> str:
    h, m = divmod(int(minutes), 60)
    return f"{h}:{m:02d}"

def fmt_words(minutes: int) -> str:
    h, m = divmod(int(minutes), 60)
    if h and m:
        return f"{h} hr {m} min"
    if h:
        return f"{h} hour" + ("s" if h > 1 else "")
    return f"{m} min"

def fmt_countdown(seconds: int) -> str:
    h, r = divmod(max(0, seconds), 3600)
    m, s = divmod(r, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

def fmt_duration_words(seconds: int) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} sec"
    return fmt_words(seconds / 60)

def parse_hhmm(raw: str):
    """'9:00' / '09.00' / '0900' -> (9, 0). None if it isn't a valid time of day."""
    txt = str(raw).strip().replace(".", ":").replace(" ", "")
    if ":" not in txt and txt.isdigit() and len(txt) == 4:
        txt = txt[:2] + ":" + txt[2:]
    parts = txt.split(":")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        return None
    hh, mm = int(parts[0]), int(parts[1])
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    return hh, mm

def days_label(days) -> str:
    """Compact human name for a weekday set — 'Weekdays', 'Mon, Thu', etc."""
    try:
        d = sorted({int(x) for x in days if 0 <= int(x) <= 6})
    except Exception:
        d = []
    if not d:
        return "No days"
    if d == [0, 1, 2, 3, 4]:
        return "Weekdays"
    if d == [5, 6]:
        return "Weekends"
    if d == [0, 1, 2, 3, 4, 5, 6]:
        return "Every day"
    return ", ".join(DAY_NAMES[i] for i in d)

# ─── Shared UI helpers ────────────────────────────────────────────────────────

def card(parent, **kw) -> ctk.CTkFrame:
    return ctk.CTkFrame(parent, fg_color=C_CARD, corner_radius=16, **kw)

def label(parent, text, size=13, weight="normal", color=None, family=None, **kw) -> ctk.CTkLabel:
    # colour resolved at call time so a theme swap repaints correctly
    return ctk.CTkLabel(parent, text=text, font=ctk.CTkFont(size=size, weight=weight, family=family or FONT), text_color=color or C_INK, **kw)

# ─── Icons (drawn, not emoji) ──────────────────────────────────────────────────

_ICON_CACHE: dict = {}

def icon(kind: str, color: str, size: int = 20) -> ctk.CTkImage:
    key = (kind, color, size)
    if key in _ICON_CACHE:
        return _ICON_CACHE[key]
    s = size * 4
    pad = s // 7
    w = max(3, s // 11)
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if kind == "timer":
        d.ellipse([pad, pad, s - pad, s - pad], outline=color, width=w)
        cx = cy = s // 2
        d.line([cx, cy, cx, s // 4], fill=color, width=w)
        d.line([cx, cy, s * 2 // 3, s // 2], fill=color, width=w)
    elif kind == "block":
        d.ellipse([pad, pad, s - pad, s - pad], outline=color, width=w)
        d.line([pad + w, s - pad - w, s - pad - w, pad + w], fill=color, width=w)
    elif kind == "gear":
        cx = cy = s // 2
        r_out, r_hole = s // 2 - pad, (s // 2 - pad) * 0.42
        teeth = 8
        pts = []
        for i in range(teeth * 2):
            ang = math.pi * 2 * i / (teeth * 2)
            r = r_out if i % 2 == 0 else r_out - w * 1.6
            pts.append((cx + r * math.cos(ang), cy + r * math.sin(ang)))
        d.polygon(pts, fill=color)
        d.ellipse([cx - r_hole, cy - r_hole, cx + r_hole, cy + r_hole], fill=(0, 0, 0, 0))
    elif kind == "play":
        d.polygon([(pad, pad), (pad, s - pad), (s - pad, s // 2)], fill=color)
    elif kind == "stop":
        d.rounded_rectangle([pad, pad, s - pad, s - pad], radius=s // 6, fill=color)
    elif kind == "warning":
        d.polygon([(s // 2, pad), (s - pad, s - pad * 0.7), (pad, s - pad * 0.7)], outline=color, width=w)
        d.line([s // 2, s * 0.42, s // 2, s * 0.62], fill=color, width=w)
        d.ellipse([s // 2 - w * 0.6, s * 0.68, s // 2 + w * 0.6, s * 0.68 + w * 1.2], fill=color)
    elif kind == "check":
        d.line([pad, s // 2, s * 0.42, s - pad], fill=color, width=w)
        d.line([s * 0.42, s - pad, s - pad, pad], fill=color, width=w)
    elif kind == "dot":
        d.ellipse([pad, pad, s - pad, s - pad], fill=color)
    elif kind == "chart":
        bw = max(2, s // 6)
        gap = bw // 2
        baseline = s - pad
        x = pad
        for hfrac in (0.45, 0.85, 0.6):
            bh = (s - 2 * pad) * hfrac
            d.rounded_rectangle([x, baseline - bh, x + bw, baseline], radius=bw * 0.3, fill=color)
            x += bw + gap
    elif kind in ("sound", "mute"):
        # speaker cone, plus either two arcs (on) or a slash (off)
        bx0, bx1 = pad, pad + (s - 2 * pad) * 0.26
        by0, by1 = s * 0.38, s * 0.62
        d.rectangle([bx0, by0, bx1, by1], fill=color)
        d.polygon([(bx1, by0), (bx1, by1), (s * 0.52, s - pad), (s * 0.52, pad)], fill=color)
        if kind == "sound":
            for k, rr in enumerate((0.18, 0.30)):
                r = (s - 2 * pad) * rr
                cx, cy = s * 0.54, s / 2
                d.arc([cx - r, cy - r, cx + r, cy + r], start=-55, end=55,
                      fill=color, width=max(2, w - k))
        else:
            d.line([s * 0.60, s * 0.38, s * 0.86, s * 0.62], fill=color, width=w)
            d.line([s * 0.60, s * 0.62, s * 0.86, s * 0.38], fill=color, width=w)
    elif kind == "calendar":
        top = pad + w
        d.rounded_rectangle([pad, top, s - pad, s - pad], radius=max(2, s // 12), outline=color, width=w)
        hdr = top + (s - pad - top) * 0.30
        d.line([pad, hdr, s - pad, hdr], fill=color, width=w)
        for hx in (0.30, 0.70):                      # the two hanger rings
            x = pad + (s - 2 * pad) * hx
            d.line([x, max(0, pad - w), x, top + w], fill=color, width=w)
        r = w * 0.8                                  # a dot marking a booked day
        cx = pad + (s - 2 * pad) * 0.33
        cy = hdr + (s - pad - hdr) * 0.55
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color)
    elif kind == "link":
        # external-link glyph: a rounded square with an arrow breaking out
        # of its top-right corner, for "opens something outside the app"
        d.rounded_rectangle([pad, s * 0.34, s * 0.66, s - pad], radius=max(2, s // 14),
                             outline=color, width=w)
        ar0, ar1 = s * 0.46, s - pad
        d.line([ar0, s * 0.30 + (ar1 - ar0) * 0.55, ar1, s * 0.30], fill=color, width=w)
        head = w * 1.6
        d.line([ar1 - head * 1.7, s * 0.30, ar1, s * 0.30], fill=color, width=w)
        d.line([ar1, s * 0.30, ar1, s * 0.30 + head * 1.7], fill=color, width=w)
    # Keep the full 4x-supersampled bitmap rather than shrinking it to `size`
    # here — CTkImage re-resizes whatever it's given to size * the display's
    # DPI scaling factor at draw time. Handing it an already-tiny size x size
    # image made it upscale a low-res bitmap on any scaled display, which is
    # what actually caused the blur; a high-res source lets that resize be a
    # clean downscale instead.
    ctk_img = ctk.CTkImage(light_image=img, dark_image=img, size=(size, size))
    _ICON_CACHE[key] = ctk_img
    return ctk_img

# ─── Toggle switch (pill track + shadowed knob, checkmark grows in on-side) ───
# Replaces CTkSwitch's flat bar everywhere in the app with a rounder, more
# tactile control: a soft drop shadow under the knob for depth, and a
# checkmark that grows in as the knob lands on-side rather than a bare dot.

_SWITCH_CACHE: dict = {}
SWITCH_W, SWITCH_H = 44, 24

def _switch_frame(t: float) -> ctk.CTkImage:
    key = (THEME, round(t, 3))
    if key in _SWITCH_CACHE:
        return _SWITCH_CACHE[key]
    scale = 6
    W, H = SWITCH_W * scale, SWITCH_H * scale
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    track = _lerp_color(C_GHOST, C_GREEN, t)
    d.rounded_rectangle([0, 0, W, H], radius=H // 2, fill=track)

    margin = int(H * 0.09)
    knob_d = H - margin * 2
    kx = margin + knob_d / 2 + (W - margin * 2 - knob_d) * t
    ky = H / 2

    shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    sd.ellipse([kx - knob_d / 2, ky - knob_d / 2 + H * 0.10,
                kx + knob_d / 2, ky + knob_d / 2 + H * 0.10], fill=(0, 0, 0, 90))
    shadow = shadow.filter(ImageFilter.GaussianBlur(H * 0.05))
    img.alpha_composite(shadow)
    d.ellipse([kx - knob_d / 2, ky - knob_d / 2, kx + knob_d / 2, ky + knob_d / 2], fill="#ffffff")

    # checkmark grows in only once the knob is past the midpoint, so it
    # never appears half-formed on the off side
    ck_t = max(0.0, (t - 0.5) / 0.5)
    if ck_t > 0:
        r = knob_d * 0.30 * ck_t
        cw = max(2, int(H * 0.09))
        d.line([kx - r * 0.55, ky + r * 0.05, kx - r * 0.05, ky + r * 0.55], fill=track, width=cw)
        d.line([kx - r * 0.05, ky + r * 0.55, kx + r * 0.65, ky - r * 0.45], fill=track, width=cw)

    # Keep the 6x-supersampled bitmap rather than shrinking it here — see the
    # matching note in icon(): CTkImage re-resizes to size * DPI scaling at
    # draw time, so handing it a low-res bitmap was what actually blurred it.
    ctk_img = ctk.CTkImage(light_image=img, dark_image=img, size=(SWITCH_W, SWITCH_H))
    _SWITCH_CACHE[key] = ctk_img
    return ctk_img

def _sun_mask(size, cx, cy, r) -> Image.Image:
    m = Image.new("L", size, 0)
    d = ImageDraw.Draw(m)
    # Solid core with clearly detached rays. Merging the rays into the core
    # instead — or letting them sit thick and close — collapses the silhouette
    # into a cog; the gap is what makes it read as a sun at 15px.
    d.ellipse([cx - r * 0.62, cy - r * 0.62, cx + r * 0.62, cy + r * 0.62], fill=255)
    for k in range(8):
        a = math.pi * 2 * k / 8
        d.line([cx + math.cos(a) * r, cy + math.sin(a) * r,
                cx + math.cos(a) * r * 1.42, cy + math.sin(a) * r * 1.42],
               fill=255, width=max(2, int(r * 0.17)))
    return m

def _moon_mask(size, cx, cy, r) -> Image.Image:
    m = Image.new("L", size, 0)
    d = ImageDraw.Draw(m)
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=255)
    ox, oy = r * 0.68, -r * 0.30          # bite an offset disc out to leave a crescent
    d.ellipse([cx - r + ox, cy - r + oy, cx + r + ox, cy + r + oy], fill=0)
    return m

_THEME_SWITCH_CACHE: dict = {}
THEME_SWITCH_W = THEME_SWITCH_H = 30      # plate diameter, drawn natively by the button itself
THEME_GLYPH_SIZE = 15                     # declared glyph image size — matches the gear icon so
                                          # both buttons' image column reserves the same width and
                                          # their bounding boxes (and the gap between them) line up
THEME_SWITCH_FRAMES = 12

def theme_switch_frames(hover: bool = False) -> list:
    """Pre-baked frames of the round toggle turning sun into moon.

    Frame 0 is the light-theme rest state, the last frame the dark-theme one, and
    the frames between are the swap: the glyph rotates a quarter turn while the
    outgoing shape shrinks away and the incoming one grows in, so the change reads
    as one object turning rather than two icons cutting.

    The glyph shows the mode you'd switch TO — with a single icon and no track,
    position can't carry state, so the button has to name its action instead.

    Re-running PIL every animation frame stutters, so the whole sweep is drawn
    once per palette+hover state and swapped in as ready-made images. Keyed on
    THEME so a palette change regenerates rather than serving stale colours."""
    key = (THEME, hover)
    if key in _THEME_SWITCH_CACHE:
        return _THEME_SWITCH_CACHE[key]
    # Authored at 3x the logical size, not 1x. CTk renders a CTkImage at
    # logical size x widget scaling (2.0 here), so a 30px source gets upscaled
    # and comes out visibly soft; oversampling means CTk only ever downscales.
    D, n = THEME_SWITCH_W, THEME_SWITCH_FRAMES
    OUT = D * 3
    S = 2                                   # supersample on top of that
    w = h = OUT * S
    ink = C_INK if hover else C_MUTED       # hover darkens the glyph, matching the nav pills
    cx = cy = w / 2
    gr = w * 0.25

    def ramp(x, lo, hi):
        return max(0.0, min(1.0, (x - lo) / (hi - lo)))

    def stamp(img, mask, color, scale, spin):
        """Draw one glyph scaled about the centre and spun, at full opacity.

        Scaling the shape rather than fading it keeps the glyph solid the whole
        way through — a half-transparent icon against the plate just looks like a
        rendering fault at 30px."""
        if scale <= 0.02:
            return
        layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        layer.paste(Image.new("RGBA", (w, h), color), (0, 0), mask(gr * scale))
        if spin:
            layer = layer.rotate(spin, resample=Image.BICUBIC, center=(cx, cy))
        img.alpha_composite(layer)

    frames = []
    for i in range(n):
        t = i / max(1, n - 1)
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        # No plate drawn here — the button itself renders a native round plate
        # (same as the gear icon button), so this is glyph-only, transparent.
        spin = 90.0 * _ease_in_out(t)
        # moon (go dark) collapses through the first half, sun (go light) opens
        # through the second — they never overlap, so no muddy double image
        stamp(img, lambda r: _moon_mask((w, h), cx, cy, r), ink,
              1.0 - ramp(t, 0.10, 0.52), spin)
        # sun drawn smaller than the moon so the two match by ink coverage rather
        # than by radius — its rays push well past its nominal circle
        stamp(img, lambda r: _sun_mask((w, h), cx, cy, r * 0.84), ink,
              ramp(t, 0.48, 0.90), spin - 90.0)
        img = img.resize((OUT, OUT), Image.LANCZOS)
        frames.append(ctk.CTkImage(light_image=img, dark_image=img,
                                    size=(THEME_GLYPH_SIZE, THEME_GLYPH_SIZE)))
    _THEME_SWITCH_CACHE[key] = frames
    return frames

def make_switch(parent, variable: tk.BooleanVar, command=None) -> ctk.CTkButton:
    """A hardware-style toggle button bound to a BooleanVar — drop-in
    replacement for CTkSwitch. Plays the soft tick and slides the knob over,
    then calls command() once it lands."""
    state = {"pos": 1.0 if variable.get() else 0.0, "busy": False}
    # hover=False rather than a transparent hover_color — CTk rejects
    # "transparent" for hover_color, and the drawn image already carries the
    # whole visual, so there is nothing for a hover tint to usefully do.
    btn = ctk.CTkButton(parent, text="", image=_switch_frame(state["pos"]),
                         width=SWITCH_W, height=SWITCH_H, corner_radius=SWITCH_H // 2,
                         fg_color="transparent", hover=False,
                         border_width=0, cursor="hand2")

    def _click(_evt=None):
        if state["busy"]:
            return
        state["busy"] = True
        on = not variable.get()
        variable.set(on)
        play_tick(soft=True)
        start, end = state["pos"], (1.0 if on else 0.0)

        def frame(t):
            pos = start + (end - start) * _ease_out_cubic(t)
            state["pos"] = pos
            if btn.winfo_exists():
                btn.configure(image=_switch_frame(pos))

        def done():
            state["busy"] = False
            if command:
                command()

        animate(btn, 140, frame, done)

    btn.configure(command=_click)
    return btn

# Monkey-patch create_round_rect onto Canvas if not exists
if not hasattr(tk.Canvas, 'create_round_rect'):
    def _create_round_rect(self, x1, y1, x2, y2, radius=25, **kwargs):
        """Draw a rounded rectangle on canvas."""
        points = [
            x1 + radius, y1,
            x2 - radius, y1,
            x2, y1,
            x2, y1 + radius,
            x2, y2 - radius,
            x2, y2,
            x2 - radius, y2,
            x1 + radius, y2,
            x1, y2,
            x1, y2 - radius,
            x1, y1 + radius,
            x1, y1,
        ]
        return self.create_polygon(points, smooth=True, **kwargs)
    tk.Canvas.create_round_rect = _create_round_rect


# ─── Main Window ──────────────────────────────────────────────────────────────

class AllBlock(ctk.CTk):
    def __init__(self):
        super().__init__()
        # customtkinter's own titlebar dark-mode sync hides+reshows the whole
        # OS window on every set_appearance_mode() call (see
        # sync_titlebar_dark_mode's docstring) — that was the real source of
        # the post-transition flash. Drive the DWM call ourselves instead.
        self._deactivate_windows_window_header_manipulation = True
        self.data     = load_data()
        global SFX_ENABLED
        SFX_ENABLED = bool(self.data.get("sfx_enabled", True))

        apply_palette(detect_system_theme())   # always match the OS on launch — manual toggles are session-only
        ctk.set_appearance_mode(THEME)
        ctk.set_default_color_theme("blue")
        self.after(0, lambda: sync_titlebar_dark_mode(self, THEME == "dark"))

        self._logo_img = load_logo_image()               # header wordmark logo
        set_window_icon(self)                             # title-bar / taskbar icon

        self.blocker  = AppBlocker()
        self.blocker.set_list(self.data["apps"])

        self.session_on    = False
        self.session_end: datetime | None = None
        self.session_total = 0
        self.active_page   = "timer"

        # OptionWheel duration picker state
        self._wheel_sel    = WHEEL_DEFAULT_IDX          # settled option index
        self._wheel_pos    = float(WHEEL_DEFAULT_IDX)   # smoothed float position
        self._wheel_target = float(WHEEL_DEFAULT_IDX)   # position it eases toward
        self._wheel_prev_v = float(WHEEL_DEFAULT_IDX)   # last position, for tick pitch
        self._wheel_raf    = None                       # after() id of the ease loop
        self._wheel_last   = 0.0
        self._wheel_rowH   = 64.0
        self._wheel_layout = []                         # [(i, x, y)] for hit-testing
        self._wheel_drag   = None                       # (start_y, start_target)
        self._wheel_moved  = False
        self._wheel_img_cache = {}                      # blurred-label image cache
        self._wheel_imgs   = []                         # live refs for current frame
        self.selected_minutes = max(1, round(WHEEL_PRESETS[WHEEL_DEFAULT_IDX][1] / 60))
        self._anim_from_min   = self.selected_minutes

        self._lock = None
        self._lock_after = None
        self._lock_win = None
        self._theme_cover = None
        self._theme_overlay_img = None
        self._theme_fallback = None

        self._build_window()
        self._build_chrome()
        self._build_body()
        self._go("timer")
        self._tick()
        self._sched_overlay = None
        self.after(3000, self._schedule_tick)

        try:
            self.attributes("-alpha", 0.0)
            self.after(30, lambda: animate(self, 320, lambda t: self._safe_alpha(self, _ease_out_cubic(t))))
        except Exception:
            pass

        if DEV_FORCE_SETUP_WIZARD or not self.data.get("setup_complete"):
            self.after(150, self._show_setup_wizard)
        self.after(200, self._resume_session_if_pending)

    # ── Window ──────────────────────────────────────────────────────────────

    def _build_window(self):
        self.title(APP_NAME)
        self.minsize(635, 394)
        self.configure(fg_color=C_BG)
        w, h = 726, 440
        # Set the centered position up front so the window never flashes at the
        # default top-left spot before _center_window's delayed correction lands.
        # Always measure the primary monitor, not whichever screen Tk thinks
        # it's on, so the window reliably lands on the user's main display.
        sw, sh = primary_screen_size() or (self.winfo_screenwidth(), self.winfo_screenheight())
        x, y = max(0, (sw - w) // 2), max(0, (sh - h) // 2)
        self.geometry(f"{w}x{h}+{x}+{y}")
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        # Re-center once mapped/laid out, in case the actual rendered size differs.
        self.after(100, self._center_window)
        self._tray = _TrayIcon()
        self._tray.start()
        self.after(400, self._tray_tick)

    def _tray_tick(self):
        """Keep the tray icon in step with the schedules, and run whatever the
        tray thread asked for. Polled rather than called back directly because
        the tray's message pump lives on another thread and Tk isn't reentrant."""
        if getattr(self, "_shutting_down", False):
            return
        try:
            armed = any(sc.get("enabled", True) for sc in self._schedules())
            n = sum(1 for sc in self._schedules() if sc.get("enabled", True))
            self._tray.update(armed, "All Block — {} schedule{} enabled".format(n, "" if n == 1 else "s")
                              if armed else "All Block")
            for act in self._tray.take_actions():
                if act == "show":
                    self._restore_from_tray()
                elif act == "quit":
                    # A session is a hard lockout by design — the tray must not
                    # become the back door out of one.
                    if getattr(self, "_lock", None) is None:
                        self._on_close()
                        return
        except Exception:
            pass
        self.after(500, self._tray_tick)

    def _restore_from_tray(self):
        try:
            self.deiconify()
            self.lift()
            self.focus_force()
        except Exception:
            pass

    def _on_close(self):
        """Clean up before destroying the window."""
        self._shutting_down = True
        try:
            self._tray.stop()
        except Exception:
            pass
        # The fullscreen lock overlay is kept alive (withdrawn, never destroyed)
        # for the whole app session to dodge a DWM ghosting bug, and is marked
        # topmost at the raw Win32 level (SetWindowPos/HWND_TOPMOST in
        # _force_fullscreen), not just via Tk's -topmost attribute. A hidden but
        # still-topmost HWND can leave Windows' activation state confused enough
        # that the titlebar close button needs a second click to register. We're
        # exiting for good here, so the ghosting concern no longer applies —
        # destroy it explicitly before tearing down the main window.
        lk = getattr(self, "_lock_win", None)
        if lk is not None:
            try:
                if lk.winfo_exists():
                    lk.destroy()
            except Exception:
                pass
            self._lock_win = None
        try:
            self.destroy()
        except Exception:
            pass

    def _center_window(self):
        try:
            self.update_idletasks()
            w = self.winfo_width()
            h = self.winfo_height()
            if w <= 1 or h <= 1:
                w, h = 726, 440
            sw, sh = primary_screen_size() or (self.winfo_screenwidth(), self.winfo_screenheight())
            x = max(0, (sw - w) // 2)
            y = max(0, (sh - h) // 2)
            self.geometry(f"+{x}+{y}")
        except Exception:
            pass

    # ── Chrome ────────────────────────────────────────────────────────────────

    def _build_chrome(self):
        top = ctk.CTkFrame(self, height=56, fg_color=C_BG, corner_radius=0)
        top.pack(side="top", fill="x")
        top.pack_propagate(False)

        wm = ctk.CTkFrame(top, fg_color="transparent")
        wm.pack(side="left", padx=28)
        if getattr(self, "_logo_img", None) is not None:
            ctk.CTkLabel(wm, text="", image=self._logo_img).pack(side="left", padx=(0, 10))
        label(wm, "A L L   B L O C K", size=16, weight="bold", color=C_INK).pack(side="left")
        label(wm, "•", size=14, weight="normal", color=C_MUTED).pack(side="left", padx=(10, 8))
        label(wm, f"v{VERSION}", size=11, weight="normal", color=C_FAINT).pack(side="left")

        right = ctk.CTkFrame(top, fg_color="transparent")
        right.pack(side="right", padx=22)

        admin_ok = is_admin()
        dot_color = C_GREEN if admin_ok else C_AMBER
        admin_wrap = ctk.CTkFrame(right, fg_color="transparent", cursor="hand2" if not admin_ok else "arrow")
        admin_wrap.pack(side="left", padx=(0, 8))
        ctk.CTkLabel(admin_wrap, text="", image=icon("dot", dot_color, 9)).pack(side="left", padx=(0, 6))
        admin_lbl = label(admin_wrap, "Admin" if admin_ok else "No Admin", size=11, weight="bold", color=dot_color)
        admin_lbl.pack(side="left")
        if not admin_ok:
            for w in (admin_wrap, admin_lbl):
                w.bind("<Button-1>", lambda _: elevate())

        sched_btn = ctk.CTkButton(
            right, text="Schedule", image=icon("calendar", C_MUTED, 13), compound="left",
            width=94, height=30, corner_radius=15,
            fg_color=C_CARD, hover_color=C_CARD2, text_color=C_MUTED,
            border_width=1, border_color=C_BORDER, cursor="hand2",
            font=ctk.CTkFont(size=11, weight="bold", family=FONT),
            command=self._show_schedules)
        sched_btn.pack(side="left", padx=(0, 8))
        bind_hover_sfx(sched_btn, _SFX_BTN_HOVER)
        bind_press_sfx(sched_btn)

        # Theme toggle sits in the header, where a one-click, frequently-used
        # switch belongs; stats — looked at occasionally — lives in Settings.
        rest_frames = theme_switch_frames(hover=False)
        hov_frames = theme_switch_frames(hover=True)
        last = len(rest_frames) - 1
        rest_idx = last if THEME == "dark" else 0

        theme_btn = ctk.CTkButton(
            right, text="", image=rest_frames[rest_idx],
            width=THEME_SWITCH_W, height=THEME_SWITCH_H, corner_radius=15,
            fg_color=C_CARD, hover_color=C_CARD2, text_color=C_MUTED,
            border_width=1, border_color=C_BORDER, cursor="hand2")
        theme_btn.pack(side="left", padx=(0, 8))

        st = {"hover": False, "busy": False}

        def show(pos):
            src = hov_frames if st["hover"] else rest_frames
            theme_btn.configure(image=src[max(0, min(last, int(round(pos))))])

        def set_hover(on):
            def handler(_e=None):
                st["hover"] = on
                if not st["busy"]:
                    show(rest_idx)
            return handler

        def flip(_e=None):
            if st["busy"] or getattr(self, "_theme_animating", False):
                return
            st["busy"] = True
            end = 0 if THEME == "dark" else last
            # Turn the glyph over first, then hand off to the circular reveal —
            # the button reacting *before* the world changes is what makes the
            # wipe read as a consequence of the click rather than a coincidence.
            animate(theme_btn, 160,
                    lambda t: show(rest_idx + (end - rest_idx) * _ease_out_cubic(t)))
            self.after(170, self._toggle_theme)

        theme_btn.configure(command=flip)
        for w in (theme_btn, *theme_btn.winfo_children()):
            w.bind("<Enter>", set_hover(True), add="+")
            w.bind("<Leave>", set_hover(False), add="+")
        bind_hover_sfx(theme_btn, _SFX_BTN_HOVER)
        bind_press_sfx(theme_btn)
        self._theme_btn = theme_btn

        settings_btn = ctk.CTkButton(
            right, text="", image=icon("gear", C_MUTED, 15),
            width=30, height=30, corner_radius=15,
            fg_color=C_CARD, hover_color=C_CARD2, text_color=C_MUTED,
            border_width=1, border_color=C_BORDER, cursor="hand2",
            command=self._show_settings)
        settings_btn.pack(side="left", padx=(0, 8))
        bind_hover_sfx(settings_btn, _SFX_BTN_HOVER)
        bind_press_sfx(settings_btn)

        # Theme toggle lives in the Settings page now — see _show_settings.

        # single-purpose focus timer — no page navigation
        self._nav_btns: dict[str, ctk.CTkButton] = {}
        self._nav_icon_kind = {}

    def _rebuild_ui(self):
        """Tear down and rebuild the whole UI for the current palette."""
        self.configure(fg_color=C_BG)
        for w in self.winfo_children():
            if w is getattr(self, "_theme_cover", None):
                continue
            try:
                w.destroy()
            except Exception:
                pass
        self._build_chrome()
        self._build_body()
        self._go(self.active_page)

    def _toggle_theme(self):
        if getattr(self, "_theme_animating", False):
            return
        play_tick(soft=True)
        old_bg = C_BG
        new = "dark" if THEME == "light" else "light"
        new_theme_bg = _PALETTES[new]["C_BG"]
        new_rgb = _hex(new_theme_bg)

        # origin of the circular reveal = centre of the theme button
        try:
            self.update_idletasks()
            W = max(1, self.winfo_width())
            H = max(1, self.winfo_height())
            b = self._theme_btn
            bx = b.winfo_rootx() - self.winfo_rootx() + b.winfo_width() / 2
            by = b.winfo_rooty() - self.winfo_rooty() + b.winfo_height() / 2
        except Exception:
            W = max(1, self.winfo_width()); H = max(1, self.winfo_height())
            bx, by = W - 40, 28

        self._theme_animating = True

        def finish():
            # bulletproof teardown: always remove the cover + clear the flag,
            # no matter what happened during the reveal.
            cover = getattr(self, "_theme_cover", None)
            self._theme_cover = None
            if cover is not None:
                try:
                    cover.destroy()
                except Exception:
                    pass
            self._theme_animating = False
            self._theme_overlay_img = None
            if getattr(self, "_theme_fallback", None) is not None:
                try:
                    self.after_cancel(self._theme_fallback)
                except Exception:
                    pass
                self._theme_fallback = None

        # 1) snapshot the CURRENT (still-live) UI so the circle sweeps out over the
        #    real page instead of an instant blank cover — everywhere the wipe
        #    hasn't reached yet still shows the actual old-theme content.
        try:
            rx, ry = self.winfo_rootx(), self.winfo_rooty()
            before = ImageGrab.grab(bbox=(rx, ry, rx + W, ry + H)).convert("RGB")
        except Exception:
            before = Image.new("RGB", (W, H), old_bg)   # flat fallback if grab is unavailable

        # Give the overlay its first frame BEFORE it's placed/lifted — an empty
        # Label defaults to a plain grey Tk background, and if it ever became
        # visible for even one idle cycle with that default (instead of the
        # snapshot), it would read as a flash right at the start of the wipe.
        first_frame = ImageTk.PhotoImage(before)
        overlay = tk.Label(self, bd=0, highlightthickness=0, bg=old_bg, image=first_frame)
        self._theme_overlay_img = first_frame
        overlay.place(x=0, y=0, width=W, height=H)
        tk.Misc.lift(overlay)
        self._theme_cover = overlay

        max_r = math.hypot(max(bx, W - bx), max(by, H - by))

        def paint_wipe(t):
            if not overlay.winfo_exists():
                return
            r = _ease_out_cubic(t) * max_r
            frame = before.copy()
            ImageDraw.Draw(frame).ellipse([bx - r, by - r, bx + r, by + r], fill=new_rgb)
            img = ImageTk.PhotoImage(frame)
            overlay.configure(image=img)
            self._theme_overlay_img = img   # keep a live reference — Tk drops it otherwise

        def after_wipe():
            # 2) the screen now reads as a solid new_theme_bg — swap palette + rebuild
            #    invisibly, since the overlay is already indistinguishable from it.
            #    ctk.set_appearance_mode() forces its own repaint of every live CTk
            #    widget app-wide — call it AFTER the rebuild so that repaint lands on
            #    the freshly-built (already correctly-coloured) widgets instead of
            #    double-painting the old ones a beat before they get destroyed.
            apply_palette(new)
            try:
                self._rebuild_ui()
            except Exception:
                pass
            try:
                ctk.set_appearance_mode(new)
            except Exception:
                pass
            # customtkinter's automatic titlebar sync is disabled (see
            # sync_titlebar_dark_mode) since it hides/reshows the whole OS
            # window; drive the DWM call ourselves, still hidden behind the
            # overlay, with no hide/show involved.
            sync_titlebar_dark_mode(self, new == "dark")
            # rebuilding/re-theming leaves Tk with pending idle tasks — geometry
            # placement, CTk's own DPI image-rescale pass — that would otherwise
            # only flush once control returns to the mainloop. Force them to
            # settle now, while the overlay is still on top hiding it, so the
            # captured "after" frame below is the true final frame and nothing
            # is left to visibly snap into place once the overlay is gone.
            try:
                self.update_idletasks()
            except Exception:
                pass

            def reveal():
                # Some CTk widgets schedule their own delayed redraw (e.g. a
                # DPI-aware image rescale) a beat after creation, which can
                # land just after the update_idletasks() above rather than
                # before it. A short pause here, then one more idle flush,
                # gives that trailing redraw a chance to settle while the
                # overlay (a solid new_theme_bg fill at this point, since the
                # wipe circle has fully covered the window) is still up —
                # then just drop it straight to the real, settled UI. No
                # capture, no fade — a plain cut avoids every artifact that
                # came from compositing a captured "after" frame.
                try:
                    self.update_idletasks()
                except Exception:
                    pass
                finish()

            self.after(60, reveal)

        paint_wipe(0.0)   # paint the first frame immediately so there is never a hard snap
        # hard fallback: guarantee cleanup even if animate stalls
        self._theme_fallback = self.after(1800, finish)
        animate(self, 300, paint_wipe, after_wipe)

    def _build_body(self):
        self._content = ctk.CTkFrame(self, fg_color=C_BG, corner_radius=0)
        self._content.pack(side="top", fill="both", expand=True)

    def _clear_content(self):
        # Clear any stale wheel ease-loop handle so the wheel always starts responsive
        self._wheel_raf = None
        for w in self._content.winfo_children():
            try:
                w.destroy()
            except Exception:
                pass

    def _go(self, page: str):
        self._clear_content()
        builder = {
            "timer": self._page_timer,
        }.get(page, self._page_timer)
        self.active_page = page
        builder()

    # ─── Timer page (OptionWheel duration picker — reactbits.dev port) ──────────

    def _page_timer(self):
        wrap = ctk.CTkFrame(self._content, fg_color=C_BG)
        wrap.pack(fill="both", expand=True)

        # A rebuild (e.g. theme switch) makes a brand-new scene canvas — clear stale
        # handles and references so the wheel + button always start fresh.
        self._wheel_raf = None
        self._focus_btn = None
        self._focus_btn_pos = (None, None)   # force a fresh place() below — a stale (x, y) left
                                      # over from before the rebuild (e.g. a theme switch)
                                      # would make _draw_wheel() think the new button is
                                      # already positioned and skip placing it entirely.
        self._scene = tk.Canvas(wrap, bg=C_BG, highlightthickness=0, cursor="hand2")
        self._scene.pack(fill="both", expand=True, padx=6, pady=(0, 0))
        self._scene.bind("<Configure>", lambda e: self._draw_wheel())
        self._scene.bind("<Button-1>", self._wheel_press)
        self._scene.bind("<B1-Motion>", self._wheel_motion)
        self._scene.bind("<ButtonRelease-1>", self._wheel_release)
        self._scene.bind("<MouseWheel>", self._wheel_scroll)
        # arrow keys once the wheel has focus
        for seq in ("<Up>", "<Left>"):
            self._scene.bind(seq, lambda e: (self._wheel_apply(round(self._wheel_target) - 1, True), "break")[1])
        for seq in ("<Down>", "<Right>"):
            self._scene.bind(seq, lambda e: (self._wheel_apply(round(self._wheel_target) + 1, True), "break")[1])

        # The action button lives on the wheel canvas itself, parked right next to the
        # active number (positioned every frame in _draw_wheel). No bottom hint bar.
        if self.session_on:
            btn = ctk.CTkButton(
                self._scene, text="Stop", image=icon("stop", "white", 16), compound="left",
                height=58, corner_radius=29, fg_color=C_RED, hover_color="#a5342a",
                cursor="hand2", border_width=1, border_color="#7f2820",
                font=ctk.CTkFont(size=16, weight="bold", family=FONT),
                command=self._stop_session_clicked)
            smooth_hover(btn, C_RED, "#a5342a")
            bind_hover_sfx(btn, _SFX_BTN_HOVER)
            bind_press_sfx(btn)
            self._focus_btn = None
        else:
            # Compact pill with a play glyph + animated hover fade. In dark mode
            # C_BTN (a near-black grey) reads almost invisibly against the
            # near-black page — invert to a bright fill with dark text/glyph
            # instead, same idea as the light theme's dark-pill-on-light-page.
            if THEME == "dark":
                fill, hover_fill, txt = C_INK, _lerp_color(C_INK, "#000000", 0.10), C_BG
            else:
                fill, hover_fill, txt = C_BTN, C_BTN2, C_BG
            def _focus_pressed():
                play_sfx(_SFX_START)
                self._start_focus_session()
            self._focus_btn = ctk.CTkButton(
                self._scene, text="Start",
                image=icon("play", txt, 15), compound="left",
                width=160, height=56, corner_radius=28,
                fg_color=fill, hover_color=hover_fill, text_color=txt,
                border_width=1, border_color=fill,
                cursor="hand2",
                font=ctk.CTkFont(size=16, weight="bold", family=FONT),
                command=_focus_pressed)
            smooth_hover(self._focus_btn, fill, hover_fill)
            bind_hover_sfx(self._focus_btn, _SFX_BTN_HOVER)
            bind_press_sfx(self._focus_btn)
            # positioned in _draw_wheel

        self.after(20, self._draw_wheel)



    def _wheel_text_img(self, text, px, weight, hex_color, blur, rot, alpha):
        """Render one wheel label to a (gaussian-blurred, rotated, alpha-faded) image —
        the reactbits OptionWheel look where off-centre options blur + fade out. Heavily
        cached by quantised params so the ease loop reuses images and stays smooth."""
        px = max(8, int(px))
        bq = round(blur)                                # quantise blur to 1px
        aq = round(rot)                                 # quantise angle to 1°
        al = max(0, min(255, int(alpha * 255)))
        al = (al // 16) * 16                            # quantise opacity to 16 steps
        if al <= 4:
            return None
        key = (text, px, weight, hex_color, bq, aq, al)
        cache = self._wheel_img_cache
        img = cache.get(key)
        if img is not None:
            return img
        try:
            fpath = _font_file(weight)
            font = ImageFont.truetype(fpath, px) if fpath else ImageFont.load_default()
        except Exception:
            return None
        pad = bq * 3 + 6                                 # room so the blur isn't clipped
        tmp = Image.new("RGBA", (10, 10))
        bbox = ImageDraw.Draw(tmp).textbbox((0, 0), text, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        im = Image.new("RGBA", (tw + pad * 2, th + pad * 2), (0, 0, 0, 0))
        rgb = _hex(hex_color)
        ImageDraw.Draw(im).text((pad - bbox[0], pad - bbox[1]), text, font=font, fill=rgb + (al,))
        if bq > 0:
            im = im.filter(ImageFilter.GaussianBlur(bq))
        if aq:
            im = im.rotate(aq, expand=True, resample=Image.BICUBIC)
        photo = ImageTk.PhotoImage(im)
        if len(cache) > 600:                            # bound the cache
            cache.clear()
        cache[key] = photo
        return photo

    def _round_rect(self, c, x0, y0, x1, y1, r, **kw):
        """Draw a filled rounded rectangle on a tk.Canvas (used for the button shadow)."""
        r = min(r, (x1 - x0) / 2, (y1 - y0) / 2)
        pts = [
            x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r,
            x1, y1 - r, x1, y1, x1 - r, y1, x0 + r, y1,
            x0, y1, x0, y1 - r, x0, y0 + r, x0, y0,
        ]
        return c.create_polygon(pts, smooth=True, **kw)

    def _draw_wheel(self):
        c = getattr(self, "_scene", None)
        if c is None or not c.winfo_exists():
            return
        c.delete("all")
        self._wheel_imgs = []                 # fresh per-frame refs (keeps images alive)
        w, h = c.winfo_width(), c.winfo_height()
        if w < 40 or h < 40:
            return

        pos = self._wheel_pos
        n = len(WHEEL_PRESETS)
        rowH = max(66.0, min(132.0, h * 0.235))
        self._wheel_rowH = rowH
        inset_x = max(88.0, w * 0.17)
        cy = h * 0.5

        tilt_rad = math.radians(WHEEL_TILT)
        R = rowH / tilt_rad if tilt_rad > 0.0005 else 0.0
        base_px = max(28, int(rowH * 0.74))

        self._wheel_layout = []
        active_right = None
        order = sorted(range(n), key=lambda i: -abs(i - pos))  # far first, centre last
        for i in order:
            label_txt = WHEEL_PRESETS[i][0]
            d = i - pos
            dist = abs(d)
            if dist > 2.0:            # only a couple options each side stay on the wheel
                continue
            if R > 0:
                ang = max(-math.pi / 2, min(math.pi / 2, d * tilt_rad))
                y = R * math.sin(ang)
                x_off = -R * (1 - math.cos(ang)) * WHEEL_CURVE   # pull toward the left edge
                rot = -math.degrees(ang)                          # Tk angle is CCW-positive
            else:
                y, x_off, rot = d * rowH, 0.0, 0.0

            sx = inset_x + x_off
            sy = cy + y

            # quantise the distance so the VISUAL params (size/colour/blur/opacity)
            # snap to a small set of buckets → the ease loop reuses cached images and
            # only the position glides. Position stays continuous, so it's still smooth.
            dq = round(dist / 0.25) * 0.25

            # colour: muted when resting, blends to active ink near the centre
            p = max(0.0, 1 - min(dq, 1.0))
            col = _lerp_color(C_MUTED, C_INK, p)
            size = max(int(base_px * 0.62), int(base_px * (1 - min(dq, 3.0) * 0.09)))
            weight = "bold" if dist < 0.5 else "normal"
            # sharp in the centre; the immediate neighbour stays fairly crisp, then it
            # blurs harder with distance (offset keeps the ±1 option readable). Dark
            # mode's higher contrast makes the same blur look stronger, so it scales
            # down slightly there to keep the two themes visually identical.
            blur_scale = 0.85 if THEME == "dark" else 1.0
            blur = max(0.0, min(dq, 2.0) - 1.0) * WHEEL_BLUR * blur_scale * 0.5

            # opacity fades with distance ONLY (a function of the quantised dq), so the
            # image cache reuses a tiny fixed set of frames → fast. Reaching ~0 before
            # an option nears the canvas edge also removes the hard cut-off.
            alpha = max(0.0, 1.0 - dq * 0.42)
            if alpha <= 0.03:
                continue

            photo = self._wheel_text_img(label_txt, size, weight, col, blur, rot, alpha)
            if photo is not None:
                c.create_image(sx, sy, image=photo, anchor="w")
                self._wheel_imgs.append(photo)            # keep a ref so Tk won't GC it
                if dist < 0.5:                            # the active (centre) option
                    active_right = sx + photo.width()
            self._wheel_layout.append((i, sx, sy))

        # subtle centre guide so the selected slot reads clearly
        gx = inset_x - 22
        c.create_line(gx, cy, gx + 10, cy, fill=C_GHOST, width=2)

        # place the focus button (standard rounded CTkButton)
        if not self.session_on:
            fb = getattr(self, "_focus_btn", None)
            # CTkButton scales its own rendered width/height by the widget DPI-scaling
            # factor (e.g. 2x on a 200%-scaled display), so the 300x76 passed to its
            # constructor is a *logical* size, not the actual on-screen pixel size.
            # Query the real size here instead of assuming 300x76, or the button
            # overflows past the window edge on any scaled display.
            bw, bh = 196, 56
            if fb is not None and fb.winfo_exists():
                rw, rh = fb.winfo_reqwidth(), fb.winfo_reqheight()
                if rw > 1 and rh > 1:
                    bw, bh = rw, rh
            margin = max(48, w * 0.11)
            bcx = w - margin - bw / 2
            min_cx = (active_right if active_right is not None else inset_x) + 80 + bw / 2
            bcx = max(bcx, min_cx)
            btn_x = int(bcx - bw / 2)
            btn_y = int(cy - bh / 2)

            # Position the CTkButton (created in _page_timer). We call the base
            # tkinter place() directly (bypassing CTkButton's scaled override)
            # so coordinates aren't doubled under DPI scaling.
            if fb is not None and fb.winfo_exists():
                prev_x, prev_y = getattr(self, "_focus_btn_pos", (None, None))
                if prev_x != btn_x or prev_y != btn_y:
                    try:
                        tk.Widget.place(fb, x=btn_x, y=btn_y)
                        self._focus_btn_pos = (btn_x, btn_y)
                    except Exception:
                        pass

    # exponential-smoothing ease loop: nudges _wheel_pos toward _wheel_target
    # every frame (reads the target live, so a single loop handles new inputs).
    def _wheel_loop(self):
        if self._wheel_raf is not None:
            return
        self._wheel_last = time.perf_counter()
        def step():
            c = getattr(self, "_scene", None)
            if c is None or not c.winfo_exists():
                self._wheel_raf = None
                return
            now = time.perf_counter()
            # only clamp the UPPER bound (guards against a huge jump after the
            # window was minimized/stalled) — clamping dt down to the nominal
            # 8ms frame budget made k assume less time passed than actually did
            # whenever a frame landed late (normal on Windows, whose timer
            # granularity is ~15-16ms), so motion fell behind and looked jittery.
            dt = min(now - self._wheel_last, 0.05)
            self._wheel_last = now
            k = 1 - math.exp(-dt / max(WHEEL_TAU, 0.001))
            cur, tgt = self._wheel_pos, self._wheel_target
            nxt = cur + (tgt - cur) * k
            settled = abs(tgt - nxt) < 0.001
            self._wheel_pos = tgt if settled else nxt
            self._draw_wheel()
            # NB: no stop_sfx() here. This fires the moment the wheel settles —
            # only a few ms after the detent tick started — and used to truncate
            # the tick mid-waveform. Cutting a waveform at non-zero amplitude is
            # a step discontinuity, which is audible as a hard "pop" no matter
            # what the sample data is. The tick is ~10ms and ends on its own.
            # schedule on the ROOT, not the scene canvas — a theme rebuild destroys
            # the canvas and would silently kill a canvas-scheduled callback, leaving
            # _wheel_raf stuck non-None so the wheel freezes.
            self._wheel_raf = None if settled else self.after(8, step)
        step()

    def _wheel_apply(self, value, snap, play_sound=True):
        n = len(WHEEL_PRESETS)
        v = max(0.0, min(n - 1, value))
        if snap:
            v = round(v)
        self._wheel_target = v
        idx = max(0, min(n - 1, int(round(v))))
        if idx != self._wheel_sel:
            self._wheel_sel = idx
            self.selected_minutes = max(1, round(WHEEL_PRESETS[idx][1] / 60))
            if play_sound:
                # The felt detent, NOT play_tick(soft=True). _CLICK_SOFT is a
                # 14ms 1150Hz click — that hard, high, abruptly-ended burst is
                # the "pop". _BLIP_* is the low, zero-pinned tick built for
                # exactly this, with the direction of travel in its pitch.
                play_sfx(_BLIP_UP if value > self._wheel_prev_v else _BLIP_DN)
        self._wheel_prev_v = v
        self._wheel_loop()

    def _wheel_scroll(self, event):
        step = -1 if event.delta > 0 else 1   # scroll up → earlier option
        self._wheel_apply(round(self._wheel_target) + step, True)

    def _wheel_press(self, event):
        self._scene.focus_set()
        self._wheel_drag = (event.y, self._wheel_target)
        self._wheel_moved = False

    def _wheel_motion(self, event):
        if self._wheel_drag is None:
            return
        start_y, start_t = self._wheel_drag
        dy = event.y - start_y
        if not self._wheel_moved and abs(dy) > 4:
            self._wheel_moved = True
        if self._wheel_moved:
            self._wheel_apply(start_t - dy / max(self._wheel_rowH, 1), False)

    def _wheel_release(self, event):
        if self._wheel_drag is None:
            return
        moved = self._wheel_moved
        self._wheel_drag = None
        if moved:
            self._wheel_apply(self._wheel_target, True)   # settle on nearest
        else:
            self._wheel_apply(self._nearest_wheel_idx(event.y), True)  # tap to select

    def _nearest_wheel_idx(self, ey):
        best, best_d = self._wheel_sel, 1e9
        for i, _sx, sy in self._wheel_layout:
            d = abs(sy - ey)
            if d < best_d:
                best, best_d = i, d
        return best

    # ─── (blocklist / settings pages removed — timer-only focus flow) ───────────────
    # ─── First-run setup ────────────────────────────────────────────────────────

    def _show_setup_wizard(self):
        if self.data.get("setup_complete") and not DEV_FORCE_SETUP_WIZARD:
            return
        # defensive re-sync: guards against the palette having been seeded
        # before self.data was ready, so the wizard always matches the OS
        # theme rather than whatever THEME happened to be at import time.
        apply_palette(detect_system_theme())

        # Flat, solid cover — no screen-grab/blur backdrop. A grabbed frame
        # includes whatever's behind the window's own rounded corners (the
        # desktop, usually black), which baked ugly dark corner triangles
        # into the overlay once blurred. A plain color is simpler, always
        # matches the current theme exactly, and can't carry that artifact.
        # relwidth/relheight (not fixed pixel width/height) so the cover
        # keeps filling the window if it's resized while the wizard is up.
        overlay = tk.Frame(self, bg=C_BG, highlightthickness=0, bd=0)
        overlay.place(relx=0, rely=0, relwidth=1, relheight=1)
        tk.Misc.lift(overlay)
        self._setup_overlay = overlay

        card = ctk.CTkFrame(overlay, fg_color=C_CARD, corner_radius=14,
                             border_width=1, border_color=C_BORDER)
        card.place(relx=0.5, rely=0.5, anchor="center")

        title_lbl = label(card, "Welcome to All Block", size=14, weight="bold", color=C_INK)
        title_lbl.pack(pady=(20, 3), padx=20, anchor="w")
        sub_lbl = label(card, "Quick one-time setup before your first focus session.", size=11, color=C_MUTED)
        sub_lbl.pack(padx=20, anchor="w")

        admin_ok = is_admin()
        dot = C_GREEN if admin_ok else C_AMBER
        row1 = ctk.CTkFrame(card, fg_color="transparent")
        row1.pack(fill="x", padx=20, pady=(18, 2))
        ctk.CTkLabel(row1, text="", image=icon("dot", dot, 7)).pack(side="left", padx=(0, 8))
        label(row1, "Administrator access" + (" — granted" if admin_ok else " — not granted"),
              size=11, weight="bold", color=C_INK if admin_ok else C_AMBER).pack(side="left")
        admin_lbl = label(card,
              "Needed to block websites and fully lock input during a session."
              if admin_ok else
              "Some blocking will be skipped without it. Restart All Block as admin to enable everything.",
              size=10, color=C_MUTED)
        admin_lbl.pack(anchor="w", padx=(35, 20))

        row2 = ctk.CTkFrame(card, fg_color="transparent")
        row2.pack(fill="x", padx=20, pady=(16, 0))
        self._resume_var = tk.BooleanVar(value=bool(self.data.get("auto_resume", True)))
        label(row2, "Resume on restart", size=11, color=C_INK).pack(side="left")
        sw = make_switch(row2, self._resume_var)
        sw.pack(side="right")
        resume_lbl = label(card, "Otherwise a restart silently ends the lock early.", size=10, color=C_FAINT)
        resume_lbl.pack(padx=20, pady=(2, 0), anchor="w")

        row3 = ctk.CTkFrame(card, fg_color="transparent")
        row3.pack(fill="x", padx=20, pady=(16, 0))
        self._shortcut_var = tk.BooleanVar(value=True)
        label(row3, "Create desktop shortcut", size=11, color=C_INK).pack(side="left")
        sw2 = make_switch(row3, self._shortcut_var)
        sw2.pack(side="right")

        def _finish():
            self.data["setup_complete"] = True
            self.data["auto_resume"] = bool(self._resume_var.get())
            save_data(self.data)
            set_resume_on_login(self.data["auto_resume"])
            if self._shortcut_var.get():
                create_desktop_shortcut()
            if getattr(self, "_setup_configure_id", None):
                try:
                    self.unbind("<Configure>", self._setup_configure_id)
                except Exception:
                    pass
                self._setup_configure_id = None
            try:
                overlay.destroy()
            except Exception:
                pass
            self._setup_overlay = None

        btn = ctk.CTkButton(card, text="Get Started", height=34, corner_radius=17,
                             fg_color=C_INK, hover_color=C_MUTED, text_color=C_BG,
                             font=ctk.CTkFont(size=12, weight="bold", family=FONT),
                             command=_finish)
        smooth_hover(btn, C_INK, C_MUTED)
        bind_hover_sfx(btn, _SFX_BTN_HOVER)
        bind_press_sfx(btn)
        btn.pack(side="bottom", pady=(18, 20), padx=20, fill="x")

        # The card has no fixed width — it auto-sizes around its widest packed
        # child (default pack behavior). Driving the wraplength of the three
        # text labels off the window's current width is what makes the whole
        # card track window size: shrink the wraplength and the card shrinks
        # with it. Forcing the frame's own width directly instead fights that
        # auto-fit and corrupts CTkFrame's rounded-corner canvas.
        def _rescale(_evt=None):
            W = max(1, self.winfo_width())
            card_w = max(260, min(320, int(W * 0.24)))
            sub_lbl.configure(wraplength=card_w - 40)
            admin_lbl.configure(wraplength=card_w - 55)
            resume_lbl.configure(wraplength=card_w - 40)

        _rescale()
        self._setup_configure_id = self.bind("<Configure>", _rescale, add="+")

    # ─── Settings ────────────────────────────────────────────────────────────────

    def _show_settings(self):
        overlay = tk.Frame(self, bg=C_BG, highlightthickness=0, bd=0)
        overlay.place(relx=0, rely=0, relwidth=1, relheight=1)
        tk.Misc.lift(overlay)
        self._settings_overlay = overlay

        # Wide and short rather than tall and narrow: four settings in a 2x2
        # grid of tiles reads in one glance and leaves the card well clear of
        # the window edges, where a single stacked column ran the full height.
        card = ctk.CTkFrame(overlay, fg_color=C_CARD, corner_radius=14,
                             border_width=1, border_color=C_BORDER, width=520)
        # Start at the animation's slide-in offset, not the final centered
        # spot — see _animate_modal_in for why placing it centered here would
        # cause a glitchy snap-then-jump-then-slide instead of one smooth move.
        card.place(relx=0.5, rely=0.5 + _MODAL_SLIDE, anchor="center")

        def _close(_evt=None):
            try:
                self.unbind("<Escape>", esc_id)
            except Exception:
                pass
            try:
                overlay.destroy()
            except Exception:
                pass
            self._settings_overlay = None

        header = ctk.CTkFrame(card, fg_color="transparent")
        header.pack(fill="x", padx=20, pady=(18, 0))
        label(header, "Settings", size=14, weight="bold", color=C_INK).pack(side="left")
        close_x = ctk.CTkButton(header, text="✕", width=26, height=26, corner_radius=13,
                                 fg_color="transparent", hover_color=C_CARD2, text_color=C_MUTED,
                                 font=ctk.CTkFont(size=12, weight="bold", family=FONT),
                                 command=_close)
        close_x.pack(side="right")
        bind_hover_sfx(close_x, _SFX_BTN_HOVER)
        bind_press_sfx(close_x)

        # Sound is a mute control, not a feature to advertise — an icon-only
        # button tucked beside the close X, quiet enough to ignore until you
        # want it, instead of a labelled row competing with real settings.
        sfx_var = tk.BooleanVar(value=bool(self.data.get("sfx_enabled", True)))
        sfx_btn = ctk.CTkButton(header, text="", width=26, height=26, corner_radius=13,
                                 fg_color="transparent", hover_color=C_CARD2,
                                 image=icon("sound" if sfx_var.get() else "mute", C_FAINT, 14),
                                 command=lambda: _toggle_sfx())
        sfx_btn.pack(side="right", padx=(0, 4))

        def _toggle_sfx():
            global SFX_ENABLED
            enabled = not bool(sfx_var.get())
            sfx_var.set(enabled)
            SFX_ENABLED = enabled
            self.data["sfx_enabled"] = enabled
            save_data(self.data)
            if enabled:
                play_tick(soft=True)
            else:
                # kill the queue *and* whatever is mid-playback, so silence
                # lands on the click rather than after the current sound ends
                stop_sfx()
            if sfx_btn.winfo_exists():
                sfx_btn.configure(image=icon("sound" if enabled else "mute", C_FAINT, 14))

        admin_ok = is_admin()
        dot = C_GREEN if admin_ok else C_AMBER
        row1 = ctk.CTkFrame(card, fg_color="transparent")
        row1.pack(fill="x", padx=20, pady=(14, 2))
        ctk.CTkLabel(row1, text="", image=icon("dot", dot, 7)).pack(side="left", padx=(0, 8))
        label(row1, "Administrator access" + (" — granted" if admin_ok else " — not granted"),
              size=11, weight="bold", color=C_INK if admin_ok else C_AMBER).pack(side="left")
        admin_lbl = label(card,
              "Website blocking and the strongest input lock are active."
              if admin_ok else
              "Some blocking is skipped without it. Restart All Block as admin to enable everything.",
              size=10, color=C_MUTED)
        admin_lbl.pack(anchor="w", padx=(35, 20))

        grid = ctk.CTkFrame(card, fg_color="transparent")
        grid.pack(fill="x", padx=20, pady=(14, 0))
        grid.grid_columnconfigure(0, weight=1, uniform="tile")
        grid.grid_columnconfigure(1, weight=1, uniform="tile")

        def tile(r, c, title, caption, icon_kind, accent):
            """One settings cell: a tinted icon badge, title/caption, and a
            control docked right. The badge's accent colour is what actually
            breaks up the grid — four identical beige tiles read as one flat
            block, four tinted category colours read as a settings menu."""
            t = ctk.CTkFrame(grid, fg_color=C_CARD2, corner_radius=10)
            t.grid(row=r, column=c, sticky="nsew", padx=(0, 5) if c == 0 else (5, 0),
                   pady=(0, 8) if r == 0 else 0)
            inner = ctk.CTkFrame(t, fg_color="transparent")
            inner.pack(fill="x", padx=12, pady=10)
            badge = ctk.CTkFrame(inner, fg_color=_lerp_color(C_CARD2, accent, 0.18),
                                  corner_radius=8, width=30, height=30)
            badge.pack(side="left", padx=(0, 10))
            badge.pack_propagate(False)
            ctk.CTkLabel(badge, text="", image=icon(icon_kind, accent, 15)).pack(expand=True)
            txt = ctk.CTkFrame(inner, fg_color="transparent")
            txt.pack(side="left", fill="x", expand=True)
            label(txt, title, size=11, weight="bold", color=C_INK).pack(anchor="w")
            label(txt, caption, size=9, color=C_MUTED).pack(anchor="w")
            return inner

        resume_cell = tile(0, 0, "Resume on restart", "Survives a reboot mid-lock.",
                            "timer", C_GREEN)
        resume_var = tk.BooleanVar(value=bool(self.data.get("auto_resume", True)))

        def _on_resume_toggle():
            enabled = bool(resume_var.get())
            self.data["auto_resume"] = enabled
            save_data(self.data)
            set_resume_on_login(enabled)

        make_switch(resume_cell, resume_var, command=_on_resume_toggle).pack(side="right")

        stats_cell = tile(0, 1, "Session stats", "Focus time, streak, history.",
                           "chart", C_AMBER)
        stats_btn = ctk.CTkButton(stats_cell, text="View", width=64, height=26, corner_radius=13,
                                   fg_color=C_CARD, hover_color=C_GHOST, text_color=C_INK,
                                   font=ctk.CTkFont(size=11, weight="bold", family=FONT),
                                   command=lambda: (_close(), self._show_stats()))
        stats_btn.pack(side="right")
        bind_hover_sfx(stats_btn, _SFX_BTN_HOVER)
        bind_press_sfx(stats_btn)

        sched_cell = tile(1, 0, "Schedules", "Blocks that start themselves.",
                          "calendar", C_BLUE)
        sched_btn = ctk.CTkButton(sched_cell, text="Manage", width=64, height=26, corner_radius=13,
                                   fg_color=C_CARD, hover_color=C_GHOST, text_color=C_INK,
                                   font=ctk.CTkFont(size=11, weight="bold", family=FONT),
                                   command=lambda: (_close(), self._show_schedules()))
        sched_btn.pack(side="right")
        bind_hover_sfx(sched_btn, _SFX_BTN_HOVER)
        bind_press_sfx(sched_btn)

        shortcut_cell = tile(1, 1, "Desktop shortcut", "Launch it from the desktop.",
                             "link", C_RED)
        shortcut_btn = ctk.CTkButton(shortcut_cell, text="Create", width=64, height=26, corner_radius=13,
                                      fg_color=C_CARD, hover_color=C_GHOST, text_color=C_INK,
                                      font=ctk.CTkFont(size=11, weight="bold", family=FONT),
                                      command=lambda: _create_shortcut())
        shortcut_btn.pack(side="right")
        bind_hover_sfx(shortcut_btn, _SFX_BTN_HOVER)
        bind_press_sfx(shortcut_btn)

        def _create_shortcut():
            create_desktop_shortcut()
            shortcut_btn.configure(text="Done", state="disabled")
            self.after(1600, lambda: shortcut_btn.configure(text="Create", state="normal")
                       if shortcut_btn.winfo_exists() else None)

        close_btn = ctk.CTkButton(card, text="Close", height=34, corner_radius=17,
                                   fg_color=C_INK, hover_color=C_MUTED, text_color=C_BG,
                                   font=ctk.CTkFont(size=12, weight="bold", family=FONT),
                                   command=_close)
        smooth_hover(close_btn, C_INK, C_MUTED)
        bind_hover_sfx(close_btn, _SFX_BTN_HOVER)
        bind_press_sfx(close_btn)
        close_btn.pack(side="bottom", pady=(16, 18), padx=20, fill="x")

        esc_id = self.bind("<Escape>", _close, add="+")
        _animate_modal_in(card)

    # ─── Session stats ───────────────────────────────────────────────────────────

    def _compute_stats(self):
        sessions = self.data.get("sessions", [])
        total_minutes = sum(float(s.get("duration", 0)) for s in sessions)
        days = set()
        for s in sessions:
            try:
                days.add(datetime.fromisoformat(s["date"]).date())
            except Exception:
                pass
        streak = 0
        cur = datetime.now().date()
        if cur not in days:
            cur -= timedelta(days=1)   # no session yet today still counts an active streak
        while cur in days:
            streak += 1
            cur -= timedelta(days=1)
        # 4 keeps the card clear of the window edge even on the shortest layout
        recent = list(reversed(sessions))[:4]
        return total_minutes, len(sessions), streak, recent

    def _show_stats(self):
        overlay = tk.Frame(self, bg=C_BG, highlightthickness=0, bd=0)
        overlay.place(relx=0, rely=0, relwidth=1, relheight=1)
        tk.Misc.lift(overlay)
        self._stats_overlay = overlay

        card = ctk.CTkFrame(overlay, fg_color=C_CARD, corner_radius=14,
                             border_width=1, border_color=C_BORDER, width=340)
        card.place(relx=0.5, rely=0.5 + _MODAL_SLIDE, anchor="center")

        def _close(_evt=None):
            try:
                self.unbind("<Escape>", esc_id)
            except Exception:
                pass
            try:
                overlay.destroy()
            except Exception:
                pass
            self._stats_overlay = None

        header = ctk.CTkFrame(card, fg_color="transparent")
        header.pack(fill="x", padx=20, pady=(20, 0))
        label(header, "Session Stats", size=14, weight="bold", color=C_INK).pack(side="left")
        close_x = ctk.CTkButton(header, text="✕", width=26, height=26, corner_radius=13,
                                 fg_color="transparent", hover_color=C_CARD2, text_color=C_MUTED,
                                 font=ctk.CTkFont(size=12, weight="bold", family=FONT),
                                 command=_close)
        close_x.pack(side="right")
        bind_hover_sfx(close_x, _SFX_BTN_HOVER)
        bind_press_sfx(close_x)

        label(card, "Your focus history at a glance.", size=11, color=C_MUTED).pack(padx=20, pady=(3, 0), anchor="w")

        total_minutes, count, streak, recent = self._compute_stats()

        stats_row = ctk.CTkFrame(card, fg_color="transparent")
        stats_row.pack(fill="x", padx=20, pady=(18, 4))

        def _stat_block(value, caption):
            blk = ctk.CTkFrame(stats_row, fg_color="transparent")
            label(blk, value, size=18, weight="bold", color=C_INK).pack(anchor="w")
            label(blk, caption, size=10, color=C_MUTED).pack(anchor="w")
            blk.pack(side="left", padx=(0, 24))

        _stat_block(fmt_duration_words(round(total_minutes * 60)), "Total focus time")
        _stat_block(str(count), "Sessions")
        _stat_block(f"{streak} day" + ("s" if streak != 1 else ""), "Current streak")

        ctk.CTkFrame(card, fg_color=C_BORDER, height=1).pack(fill="x", padx=20, pady=(16, 10))
        label(card, "Recent sessions", size=11, weight="bold", color=C_INK).pack(padx=20, anchor="w")

        hist_wrap = ctk.CTkFrame(card, fg_color="transparent")
        hist_wrap.pack(fill="x", padx=20, pady=(6, 4))

        if not recent:
            label(hist_wrap, "No sessions yet — start one to see it here.",
                  size=10, color=C_MUTED).pack(anchor="w", pady=(0, 4))
        else:
            for s in recent:
                row = ctk.CTkFrame(hist_wrap, fg_color="transparent")
                row.pack(fill="x", pady=2)
                try:
                    when = datetime.fromisoformat(s.get("date", "")).strftime("%b %d, %I:%M %p")
                except Exception:
                    when = "—"
                label(row, when, size=10, color=C_MUTED).pack(side="left")
                dur_sec = round(float(s.get("duration", 0)) * 60)
                label(row, fmt_duration_words(dur_sec), size=10, color=C_INK).pack(side="right")

        close_btn = ctk.CTkButton(card, text="Close", height=34, corner_radius=17,
                                   fg_color=C_INK, hover_color=C_MUTED, text_color=C_BG,
                                   font=ctk.CTkFont(size=12, weight="bold", family=FONT),
                                   command=_close)
        smooth_hover(close_btn, C_INK, C_MUTED)
        bind_hover_sfx(close_btn, _SFX_BTN_HOVER)
        bind_press_sfx(close_btn)
        close_btn.pack(side="bottom", pady=(14, 20), padx=20, fill="x")

        esc_id = self.bind("<Escape>", _close, add="+")
        _animate_modal_in(card)

    # ─── Scheduler ───────────────────────────────────────────────────────────────

    def _schedules(self) -> list:
        return self.data.setdefault("schedules", [])

    def _schedule_tick(self):
        """Own slow loop rather than riding _tick — schedules only need
        minute-ish resolution and this keeps the 500ms countdown path clean."""
        if not self.winfo_exists() or getattr(self, "_shutting_down", False):
            return
        try:
            self._fire_due_schedules()
        except Exception:
            pass
        self.after(15000, self._schedule_tick)

    def _fire_due_schedules(self):
        if self.session_on or getattr(self, "_transitioning", False):
            return
        now = datetime.now()
        today = now.date().isoformat()
        for sc in self._schedules():
            if not sc.get("enabled", True):
                continue
            try:
                days = [int(d) for d in sc.get("days", [])]
            except Exception:
                continue
            if now.weekday() not in days or sc.get("last_fired") == today:
                continue
            start = now.replace(hour=int(sc.get("hour", 9)), minute=int(sc.get("minute", 0)),
                                second=0, microsecond=0)
            if not (0 <= (now - start).total_seconds() <= SCHEDULE_GRACE_S):
                continue
            self._start_session_secs(int(sc.get("seconds", 1800)))
            # only burn today's slot if the block actually started — a failed
            # start (e.g. no admin rights) should still be retried in-window
            if self.session_on:
                sc["last_fired"] = today
                save_data(self.data)
            return

    def _show_schedules(self):
        overlay = tk.Frame(self, bg=C_BG, highlightthickness=0, bd=0)
        overlay.place(relx=0, rely=0, relwidth=1, relheight=1)
        tk.Misc.lift(overlay)
        self._sched_overlay = overlay

        card = ctk.CTkFrame(overlay, fg_color=C_CARD, corner_radius=14,
                             border_width=1, border_color=C_BORDER, width=420)
        card.place(relx=0.5, rely=0.5 + _MODAL_SLIDE, anchor="center")

        def _close(_evt=None):
            try:
                self.unbind("<Escape>", esc_id)
            except Exception:
                pass
            try:
                overlay.destroy()
            except Exception:
                pass
            self._sched_overlay = None

        header = ctk.CTkFrame(card, fg_color="transparent")
        header.pack(fill="x", padx=20, pady=(20, 0))
        label(header, "Schedules", size=14, weight="bold", color=C_INK).pack(side="left")
        close_x = ctk.CTkButton(header, text="✕", width=26, height=26, corner_radius=13,
                                 fg_color="transparent", hover_color=C_CARD2, text_color=C_MUTED,
                                 font=ctk.CTkFont(size=12, weight="bold", family=FONT),
                                 command=_close)
        close_x.pack(side="right")
        bind_hover_sfx(close_x, _SFX_BTN_HOVER)
        bind_press_sfx(close_x)

        subtitle = label(card, "Blocks that start themselves, automatically.",
                         size=11, color=C_MUTED)
        subtitle.pack(padx=20, pady=(3, 0), anchor="w")

        body = ctk.CTkFrame(card, fg_color="transparent")
        body.pack(fill="x", padx=20, pady=(12, 16))

        def show_subtitle(on):
            """The tagline explains what schedules are for — useful over an empty
            list, dead weight over rows that already demonstrate it, and at the
            free limit the card needs those pixels to stay inside the window."""
            if on:
                subtitle.pack(before=body, padx=20, pady=(3, 0), anchor="w")
            else:
                subtitle.pack_forget()

        def clear_body():
            for w in body.winfo_children():
                w.destroy()

        def render_list():
            clear_body()
            items = self._schedules()
            show_subtitle(not items)
            if not items:
                label(body, "Nothing scheduled yet — add one to block on autopilot.",
                      size=10, color=C_MUTED).pack(anchor="w", pady=(0, 6))
            host = body
            # The card is centred and sized to its content, so every extra row
            # pushes it toward the window edges. Past the cap the list stops
            # growing and says how many it's holding back instead — a scrollable
            # frame can't be height-pinned (CTk blocks grid_propagate and the
            # scrollbar drives the frame taller than the canvas asks for), so
            # the only reliable ceiling is not to render the overflow at all.
            # When there is an overflow line it takes a row's place rather than
            # stacking on top of the full set, so the tallest the list can ever
            # get is the same either way.
            cap = MAX_VISIBLE_SCHEDULE_ROWS
            if len(items) > cap:
                cap -= 1
            shown, hidden = items[:cap], items[cap:]

            for idx, sc in enumerate(shown):
                on = bool(sc.get("enabled", True))
                row = ctk.CTkFrame(host, fg_color=C_CARD2, corner_radius=10)
                row.pack(fill="x", pady=3)
                inner = ctk.CTkFrame(row, fg_color="transparent")
                inner.pack(fill="x", padx=12, pady=8)

                txt = ctk.CTkFrame(inner, fg_color="transparent")
                txt.pack(side="left", fill="x", expand=True)
                label(txt, f"{int(sc.get('hour', 9)):02d}:{int(sc.get('minute', 0)):02d}",
                      size=15, weight="bold", color=C_INK if on else C_FAINT).pack(anchor="w")
                label(txt, f"{days_label(sc.get('days', []))}  ·  {fmt_duration_words(int(sc.get('seconds', 1800)))}",
                      size=10, color=C_MUTED).pack(anchor="w")

                def _delete(i=idx):
                    try:
                        self._schedules().pop(i)
                    except IndexError:
                        pass
                    save_data(self.data)
                    render_list()

                del_btn = ctk.CTkButton(inner, text="✕", width=24, height=24, corner_radius=12,
                                         fg_color="transparent", hover_color=C_CARD, text_color=C_MUTED,
                                         font=ctk.CTkFont(size=11, weight="bold", family=FONT),
                                         command=_delete)
                del_btn.pack(side="right", padx=(10, 0))
                bind_hover_sfx(del_btn, _SFX_BTN_HOVER)
                bind_press_sfx(del_btn)

                var = tk.BooleanVar(value=on)

                def _toggle(i=idx, v=var):
                    try:
                        self._schedules()[i]["enabled"] = bool(v.get())
                    except IndexError:
                        return
                    save_data(self.data)
                    play_tick(soft=True)
                    render_list()

                sw = make_switch(inner, var, command=_toggle)
                sw.pack(side="right")

            if hidden:
                label(body, f"+{len(hidden)} more not shown", size=10, color=C_FAINT
                      ).pack(anchor="w", pady=(6, 0))

            if len(items) >= SCHEDULE_LIMIT:
                gate = ctk.CTkFrame(body, fg_color=C_CARD2, corner_radius=10,
                                     border_width=1, border_color=C_BORDER)
                gate.pack(fill="x", pady=(12, 0))
                # One line, not a headline plus a line: "Schedule limit reached"
                # only restated what the missing add button already showed, and
                # the card has to clear the window bottom at the limit.
                label(gate, f"Limit of {SCHEDULE_LIMIT} schedules reached. Delete one to add another.",
                      size=11, color=C_INK).pack(anchor="w", padx=14, pady=(10, 12))
            else:
                left = f"   ({SCHEDULE_LIMIT - len(items)} left)"
                add = ctk.CTkButton(body, text=f"+  New schedule{left}", height=34, corner_radius=17,
                                     fg_color=C_INK, hover_color=C_MUTED, text_color=C_BG,
                                     font=ctk.CTkFont(size=12, weight="bold", family=FONT),
                                     command=render_form)
                smooth_hover(add, C_INK, C_MUTED)
                bind_hover_sfx(add, _SFX_BTN_HOVER)
                bind_press_sfx(add)
                add.pack(fill="x", pady=(12, 0))

        def render_form():
            clear_body()
            picked = {0, 1, 2, 3, 4}          # weekdays is the overwhelmingly common case

            label(body, "Days", size=11, weight="bold", color=C_INK).pack(anchor="w")
            days_row = ctk.CTkFrame(body, fg_color="transparent")
            days_row.pack(fill="x", pady=(6, 14))
            chips = {}

            def paint_chip(i):
                sel = i in picked
                chips[i].configure(fg_color=C_INK if sel else C_CARD2,
                                   text_color=C_BG if sel else C_MUTED)

            def toggle_day(i):
                picked.symmetric_difference_update({i})
                paint_chip(i)

            for i, dn in enumerate(DAY_NAMES):
                b = ctk.CTkButton(days_row, text=dn[0], width=34, height=34, corner_radius=17,
                                   fg_color=C_CARD2, text_color=C_MUTED, hover_color=C_BORDER,
                                   border_width=1, border_color=C_BORDER, cursor="hand2",
                                   font=ctk.CTkFont(size=11, weight="bold", family=FONT),
                                   command=lambda i=i: toggle_day(i))
                b.pack(side="left", padx=(0, 6))
                chips[i] = b
                bind_hover_sfx(b, _SFX_BTN_HOVER)
                bind_press_sfx(b)
                paint_chip(i)

            fields = ctk.CTkFrame(body, fg_color="transparent")
            fields.pack(fill="x", pady=(0, 16))

            tcol = ctk.CTkFrame(fields, fg_color="transparent")
            tcol.pack(side="left")
            label(tcol, "Start time", size=11, weight="bold", color=C_INK).pack(anchor="w")
            time_e = ctk.CTkEntry(tcol, width=92, height=34, corner_radius=8,
                                   fg_color=C_CARD2, border_color=C_BORDER, border_width=1,
                                   text_color=C_INK, justify="center",
                                   font=ctk.CTkFont(size=14, weight="bold", family=FONT))
            time_e.insert(0, "09:00")
            time_e.pack(anchor="w", pady=(6, 0))

            dcol = ctk.CTkFrame(fields, fg_color="transparent")
            dcol.pack(side="left", padx=(18, 0))
            label(dcol, "Duration", size=11, weight="bold", color=C_INK).pack(anchor="w")
            dur_menu = ctk.CTkOptionMenu(dcol, width=140, height=34, corner_radius=8,
                                          values=[l for l, _ in SCHEDULE_DURATIONS],
                                          fg_color=C_CARD2, button_color=C_CARD2,
                                          button_hover_color=C_BORDER, text_color=C_INK,
                                          dropdown_fg_color=C_CARD, dropdown_text_color=C_INK,
                                          dropdown_hover_color=C_CARD2,
                                          font=ctk.CTkFont(size=12, weight="bold", family=FONT),
                                          dropdown_font=ctk.CTkFont(size=12, family=FONT))
            dur_menu.set(SCHEDULE_DURATIONS[SCHEDULE_DEFAULT_IDX][0])
            dur_menu.pack(anchor="w", pady=(6, 0))

            def _save():
                hhmm = parse_hhmm(time_e.get())
                if hhmm is None:
                    self._toast("Enter a start time as HH:MM — e.g. 09:00.", success=False)
                    return
                if not picked:
                    self._toast("Pick at least one day.", success=False)
                    return
                if len(self._schedules()) >= SCHEDULE_LIMIT:
                    self._toast(f"Limit is {SCHEDULE_LIMIT} schedules — delete one to add another.", success=False)
                    render_list()
                    return
                secs = dict(SCHEDULE_DURATIONS)[dur_menu.get()]
                self._schedules().append({
                    "days": sorted(picked), "hour": hhmm[0], "minute": hhmm[1],
                    "seconds": int(secs), "enabled": True, "last_fired": ""})
                save_data(self.data)
                self._toast("Schedule saved.", success=True)
                render_list()

            btns = ctk.CTkFrame(body, fg_color="transparent")
            btns.pack(fill="x")
            cancel = ctk.CTkButton(btns, text="Cancel", width=110, height=34, corner_radius=17,
                                    fg_color=C_CARD2, hover_color=C_BORDER, text_color=C_MUTED,
                                    font=ctk.CTkFont(size=12, weight="bold", family=FONT),
                                    command=render_list)
            cancel.pack(side="left")
            bind_hover_sfx(cancel, _SFX_BTN_HOVER)
            bind_press_sfx(cancel)

            save_b = ctk.CTkButton(btns, text="Save schedule", height=34, corner_radius=17,
                                    fg_color=C_INK, hover_color=C_MUTED, text_color=C_BG,
                                    font=ctk.CTkFont(size=12, weight="bold", family=FONT),
                                    command=_save)
            smooth_hover(save_b, C_INK, C_MUTED)
            bind_hover_sfx(save_b, _SFX_BTN_HOVER)
            bind_press_sfx(save_b)
            save_b.pack(side="right", fill="x", expand=True, padx=(10, 0))

        render_list()
        esc_id = self.bind("<Escape>", _close, add="+")
        _animate_modal_in(card)

    # ─── Resume after restart ───────────────────────────────────────────────────

    def _resume_session_if_pending(self):
        if self.session_on:
            return
        state = load_session_state()
        if not state:
            return
        try:
            end_dt = datetime.fromisoformat(state["end"])
        except Exception:
            clear_session_state()
            return
        remaining = (end_dt - datetime.now()).total_seconds()
        if remaining <= 1:
            clear_session_state()
            return
        self.session_on = True
        self.session_total = int(state.get("total") or remaining)
        self.session_end = end_dt
        self._pending_session_secs = None
        self._show_lock()

    # ─── Session control ────────────────────────────────────────────────────────

    def _start_session(self, minutes: int):
        self._start_session_secs(int(minutes) * 60)

    def _start_session_secs(self, seconds: int):
        # guard against double-clicks / re-entrancy during the transition
        if self.session_on or getattr(self, "_transitioning", False):
            return
        if not is_admin():
            self._toast('Admin required — click "No Admin" above to enable full blocking first.', success=False)
            return
        seconds = max(1, int(seconds))
        self.session_on    = True
        self.session_total = seconds
        # the countdown must start *after* the lock overlay's fade-in completes, so that
        # the very first frame the user sees at fullscreen reads the full duration. We
        # stash the length here and stamp session_end inside _on_transition_done.
        self._pending_session_secs = seconds
        self.session_end   = None
        self.data.setdefault("sessions", []).append({
            "date": datetime.now().isoformat(), "duration": round(seconds / 60, 2)})
        save_data(self.data)
        self._show_lock()

    def _start_focus_session(self):
        """Called by the Start Focus button when clicked — starts session with current wheel selection."""
        secs = WHEEL_PRESETS[self._wheel_sel][1] + 1  # +1s internal buffer for fade-in
        self._start_session_secs(secs)

    def _stop_session_clicked(self):
        play_sfx(_SFX_CANCEL)
        self._stop_session()

    def _stop_session(self):
        self.session_on  = False
        self.session_end = None
        clear_session_state()
        self._hide_lock()

        def _return_to_timer():
            if self.winfo_exists():
                self._go("timer")
        # let the lock overlay fade out before swapping the page back in
        if getattr(self, "_transitioning", False):
            self.after(250 + 20, _return_to_timer)
        else:
            _return_to_timer()

    # ─── Fullscreen focus-lock overlay ─────────────────────────────────────────

    def _safe_alpha(self, win, a):
        try:
            if win.winfo_exists():
                win.attributes("-alpha", a)
        except Exception:
            pass

    def _force_fullscreen(self, lk):
        """Size the OS window via Win32 directly — consistent within our process at any DPI,
        unlike tk's -fullscreen/geometry which were getting dropped or half-scaled."""
        try:
            lk.update_idletasks()
            user32 = ctypes.windll.user32
            # proper 64-bit-safe signatures — default ctypes int types truncate HWNDs
            user32.GetParent.restype    = wintypes.HWND
            user32.GetParent.argtypes   = [wintypes.HWND]
            user32.SetWindowPos.restype = wintypes.BOOL
            user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND,
                                            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                            wintypes.UINT]
            gsm = user32.GetSystemMetrics
            x, y, w, h = gsm(76), gsm(77), gsm(78), gsm(79)   # virtual screen (all monitors)
            if w <= 0 or h <= 0:
                x, y, w, h = 0, 0, gsm(0), gsm(1)             # fallback: primary monitor
            hwnd = user32.GetParent(lk.winfo_id()) or lk.winfo_id()
            self._lock_hwnd = hwnd
            HWND_TOPMOST = wintypes.HWND(-1)
            SWP_SHOWWINDOW = 0x0040
            user32.SetWindowPos(hwnd, HWND_TOPMOST, int(x), int(y), int(w), int(h), SWP_SHOWWINDOW)
            self._lock_geo = (int(w), int(h))
        except Exception:
            self._lock_hwnd = None
            try:
                lk.geometry(f"{self.winfo_screenwidth()}x{self.winfo_screenheight()}+0+0")
            except Exception:
                pass

    def _ensure_lock_window(self):
        """Build the full-screen overlay ONCE and keep it withdrawn. The grey square
        was DWM caching the last frame of a top-most borderless window when it was
        DESTROYED. So we never destroy it — sessions just deiconify / withdraw the
        same window, which unmaps cleanly with no ghost."""
        lk = getattr(self, "_lock_win", None)
        if lk is not None and lk.winfo_exists():
            return lk
        lk = tk.Toplevel(self)
        lk.withdraw()                        # created hidden; only ever shown full-size
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        lk.overrideredirect(True)
        lk.geometry(f"{sw}x{sh}+0+0")
        try:
            lk.attributes("-topmost", True)  # NO -alpha → non-layered
        except Exception:
            pass
        cv = tk.Canvas(lk, bg=C_BG, highlightthickness=0, bd=0)
        cv.pack(fill="both", expand=True)
        self._lock_canvas = cv
        for seq in ("<Alt-F4>", "<Control-w>", "<Control-q>", "<Alt-Tab>"):
            lk.bind(seq, lambda e: "break")
        lk.protocol("WM_DELETE_WINDOW", lambda: None)
        self._lock_win = lk
        return lk

    def _show_lock(self):
        if self._lock is not None:
            return
        set_taskbar_visible(False)

        lk = self._ensure_lock_window()
        self._lock = lk
        self._lock_hwnd = None
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self._lock_geo = (sw, sh)
        # repaint for the current theme, then show the (already full-size) window
        lk.configure(bg=C_BG)
        self._lock_canvas.configure(bg=C_BG)
        lk.geometry(f"{sw}x{sh}+0+0")

        lk.update_idletasks()
        lk.deiconify()
        try:
            lk.attributes("-topmost", True)
        except Exception:
            pass
        self._force_fullscreen(lk)
        lk.after(50, lambda: self._lock is lk and lk.winfo_exists() and self._force_fullscreen(lk))
        lk.after(60, lambda: self._safe_focus(lk))

        # smooth transition: fade the lock overlay in, then (only at the end) hard-
        # engage the input block + the tick that draws the lock screen content.
        try:
            lk.attributes("-alpha", 0.0)
        except Exception:
            pass
        self._transitioning = True

        def _on_transition_done():
            self._transitioning = False
            # stamp the session end now (after fade-in) so the on-screen countdown
            # starts at the full duration the moment fullscreen appears
            secs = getattr(self, "_pending_session_secs", None)
            if secs:
                self.session_end = datetime.now() + timedelta(seconds=secs)
            if self.session_end:
                save_session_state(self.session_end, self.session_total)
            block_user_input(True)   # mouse + keyboard truly disabled for the session
            self._lock_tick()

        animate(lk, 250, lambda t: self._safe_alpha(lk, _ease_out_cubic(t)),
                on_done=_on_transition_done)

    def _safe_focus(self, win):
        try:
            if win is self._lock and win.winfo_exists():
                win.focus_force()
        except Exception:
            pass

    def _lock_tick(self):
        lk = self._lock
        if lk is None or not lk.winfo_exists():
            return
        cv = self._lock_canvas
        sw, sh = cv.winfo_width(), cv.winfo_height()
        if sw < 10:
            sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        cv.delete("all")
        remaining = int((self.session_end - datetime.now()).total_seconds()) if self.session_end else 0

        # keep it above everything and holding the keyboard
        try:
            lk.attributes("-topmost", True)
            lk.lift()
            if lk.focus_displayof() is not lk:
                lk.focus_force()
        except Exception:
            pass
        # snap back instantly if a touchpad gesture (show-desktop / app-switch) shoves it away
        hwnd = getattr(self, "_lock_hwnd", None)
        if hwnd:
            try:
                u = ctypes.windll.user32
                u.ShowWindow(hwnd, 5)                                     # SW_SHOW (undo minimise)
                u.SetWindowPos(hwnd, wintypes.HWND(-1), 0, 0, 0, 0, 0x0003 | 0x0040)  # TOPMOST, NOMOVE|NOSIZE|SHOW
                u.SetForegroundWindow(hwnd)
            except Exception:
                pass
        # re-assert the hard input lock every frame so a Ctrl+Alt+Del bounce can't unlock it
        block_user_input(True)
        if _INPUT_BLOCKER.panic:            # secret bailout combo tripped → bail out
            self._stop_session()
            play_sfx(_SFX_CANCEL)
            self._toast("Session ended early.", success=True)
            return

        cx, cy = sw / 2, sh / 2

        pulse = 0.5 + 0.5 * math.sin(time.perf_counter() * 1.6)
        pr = 5 + pulse * 4
        cv.create_oval(cx - pr, cy - sh * 0.26 - pr, cx + pr, cy - sh * 0.26 + pr,
                       fill=_lerp_color(C_CARD2, C_INK, pulse), outline="")

        # NEGATIVE font sizes = pixels → DPI-stable, sharp, and never overlapping
        head = max(20, int(sh * 0.028))
        cv.create_text(cx, cy - sh * 0.19, text="D E V I C E   B L O C K E D",
                       fill=C_FAINT, font=(FONT, -head, "bold"))

        big = max(64, int(sh * 0.22))
        cv.create_text(cx, cy - big * 0.02, text=fmt_countdown(remaining),
                       fill=C_INK, font=(FONT, -big, "bold"))

        total = max(1, self.session_total)
        frac = max(0.0, min(1.0, remaining / total))
        bw = sw * 0.34
        bx0 = cx - bw / 2
        by = cy + big * 0.62
        cv.create_line(bx0, by, bx0 + bw, by, fill=C_CARD2, width=4, capstyle="round")
        if frac > 0:
            cv.create_line(bx0, by, bx0 + bw * frac, by, fill=C_INK, width=4, capstyle="round")

        sub = max(18, int(sh * 0.022))
        cv.create_text(cx, by + sub * 2.4,
                       text=f"{fmt_duration_words(max(1, self.session_total - 1))} session in progress",
                       fill=C_MUTED, font=(FONT, -sub))

        self._lock_after = lk.after(33, self._lock_tick)

    def _hide_lock(self):
        self._lock = None
        self._lock_hwnd = None
        if self._lock_after is not None:
            try:
                self.after_cancel(self._lock_after)
            except Exception:
                pass
            self._lock_after = None
        block_user_input(False)   # release the mouse/keyboard as the lock fades away
        set_taskbar_visible(True)  # and bring the taskar back during the transition
        lk = getattr(self, "_lock_win", None)
        # smooth transition: fade the overlay out before unmapping it (keeps the same
        # persistent window so the DWM ghost square never reappears).
        if lk is not None and lk.winfo_exists():
            self._transitioning = True

            def _done():
                self._transitioning = False
                try:
                    lk.withdraw()
                except Exception:
                    pass
                # A topmost overrideredirect window can leave Windows' OS-level
                # foreground/activation stuck on it even after withdraw() unmaps
                # it, which is why the titlebar close button sometimes needed a
                # second click to register — the first click just reactivated
                # the main window. Reclaim foreground explicitly.
                try:
                    self.lift()
                    self.focus_force()
                except Exception:
                    pass
            animate(lk, 250, lambda t: self._safe_alpha(lk, _ease_in_out(1 - t)), on_done=_done)
        else:
            self._transitioning = False

    # ─── Timer loop ───────────────────────────────────────────────────────────

    def _tick(self):
        if not self.winfo_exists() or getattr(self, "_shutting_down", False):
            return
        try:
            if self.session_on and self.session_end:
                remaining = int((self.session_end - datetime.now()).total_seconds())
                if remaining <= 0:
                    self._stop_session()
                    play_sfx(_SFX_COMPLETE)
                    self._toast("Session complete!", success=True)
        except Exception:
            pass
        self.after(500, self._tick)

    # ─── Toast ────────────────────────────────────────────────────────────────

    def _toast(self, msg: str, success: bool = True):
        try:
            t = ctk.CTkToplevel(self)
            t.withdraw()                 # THE grey square: CTkToplevel otherwise maps a
                                         # default ~200px frame at the top-left corner for
                                         # a frame before geometry() moves it, and DWM
                                         # caches that flash as a ghost. Stay hidden until
                                         # we are already sized AND positioned.
            t.overrideredirect(True)
            t.attributes("-topmost", True)
            t.configure(fg_color="transparent")
            color = C_GREEN if success else C_RED
            f = ctk.CTkFrame(t, fg_color=color, corner_radius=12)
            f.pack(padx=2, pady=2)
            ctk.CTkLabel(f, text=msg, font=ctk.CTkFont(size=13, weight="bold", family=FONT), text_color="white").pack(padx=18, pady=12)
            t.update_idletasks()
            x = self.winfo_x() + (self.winfo_width() - t.winfo_reqwidth()) // 2
            y = self.winfo_y() + self.winfo_height() - 90
            t.geometry(f"+{x}+{y}")
            t.update_idletasks()
            t.deiconify()                # now show it — already at its final place/size
            # CTkToplevel re-asserts its own geometry ~200ms after creation, which can
            # bounce it back to the corner; pin it again after that settles.
            t.after(230, lambda: (t.winfo_exists() and t.geometry(f"+{x}+{y}")))
            def _dismiss():
                try:
                    t.destroy()
                except Exception:
                    pass
                # Same foreground-activation quirk as the lock overlay: a topmost
                # toast that disappears can leave Windows' activation stuck on it,
                # so the titlebar close button needs an extra click afterward.
                try:
                    self.lift()
                    self.focus_force()
                except Exception:
                    pass
            t.after(2200, _dismiss)
        except Exception:
            pass

# ─── Entry ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    set_dpi_awareness()      # native-resolution rendering → sharp full-screen text
    raise_timer_resolution() # steady after()-callback timing → smooth wheel easing
    set_app_user_model_id()  # our taskbar icon/identity, before any window exists
    ensure_logo_files()      # generate assets/logo.png + logo.ico on first run
    ensure_admin()   # relaunch elevated so BlockInput + website blocking work
    load_fonts()         # register bundled Montserrat before any widget is built
    app = AllBlock()
    try:
        app.mainloop()
    finally:
        block_user_input(False)      # never leave the machine with input disabled
        set_taskbar_visible(True)    # never leave the taskbar hidden on exit
        try:
            ctypes.windll.winmm.timeEndPeriod(1)   # release the 1ms timer request
        except Exception:
            pass
