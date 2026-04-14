"""
Kaira -- Voice Dictation Tool
=============================
Personal voice-to-text tool for Billy Wellington.

Controls:
  Ctrl+Win       = Start / stop dictation
  Click widget   = Start / stop dictation
  Ctrl+Shift+Q   = Quit

Floating overlay with mic-reactive waveform (Warm Ember theme).
Uses PyQt6 with QPainter for the overlay -- true per-pixel alpha
transparency via DWM compositing. Works on Intel UHD + Win11.
Qt.WindowType.Tool hides from taskbar automatically.

Runs silently on startup. All activity logged to logs/ folder.
Requires: OpenAI API key in .env file
"""

import os
import sys
import wave
import time
import math
import logging
import threading
from datetime import datetime

import numpy as np
import sounddevice as sd
import pyperclip
from pynput import keyboard as pynput_kb
from openai import OpenAI
from dotenv import load_dotenv

from PyQt6.QtWidgets import QApplication, QWidget
from PyQt6.QtCore import Qt, QTimer, QRectF, QPointF, pyqtSignal
from PyQt6.QtGui import (
    QPainter, QColor, QBrush, QPen, QFont, QFontMetrics,
    QPainterPath, QMouseEvent,
)

try:
    import msvcrt
except ImportError:
    msvcrt = None

# --- Paths ---
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(SCRIPT_DIR, "logs")
AUDIO_DIR = os.path.join(LOG_DIR, "audio")
LOCK_PATH = os.path.join(SCRIPT_DIR, "kaira.lock")
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(AUDIO_DIR, exist_ok=True)

AUDIO_RETENTION_DAYS = 7

# --- Logging ---
def _next_log_file():
    today = datetime.now().strftime('%Y-%m-%d')
    n = 1
    while True:
        path = os.path.join(LOG_DIR, f"{today}_{n:03d}.log")
        if not os.path.exists(path):
            return path
        n += 1

log_file = _next_log_file()
_handlers = [logging.FileHandler(log_file, encoding="utf-8")]
if sys.stdout is not None:
    _handlers.append(logging.StreamHandler(sys.stdout))
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=_handlers,
)
log = logging.getLogger("kaira")

# --- Load API key ---
load_dotenv(os.path.join(SCRIPT_DIR, ".env"))
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# --- Audio settings ---
SAMPLE_RATE = 16000
CHANNELS = 1


# ============================================================
# AUDIO RECORDER
# ============================================================

class Recorder:
    def __init__(self):
        self.is_recording = False
        self.audio_chunks = []
        self.stream = None
        self.current_level = 0.0

    def start(self):
        self.audio_chunks = []
        self.current_level = 0.0
        self.is_recording = True
        self.stream = sd.InputStream(
            samplerate=SAMPLE_RATE, channels=CHANNELS,
            dtype="int16", callback=self._callback, blocksize=1024,
        )
        self.stream.start()

    def _callback(self, indata, frames, time_info, status):
        if self.is_recording:
            self.audio_chunks.append(indata.copy())
            rms = np.sqrt(np.mean(indata.astype(np.float32) ** 2))
            self.current_level = min(1.0, rms / 3000.0)

    def stop(self):
        self.is_recording = False
        if self.stream:
            self.stream.stop()
            self.stream.close()
            self.stream = None
        if not self.audio_chunks:
            return None, 0.0
        audio = np.concatenate(self.audio_chunks, axis=0)
        duration = len(audio) / SAMPLE_RATE
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        path = os.path.join(AUDIO_DIR, f"{stamp}.wav")
        with wave.open(path, "wb") as wf:
            wf.setnchannels(CHANNELS)
            wf.setsampwidth(2)
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(audio.tobytes())
        return path, duration


# ============================================================
# TRANSCRIPTION & PASTE
# ============================================================

def transcribe(audio_path):
    with open(audio_path, "rb") as f:
        response = client.audio.transcriptions.create(
            model="whisper-1", file=f,
            language="en", response_format="text",
        )
    return response.strip()


def cleanup_old_audio():
    cutoff = time.time() - (AUDIO_RETENTION_DAYS * 86400)
    removed = 0
    try:
        for name in os.listdir(AUDIO_DIR):
            if not name.lower().endswith(".wav"):
                continue
            path = os.path.join(AUDIO_DIR, name)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    removed += 1
            except OSError:
                pass
    except OSError:
        return
    if removed:
        log.info(f"[CLEANUP] Removed {removed} audio file(s) older than {AUDIO_RETENTION_DAYS}d")


def paste_text(text):
    pyperclip.copy(text)
    time.sleep(0.1)
    ctrl = pynput_kb.Controller()
    ctrl.press(pynput_kb.Key.ctrl)
    ctrl.press('v')
    ctrl.release('v')
    ctrl.release(pynput_kb.Key.ctrl)


# ============================================================
# SINGLE-INSTANCE LOCK
# ============================================================

def acquire_single_instance_lock():
    if msvcrt is None:
        return True
    try:
        lock_fd = open(LOCK_PATH, "a+")
        msvcrt.locking(lock_fd.fileno(), msvcrt.LK_NBLCK, 1)
        lock_fd.seek(0)
        lock_fd.truncate()
        lock_fd.write(str(os.getpid()))
        lock_fd.flush()
        return lock_fd
    except OSError:
        return None


# ============================================================
# PYQT6 OVERLAY WIDGET
# ============================================================

# States
IDLE = "idle"
RECORDING = "recording"
PROCESSING = "processing"
DONE = "done"

# Theme (Warm Ember)
BG_COLOR = QColor(13, 10, 8, 220)
BORDER_IDLE = QColor(255, 149, 0, 50)
BORDER_ACTIVE = QColor(255, 149, 0, 100)
ACCENT = QColor(255, 149, 0)
ACCENT_DIM = QColor(255, 149, 0, 90)
DOT_DIM = QColor(136, 136, 136, 180)
TEXT_COLOR = QColor(230, 225, 220, 160)
GLOW_COLOR = QColor(255, 149, 0, 25)
REC_DOT = QColor(255, 68, 0)


class KairaWidget(QWidget):
    """Floating pill overlay -- transparent, borderless, always-on-top, no taskbar."""

    state_changed = pyqtSignal(str)

    # Geometry
    PILL_H = 28
    CORNER_R = 14
    IDLE_W = 28       # perfect circle
    HOVER_W = 90
    REC_W = 180
    PROC_W = 140
    DONE_W = 28

    def __init__(self):
        super().__init__()

        # Window flags: frameless + always-on-top + Tool (no taskbar)
        # NoDropShadowWindowHint prevents Qt from requesting DWM shadow
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.NoDropShadowWindowHint
        )
        # Per-pixel alpha via DWM (NOT color-key -- works on Intel UHD)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)

        self._state = IDLE
        self.recorder = Recorder()
        self._hovered = False
        self._drag_pos = None
        self._anim_t = 0.0
        self._spinner_angle = 0.0
        self._bar_levels = [0.0] * 20
        self._bar_targets = [0.0] * 20

        # Animated width
        self._target_w = float(self.IDLE_W)
        self._current_w = float(self.IDLE_W)

        # Position: bottom-center, above taskbar
        screen = QApplication.primaryScreen().availableGeometry()
        widget_area_w = 300  # max possible pill width + glow margin
        self._home_x = screen.x() + (screen.width() - widget_area_w) // 2
        self._home_y = screen.y() + screen.height() - self.PILL_H - 25
        self.setGeometry(self._home_x, self._home_y, widget_area_w, self.PILL_H + 12)

        # Animation tick (~60fps)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(16)

        self.setMouseTracking(True)
        self.state_changed.connect(self._set_state)

    def showEvent(self, event):
        """Nuke all DWM decorations after the native window handle exists."""
        super().showEvent(event)
        try:
            import ctypes
            import ctypes.wintypes
            hwnd = ctypes.wintypes.HWND(int(self.winId()))
            dwmapi = ctypes.windll.dwmapi
            user32 = ctypes.windll.user32

            # 1. Disable non-client area rendering
            val = ctypes.c_int(1)  # DWMNCRP_DISABLED
            dwmapi.DwmSetWindowAttribute(hwnd, 2, ctypes.byref(val), 4)

            # 2. Disable Win11 rounded corners
            val = ctypes.c_int(1)  # DWMWCP_DONOTROUND
            dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(val), 4)

            # 3. Suppress window border
            val = ctypes.c_uint(0xFFFFFFFE)  # DWMWA_COLOR_NONE
            dwmapi.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(val), 4)

            # 4. Set backdrop to NONE (no Mica/Acrylic)
            val = ctypes.c_int(0)  # DWMSBT_NONE
            dwmapi.DwmSetWindowAttribute(hwnd, 38, ctypes.byref(val), 4)

            # 5. Remove CS_DROPSHADOW from class style
            GCL_STYLE = -26
            CS_DROPSHADOW = 0x00020000
            style = user32.GetClassLongPtrW(hwnd, GCL_STYLE)
            if style & CS_DROPSHADOW:
                user32.SetClassLongPtrW(hwnd, GCL_STYLE, style & ~CS_DROPSHADOW)

            log.info("[UI] DWM decorations removed")
        except Exception as e:
            log.warning(f"[UI] DWM fix failed: {e}")

    # --- State machine ---

    def _set_state(self, state):
        self._state = state
        targets = {
            IDLE: self.IDLE_W,
            RECORDING: self.REC_W,
            PROCESSING: self.PROC_W,
            DONE: self.DONE_W,
        }
        self._target_w = float(targets.get(state, self.IDLE_W))

    def toggle_dictation(self):
        """Thread-safe toggle via signal."""
        if self._state in (IDLE, "hover"):
            self.state_changed.emit(RECORDING)
            self._start_recording()
        elif self._state == RECORDING:
            self._stop_recording()

    def _start_recording(self):
        self._bar_levels = [0.0] * 20
        self._bar_targets = [0.0] * 20
        log.info("[REC] Listening... speak now")
        self.recorder.start()

    def _stop_recording(self):
        self.state_changed.emit(PROCESSING)
        log.info("[STOP] Processing...")
        audio_path, duration = self.recorder.stop()
        if audio_path:
            log.info(f"[SAVED] {audio_path} ({duration:.1f}s)")
            log.info(f"[PROCESSING] Sending {duration:.1f}s to Whisper...")
            threading.Thread(
                target=self._transcribe_and_paste,
                args=(audio_path, duration),
                daemon=True,
            ).start()
        else:
            log.info("[SKIP] No audio captured")
            self.state_changed.emit(IDLE)

    def _transcribe_and_paste(self, audio_path, duration):
        try:
            start = time.time()
            text = transcribe(audio_path)
            elapsed = time.time() - start
            if text:
                paste_text(text)
                log.info(f"[PASTED] ({duration:.1f}s audio, {elapsed:.1f}s API) {text}")
            else:
                log.info("[SKIP] Empty transcription")
        except Exception as e:
            log.error(f"[ERROR] {e}")
            log.error(f"[RECOVERABLE] Audio saved at: {audio_path} -- retry with this file")
        finally:
            self.state_changed.emit(DONE)
            time.sleep(0.4)
            self.state_changed.emit(IDLE)

    # --- Animation loop ---

    def _tick(self):
        self._anim_t += 0.016

        # Smooth width interpolation
        diff = self._target_w - self._current_w
        self._current_w += diff * 0.18

        # Update bar levels from recorder
        if self._state == RECORDING and self.recorder.is_recording:
            level = self.recorder.current_level
            for i in range(20):
                variation = 0.6 + (hash((i, int(self._anim_t * 60))) % 100) / 150.0
                self._bar_targets[i] = min(1.0, level * variation * 3.5)

        # Smooth bars (fast attack, slow decay)
        for i in range(20):
            target = self._bar_targets[i]
            current = self._bar_levels[i]
            k = 0.35 if target > current else 0.12
            self._bar_levels[i] += (target - current) * k

        # Spinner
        if self._state == PROCESSING:
            self._spinner_angle = (self._spinner_angle + 5) % 360

        self.update()

    # --- Painting ---

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        w = self._current_w
        h = self.PILL_H
        r = self.CORNER_R
        # Center the pill horizontally in the widget
        x = (self.width() - w) / 2
        y = 4.0  # small top margin for glow

        pill = QPainterPath()
        pill.addRoundedRect(QRectF(x, y, w, h), r, r)

        # Outer glow (recording/processing only)
        if self._state in (RECORDING, PROCESSING):
            p.setPen(Qt.PenStyle.NoPen)
            for i in range(3):
                glow = QColor(255, 149, 0, 10 - i * 3)
                p.setBrush(QBrush(glow))
                expand = (3 - i) * 2
                glow_path = QPainterPath()
                glow_path.addRoundedRect(
                    QRectF(x - expand, y - expand, w + expand * 2, h + expand * 2),
                    r + expand, r + expand
                )
                p.drawPath(glow_path)

        # Background
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(BG_COLOR))
        p.drawPath(pill)

        # Border
        border_color = BORDER_ACTIVE if self._state != IDLE else BORDER_IDLE
        if self._hovered and self._state == IDLE:
            border_color = QColor(255, 149, 0, 80)
        p.setPen(QPen(border_color, 1.2))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(pill)

        # Content
        cx = x + w / 2
        cy = y + h / 2

        if self._state == IDLE:
            if self._hovered and self._current_w > 40:
                # Dot + text centered together
                font = QFont("Segoe UI", 8)
                font.setWeight(QFont.Weight.Medium)
                metrics = QFontMetrics(font)
                text = "Ctrl+Win"
                tw = metrics.horizontalAdvance(text)
                dot_r = 3.5
                gap = 6
                total = dot_r * 2 + gap + tw
                start_x = cx - total / 2
                self._draw_idle_dot(p, start_x + dot_r, cy)
                self._draw_hint(p, start_x + dot_r * 2 + gap, y, h, text)
            else:
                self._draw_idle_dot(p, cx, cy)
        elif self._state == RECORDING:
            self._draw_rec_dot(p, x, cy)
            self._draw_waveform(p, x, w, y, h)
        elif self._state == PROCESSING:
            # Spinner + text centered together
            font = QFont("Segoe UI", 8)
            font.setWeight(QFont.Weight.Medium)
            metrics = QFontMetrics(font)
            text = "Transcribing..."
            tw = metrics.horizontalAdvance(text)
            spinner_d = 12
            gap = 8
            total = spinner_d + gap + tw
            start_x = cx - total / 2
            self._draw_spinner(p, start_x + 6, cy)
            self._draw_hint(p, start_x + spinner_d + gap, y, h, text)
        elif self._state == DONE:
            self._draw_done_dot(p, cx, cy)

        p.end()

    def _draw_idle_dot(self, p, cx, cy):
        color = ACCENT if self._hovered else DOT_DIM
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(color))
        p.drawEllipse(QPointF(cx, cy), 3.5, 3.5)

    def _draw_rec_dot(self, p, pill_x, cy):
        # Pulsing red dot
        pulse = 0.5 + 0.5 * math.sin(self._anim_t * 6)
        alpha = int(180 + 75 * pulse)
        color = QColor(255, 68, 0, alpha)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(color))
        p.drawEllipse(QPointF(pill_x + 12, cy), 3.5, 3.5)

    def _draw_waveform(self, p, pill_x, pill_w, pill_y, pill_h):
        bar_count = len(self._bar_levels)
        bar_area_start = pill_x + 22
        bar_area_end = pill_x + pill_w - 8
        bar_area = bar_area_end - bar_area_start
        if bar_area <= 0:
            return
        spacing = bar_area / bar_count

        for i, level in enumerate(self._bar_levels):
            bx = bar_area_start + i * spacing + spacing / 2
            bar_h = max(2, level * (pill_h - 10))
            bar_w = max(1.5, spacing * 0.45)
            by = pill_y + pill_h / 2 - bar_h / 2

            alpha = int(120 + 135 * min(1.0, level))
            color = QColor(255, 149, 0, alpha)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(color))
            p.drawRoundedRect(QRectF(bx - bar_w / 2, by, bar_w, bar_h), 1, 1)

    def _draw_spinner(self, p, cx, cy):
        sr = 6
        rect = QRectF(cx - sr, cy - sr, sr * 2, sr * 2)
        # Background arc
        p.setPen(QPen(ACCENT_DIM, 2))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(rect)
        # Active arc
        pen = QPen(ACCENT, 2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.drawArc(rect, int(self._spinner_angle * 16), 270 * 16)

    def _draw_hint(self, p, left_x, pill_y, pill_h, text):
        """Left-aligned label, positioned right after the icon."""
        font = QFont("Segoe UI", 8)
        font.setWeight(QFont.Weight.Medium)
        p.setFont(font)
        p.setPen(QPen(TEXT_COLOR))
        metrics = QFontMetrics(font)
        ty = pill_y + pill_h / 2 + metrics.ascent() / 2 - 2
        p.drawText(QPointF(left_x, ty), text)

    def _draw_done_dot(self, p, cx, cy):
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(ACCENT))
        scale = 1.0 + 0.3 * math.sin(self._anim_t * 10)
        p.drawEllipse(QPointF(cx, cy), 4 * scale, 4 * scale)

    # --- Mouse events ---

    def enterEvent(self, event):
        self._hovered = True
        if self._state == IDLE:
            self._target_w = float(self.HOVER_W)

    def leaveEvent(self, event):
        self._hovered = False
        if self._state == IDLE:
            self._target_w = float(self.IDLE_W)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.toggle_dictation()

    def mouseMoveEvent(self, event):
        pass  # no drag for now


# ============================================================
# HOTKEY HANDLER
# ============================================================

def setup_hotkeys(widget):
    current_keys = set()

    def on_press(key):
        current_keys.add(key)
        ctrl = any(k in current_keys for k in (
            pynput_kb.Key.ctrl_l, pynput_kb.Key.ctrl_r, pynput_kb.Key.ctrl))
        win = any(k in current_keys for k in (
            pynput_kb.Key.cmd, pynput_kb.Key.cmd_l, pynput_kb.Key.cmd_r))
        shift = any(k in current_keys for k in (
            pynput_kb.Key.shift_l, pynput_kb.Key.shift_r, pynput_kb.Key.shift))

        if ctrl and win:
            widget.toggle_dictation()

        if ctrl and shift:
            vk = getattr(key, 'vk', None)
            char = getattr(key, 'char', None)
            if vk == 81 or (char and char.lower() == 'q'):
                log.info("[QUIT] Shutting down.")
                os._exit(0)

    def on_release(key):
        current_keys.discard(key)

    listener = pynput_kb.Listener(on_press=on_press, on_release=on_release)
    listener.daemon = True
    listener.start()


# ============================================================
# MAIN
# ============================================================

def main():
    lock = acquire_single_instance_lock()
    if lock is None:
        log.warning("Another Kaira instance is already running. Exiting.")
        return
    main._lock = lock

    if not os.getenv("OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY") == "your-key-here":
        log.error("No API key. Set OPENAI_API_KEY in .env file.")
        return

    try:
        default_input = sd.query_devices(kind="input")
        mic_name = default_input["name"]
    except Exception as e:
        log.error(f"No microphone found: {e}")
        return

    log.info("=" * 48)
    log.info("  Kaira -- Voice Dictation Tool (PyQt6)")
    log.info("=" * 48)
    log.info(f"  Mic: {mic_name}")
    log.info(f"  Log: {log_file}")
    log.info("  Ctrl+Win     = Start / stop dictation")
    log.info("  Click widget = Start / stop dictation")
    log.info("  Ctrl+Shift+Q = Quit")
    log.info("=" * 48)
    cleanup_old_audio()

    app = QApplication(sys.argv)
    widget = KairaWidget()
    widget.show()

    log.info(f"Widget pos: ({widget.x()}, {widget.y()})")
    log.info("Ready. Waiting for Ctrl+Win...")

    setup_hotkeys(widget)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
