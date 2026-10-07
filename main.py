"""
main.py
Entry point. Creates:
  - One always-on-top floating button, with a persistent drag lock
  - A system tray icon (right-click -> Open App / Quit)
  - A configurable global shortcut for the selected default action
  - A single app window with Home / Settings / About pages, each with a
    Back button, instead of separate popups
  - An on-screen status bubble near the floating button showing
    Processing / Done / Error, so the user always sees what's happening
  - Clean shutdown on Ctrl+C in the terminal (PowerShell) as well as from
    the tray menu / window close button

Run with:  python main.py
Stop with: Ctrl+C in the terminal, or Quit from the tray icon
Package with:  .\build_release.ps1
"""

import logging
import os
import signal
import sys
import subprocess
import threading
import traceback
import ctypes
import tkinter as tk
from tkinter import ttk
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pystray
from pynput import keyboard
from PIL import Image, ImageDraw, ImageTk

from config import load_settings, save_settings
from ai_provider import (
    DEFAULT_PROVIDER_ORDER,
    TEMPLATES,
    PROVIDER_DEFAULTS,
    PROVIDER_LABELS,
    provider_label,
    normalize_api_key,
    compatible_chat_completions_url,
    configured_providers,
    process_with_fallback,
    AIError,
)
from clipboard_helper import (
    get_selected_text,
    paste_text,
    paste_text_streaming,
    ClipboardError,
)


# ---------------------------------------------------------------------------
# Colors / fonts - one place to tweak the look
# ---------------------------------------------------------------------------
COLOR_BG = "#1e1e2e"
COLOR_BG_CARD = "#2a2a3d"
COLOR_ACCENT = "#3d8bfd"
COLOR_ACCENT_HOVER = "#5c9dff"
COLOR_TEXT = "#f0f0f5"
COLOR_SUBTEXT = "#9a9ab0"
COLOR_SUCCESS = "#3ddc84"
COLOR_ERROR = "#ff5c5c"
COLOR_WARN = "#ffb84d"

FONT_TITLE = ("Segoe UI", 15, "bold")
FONT_LABEL = ("Segoe UI", 10, "bold")
FONT_TEXT = ("Segoe UI", 10)
FONT_SMALL = ("Segoe UI", 8)
FONT_BTN = ("Segoe UI", 10, "bold")
ICON_NAME = "assets/promptify-icon.png"
FLOATING_TRANSPARENT_COLOR = "#010203"

LOGGER = logging.getLogger("Promptify")
LOG_DIR = None
LOG_FILE = None
INSTANCE_MUTEX = None
INSTANCE_MUTEX_NAME = "Local\\Promptify.SingleInstance.v1"
DEFAULT_HOTKEY = "<ctrl>+<alt>+f"
HOTKEY_MODIFIERS = ("ctrl", "alt", "shift", "cmd")
HOTKEY_MODIFIER_ALIASES = {
    "control": "ctrl",
    "win": "cmd",
    "windows": "cmd",
    "meta": "cmd",
}
HOTKEY_SPECIAL_KEYS = {
    "return": "enter",
    "escape": "esc",
    "del": "delete",
    "pageup": "page_up",
    "pagedown": "page_down",
}
HOTKEY_ALLOWED_KEYS = {
    "space", "tab", "enter", "esc", "delete", "insert", "home", "end",
    "page_up", "page_down", "up", "down", "left", "right",
}


def normalize_hotkey(value):
    """Return a validated pynput hotkey string with at least one modifier."""
    if not isinstance(value, str):
        raise ValueError("Enter a shortcut such as Ctrl+Alt+F.")

    parts = [part.strip().lower().strip("<>") for part in value.split("+")]
    parts = [HOTKEY_MODIFIER_ALIASES.get(part, part) for part in parts if part]
    modifiers = [part for part in parts if part in HOTKEY_MODIFIERS]
    keys = [part for part in parts if part not in HOTKEY_MODIFIERS]
    if len(modifiers) != len(set(modifiers)):
        raise ValueError("A shortcut cannot repeat the same modifier.")
    if not modifiers or len(keys) != 1:
        raise ValueError("Use one key and at least one modifier, such as Ctrl+Alt+F.")

    key = HOTKEY_SPECIAL_KEYS.get(keys[0], keys[0])
    if not (
        len(key) == 1 and key.isascii() and key.isalnum()
        or key in HOTKEY_ALLOWED_KEYS
        or key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24
    ):
        raise ValueError("Use a letter, number, function key, or supported navigation key.")

    ordered_modifiers = [modifier for modifier in HOTKEY_MODIFIERS if modifier in modifiers]
    formatted = [f"<{modifier}>" for modifier in ordered_modifiers]
    formatted.append(key if len(key) == 1 else f"<{key}>")
    return "+".join(formatted)


def format_hotkey(value):
    """Format a normalized pynput hotkey for display in Settings."""
    parts = [part.strip("<>") for part in value.split("+")]
    return "+".join(part.upper() if len(part) == 1 else part.title() for part in parts)


def acquire_single_instance():
    """Hold a named Windows mutex for this user's interactive session."""
    global INSTANCE_MUTEX
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = (
        ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p
    )
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    kernel32.CloseHandle.restype = ctypes.c_int

    ctypes.set_last_error(0)
    handle = kernel32.CreateMutexW(None, False, INSTANCE_MUTEX_NAME)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == 183:
        kernel32.CloseHandle(handle)
        return False
    INSTANCE_MUTEX = handle
    return True


def release_single_instance():
    global INSTANCE_MUTEX
    if INSTANCE_MUTEX:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel32.CloseHandle.restype = ctypes.c_int
        kernel32.CloseHandle(INSTANCE_MUTEX)
        INSTANCE_MUTEX = None


def _log_exception(context, exc, tb=None):
    LOGGER.error("%s failed (%s)", context, type(exc).__name__)
    for frame in traceback.extract_tb(tb or exc.__traceback__):
        LOGGER.error("  at %s:%d in %s", frame.filename, frame.lineno, frame.name)


def configure_logging():
    global LOG_DIR, LOG_FILE

    directories = []
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        directories.append(Path(local_app_data) / "Promptify" / "logs")
    directories.append(Path.home() / ".promptify" / "logs")

    for directory in directories:
        try:
            directory.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(
                directory / "promptify.log",
                maxBytes=1_000_000,
                backupCount=5,
                encoding="utf-8",
            )
        except OSError:
            continue

        LOG_DIR = directory
        LOG_FILE = directory / "promptify.log"
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s [%(threadName)s] %(message)s"
        ))
        LOGGER.setLevel(logging.INFO)
        LOGGER.addHandler(handler)
        LOGGER.propagate = False
        LOGGER.info("Logging initialized: %s", LOG_FILE)
        break
    else:
        LOGGER.addHandler(logging.NullHandler())
        return

    def handle_uncaught(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            return
        _log_exception("Uncaught exception", exc_value, exc_tb)

    def handle_thread_exception(args):
        LOGGER.error(
            "Unhandled thread exception in %s (%s)",
            args.thread.name,
            args.exc_type.__name__,
        )
        for frame in traceback.extract_tb(args.exc_traceback):
            LOGGER.error("  at %s:%d in %s", frame.filename, frame.lineno, frame.name)

    sys.excepthook = handle_uncaught
    threading.excepthook = handle_thread_exception


def resource_path(name):
    bundle_dir = getattr(sys, "_MEIPASS", None)
    if bundle_dir is not None:
        return Path(bundle_dir) / name

    compiled = globals().get("__compiled__")
    bundle_dir = getattr(compiled, "containing_dir", None)
    if bundle_dir is not None:
        candidate = Path(bundle_dir) / name
        if candidate.is_file():
            return candidate

        temp_dir = Path(os.environ.get("TEMP", Path.home()))
        extraction_dirs = sorted(
            temp_dir.glob(f"onefile_{os.getppid()}_*"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for extraction_dir in extraction_dirs:
            candidate = extraction_dir / name
            if candidate.is_file():
                return candidate

    return Path(__file__).resolve().parent / name


def make_tray_image():
    """Simple generated tray icon, no external image file needed."""
    icon_path = resource_path(ICON_NAME)
    try:
        return Image.open(icon_path).convert("RGBA").resize(
            (64, 64), Image.Resampling.LANCZOS
        )
    except (OSError, ValueError) as exc:
        LOGGER.warning("Could not load tray icon from %s (%s)", icon_path, type(exc).__name__)

    img = Image.new("RGBA", (64, 64), color=(0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((4, 4, 60, 60), fill=(61, 139, 253, 255))
    d.text((22, 18), "AI", fill=(255, 255, 255, 255))
    return img


class RoundButton(tk.Canvas):
    """A flat, hover-highlighted button - nicer than stock tk.Button."""

    def __init__(self, parent, text, command, bg=COLOR_ACCENT,
                 hover=COLOR_ACCENT_HOVER, fg="white", width=180, height=36,
                 font=FONT_BTN):
        super().__init__(parent, width=width, height=height,
                          bg=parent["bg"], highlightthickness=0)
        self.command = command
        self.bg = bg
        self.hover = hover
        self.rect = self.create_rectangle(0, 0, width, height, fill=bg, outline="")
        self.label = self.create_text(width / 2, height / 2, text=text,
                                       fill=fg, font=font)
        self.bind("<Enter>", lambda e: self.itemconfig(self.rect, fill=self.hover))
        self.bind("<Leave>", lambda e: self.itemconfig(self.rect, fill=self.bg))
        self.bind("<Button-1>", lambda e: self.command())
        self.config(cursor="hand2")

    def set_text(self, text):
        self.itemconfig(self.label, text=text)


class App:
    def __init__(self):
        self.settings = load_settings()
        self._known_api_keys = set()
        self._refresh_sensitive_values(self.settings)
        self._last_external_hwnd = None
        self._process_id = os.getpid()
        self._closing = False
        self._signal_after_id = None
        self._foreground_after_id = None
        self._hotkey_listener = None
        self._recording_listener = None
        self._hotkey_recording_context = None
        self._position_locked = self.settings.get("position_locked", False)

        self.root = tk.Tk()
        self.root.withdraw()  # main root stays hidden; we use Toplevels
        self.root.report_callback_exception = self._report_callback_exception

        # ---- App window (Home / Settings / About) ----
        self.app_win = None
        self.page_container = None
        self.current_page = None

        # ---- Floating button + status bubble ----
        self.floating_btn = None
        self.status_bubble = None
        self.status_label = None
        self.status_copy_button = None
        self._status_copy_text = ""
        self._status_after_id = None

        self.build_floating_button()
        self.build_tray_icon()
        if self.settings.get("credential_store_error"):
            self.root.after(
                250,
                lambda: self.show_status(
                    self.settings["credential_store_error"], COLOR_ERROR
                ),
            )

        # Clean shutdown on Ctrl+C from the terminal
        signal.signal(signal.SIGINT, self._on_sigint)
        # Tk doesn't process OS signals unless we wake it up periodically
        self._signal_after_id = self.root.after(200, self._pump_signals)
        self._foreground_after_id = self.root.after(100, self._track_foreground_window)
        try:
            self._configure_global_hotkey(
                self.settings.get("hotkey", DEFAULT_HOTKEY),
                self.settings.get("hotkey_enabled", True),
            )
        except Exception as exc:
            _log_exception("Registering global shortcut", exc)
            self.root.after(
                300,
                lambda: self.show_status(
                    "The global shortcut could not be registered. Change it in Settings.",
                    COLOR_ERROR, autohide_ms=7000,
                ),
            )

    # =========================================================================
    # Shutdown handling (Ctrl+C in PowerShell / terminal)
    # =========================================================================
    def _on_sigint(self, signum, frame):
        self.quit_app()

    def _pump_signals(self):
        # Tkinter's mainloop blocks signal delivery; waking up every 200ms
        # lets Python handle a pending Ctrl+C promptly instead of hanging.
        if not self._closing and self.root.winfo_exists():
            self._signal_after_id = self.root.after(200, self._pump_signals)

    def _track_foreground_window(self):
        try:
            user32 = ctypes.windll.user32
            user32.GetForegroundWindow.restype = ctypes.c_void_p
            user32.GetWindowThreadProcessId.argtypes = [
                ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)
            ]
            hwnd = user32.GetForegroundWindow()
            if hwnd:
                process_id = ctypes.c_ulong()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
                if process_id.value != self._process_id:
                    self._last_external_hwnd = int(hwnd)
        except Exception as exc:
            LOGGER.warning("Could not track foreground window (%s)", type(exc).__name__)
        finally:
            if not self._closing and self.root.winfo_exists():
                self._foreground_after_id = self.root.after(100, self._track_foreground_window)

    def quit_app(self):
        self._closing = True
        self._cancel_hotkey_recording()
        self._stop_global_hotkey()
        for after_id in (self._signal_after_id, self._foreground_after_id):
            if after_id:
                try:
                    self.root.after_cancel(after_id)
                except tk.TclError:
                    pass
        LOGGER.info("Application shutdown requested")
        try:
            if self.tray_icon:
                self.tray_icon.stop()
        except Exception:
            pass
        try:
            self.root.after(0, self.root.destroy)
        except Exception:
            sys.exit(0)

    def _report_callback_exception(self, exc, value, tb):
        _log_exception("Tkinter callback", value, tb)

    def _stop_global_hotkey(self):
        listener = self._hotkey_listener
        self._hotkey_listener = None
        if listener:
            listener.stop()
            if listener is not threading.current_thread():
                listener.join(timeout=1)

    def _start_global_hotkey(self, hotkey):
        normalized = normalize_hotkey(hotkey)
        listener = keyboard.GlobalHotKeys({
            normalized: self._dispatch_default_action,
        })
        started = False
        try:
            listener.start()
            started = True
            listener.wait()
            if not listener.is_alive():
                listener.join(timeout=0)
                raise RuntimeError("The keyboard listener stopped before it was ready.")
        except Exception:
            if started:
                listener.stop()
                listener.join(timeout=1)
            raise
        self._hotkey_listener = listener
        LOGGER.info("Global shortcut registered: %s", format_hotkey(normalized))
        return normalized

    def _configure_global_hotkey(self, hotkey, enabled):
        normalized = normalize_hotkey(hotkey)
        old_listener = self._hotkey_listener
        old_hotkey = getattr(self, "_registered_hotkey", None)
        self._stop_global_hotkey()
        try:
            if enabled:
                normalized = self._start_global_hotkey(normalized)
            self._registered_hotkey = normalized
            self._registered_hotkey_enabled = bool(enabled)
        except Exception:
            if old_listener and old_hotkey:
                try:
                    self._start_global_hotkey(old_hotkey)
                    self._registered_hotkey = old_hotkey
                    self._registered_hotkey_enabled = True
                except Exception as restore_exc:
                    _log_exception("Restoring previous global shortcut", restore_exc)
            raise
        return normalized

    def _dispatch_default_action(self):
        try:
            self.root.after(0, self._run_default_action)
        except tk.TclError:
            LOGGER.info("Shortcut ignored while application is closing")

    def _run_default_action(self):
        if self._closing:
            return
        action = self.settings.get("default_action", "rewrite_same")
        if action not in TEMPLATES:
            action = "rewrite_same"
        LOGGER.info("Default shortcut action triggered: %s", action)
        self.run_action(action)

    def _hotkey_key_name(self, key):
        modifier_aliases = {
            "ctrl_l": "ctrl", "ctrl_r": "ctrl",
            "alt_l": "alt", "alt_r": "alt", "alt_gr": "alt",
            "shift": "shift", "shift_l": "shift", "shift_r": "shift",
            "cmd": "cmd", "cmd_l": "cmd", "cmd_r": "cmd",
        }
        key_name = getattr(key, "name", None)
        if key_name in modifier_aliases:
            return modifier_aliases[key_name], True
        if isinstance(key, keyboard.KeyCode):
            if key.char and key.char.isascii() and key.char.isalnum():
                return key.char.lower(), False
            virtual_key = key.vk
            if virtual_key is not None:
                if 0x41 <= virtual_key <= 0x5A:
                    return chr(virtual_key).lower(), False
                if 0x30 <= virtual_key <= 0x39:
                    return chr(virtual_key), False
                if 0x70 <= virtual_key <= 0x87:
                    return f"f{virtual_key - 0x6F}", False
        if key_name:
            return HOTKEY_SPECIAL_KEYS.get(key_name, key_name), False
        return None, False

    def _set_hotkey_status(self, label, text, color):
        try:
            if label.winfo_exists():
                label.config(text=text, fg=color)
        except tk.TclError:
            pass

    def _finish_hotkey_recording(self, hotkey_var, status_label, record_button,
                                 captured_hotkey=None, cancelled=False):
        listener = self._recording_listener
        self._recording_listener = None
        self._hotkey_recording_context = None
        if listener:
            listener.stop()
            if listener is not threading.current_thread():
                listener.join(timeout=1)
        try:
            if record_button.winfo_exists():
                record_button.config(state="normal")
        except tk.TclError:
            pass

        if captured_hotkey:
            hotkey_var.set(format_hotkey(captured_hotkey))
            self._set_hotkey_status(
                status_label,
                f"Shortcut captured: {format_hotkey(captured_hotkey)}. Save to apply.",
                COLOR_SUCCESS,
            )
        elif cancelled:
            self._set_hotkey_status(status_label, "Shortcut recording cancelled.", COLOR_SUBTEXT)
        else:
            self._set_hotkey_status(
                status_label, "Shortcut unchanged. Save to apply other settings.", COLOR_SUBTEXT
            )

        if not self._closing:
            try:
                self._configure_global_hotkey(
                    self.settings.get("hotkey", DEFAULT_HOTKEY),
                    self.settings.get("hotkey_enabled", True),
                )
            except Exception as exc:
                _log_exception("Restoring shortcut after recording", exc)
                self._set_hotkey_status(
                    status_label, "Could not restore the saved shortcut.", COLOR_ERROR
                )

    def _cancel_hotkey_recording(self):
        listener = self._recording_listener
        self._recording_listener = None
        self._hotkey_recording_context = None
        if listener:
            listener.stop()
        if hasattr(self, "_hotkey_listener"):
            self._stop_global_hotkey()

    def start_hotkey_recording(self, hotkey_var, status_label, record_button):
        if self._recording_listener:
            self._finish_hotkey_recording(
                hotkey_var, status_label, record_button, cancelled=True
            )
            return

        self._stop_global_hotkey()
        modifiers = set()

        def on_press(key):
            key_name, is_modifier = self._hotkey_key_name(key)
            if key_name == "esc" and not is_modifier:
                self.root.after(
                    0,
                    lambda: self._finish_hotkey_recording(
                        hotkey_var, status_label, record_button, cancelled=True
                    ),
                )
                return False
            if is_modifier:
                modifiers.add(key_name)
                return
            if not modifiers or not key_name:
                return
            try:
                chord = normalize_hotkey(
                    "+".join([*sorted(modifiers), key_name])
                )
            except ValueError:
                return
            self.root.after(
                0,
                lambda chord=chord: self._finish_hotkey_recording(
                    hotkey_var, status_label, record_button,
                    captured_hotkey=chord,
                ),
            )
            return False

        def on_release(key):
            key_name, is_modifier = self._hotkey_key_name(key)
            if is_modifier:
                modifiers.discard(key_name)

        listener = keyboard.Listener(on_press=on_press, on_release=on_release)
        self._recording_listener = listener
        self._hotkey_recording_context = (hotkey_var, status_label, record_button)
        record_button.focus_set()
        record_button.config(state="disabled")
        self._set_hotkey_status(
            status_label, "Press a shortcut with Ctrl, Alt, Shift, or Win. Esc cancels.",
            COLOR_WARN,
        )
        try:
            listener.start()
        except Exception as exc:
            self._recording_listener = None
            record_button.config(state="normal")
            self._set_hotkey_status(status_label, f"Could not record shortcut: {exc}", COLOR_ERROR)
            try:
                self._configure_global_hotkey(
                    self.settings.get("hotkey", DEFAULT_HOTKEY),
                    self.settings.get("hotkey_enabled", True),
                )
            except Exception as restore_exc:
                _log_exception("Restoring shortcut after recording failure", restore_exc)
            raise

    def _persist_floating_position(self):
        if not self.floating_btn:
            return
        new_settings = dict(self.settings)
        new_settings["floating_x"] = self.floating_btn.winfo_x()
        new_settings["floating_y"] = self.floating_btn.winfo_y()
        new_settings["position_locked"] = self._position_locked
        try:
            save_settings(new_settings)
            self.settings = new_settings
        except Exception as exc:
            _log_exception("Saving floating position", exc)
            self.show_status("Could not save the floating icon position.", COLOR_ERROR)

    def toggle_position_lock(self):
        self._position_locked = not self._position_locked
        new_settings = dict(self.settings)
        new_settings["position_locked"] = self._position_locked
        if self.floating_btn:
            new_settings["floating_x"] = self.floating_btn.winfo_x()
            new_settings["floating_y"] = self.floating_btn.winfo_y()
        try:
            save_settings(new_settings)
            self.settings = new_settings
            state = "locked" if self._position_locked else "unlocked"
            LOGGER.info("Floating icon position %s", state)
            self.show_status(f"Floating icon {state}.", COLOR_SUCCESS, autohide_ms=2000)
        except Exception as exc:
            self._position_locked = not self._position_locked
            _log_exception("Saving floating position lock", exc)
            self.show_status("Could not save the position setting.", COLOR_ERROR)

    def open_log_folder(self):
        if LOG_DIR is None:
            self.show_status("Log folder is unavailable.", COLOR_ERROR, autohide_ms=4000)
            return
        try:
            os.startfile(str(LOG_DIR))
            LOGGER.info("Opened log folder")
        except Exception as exc:
            _log_exception("Opening log folder", exc)
            self.show_status("Could not open the log folder.", COLOR_ERROR, autohide_ms=4000)

    # =========================================================================
    # Floating Button + Status Bubble
    # =========================================================================
    def build_floating_button(self):
        btn = tk.Toplevel(self.root)
        btn.overrideredirect(True)
        btn.attributes("-topmost", True)
        x = self.settings.get("floating_x", 40)
        y = self.settings.get("floating_y", 40)
        screen_width = btn.winfo_screenwidth()
        screen_height = btn.winfo_screenheight()
        x = min(max(0, x), max(0, screen_width - 52))
        y = min(max(0, y), max(0, screen_height - 52))
        btn.geometry(f"52x52+{x}+{y}")
        btn.configure(bg=FLOATING_TRANSPARENT_COLOR)
        try:
            btn.attributes("-transparentcolor", FLOATING_TRANSPARENT_COLOR)
        except tk.TclError:
            pass
        try:
            btn.attributes("-alpha", 0.95)
        except Exception:
            pass

        canvas = tk.Canvas(btn, width=52, height=52, bg=FLOATING_TRANSPARENT_COLOR,
                            highlightthickness=0)
        canvas.pack(fill="both", expand=True)
        try:
            brand_icon = Image.open(resource_path(ICON_NAME)).convert("RGBA")
            self._floating_icon_image = ImageTk.PhotoImage(
                brand_icon.resize((52, 52), Image.Resampling.LANCZOS)
            )
            canvas.create_image(26, 26, image=self._floating_icon_image)
        except (OSError, tk.TclError) as exc:
            LOGGER.warning("Could not load floating brand icon (%s)", type(exc).__name__)
            canvas.create_oval(3, 3, 49, 49, fill="#162941", outline="#52E0C2", width=2)
            canvas.create_text(26, 26, text="P", fill="#F4F8FF",
                               font=("Segoe UI", 21, "bold"))

        self._drag_data = {"x": 0, "y": 0, "moved": False}

        def start_drag(event):
            self._drag_data["x"] = event.x
            self._drag_data["y"] = event.y
            self._drag_data["moved"] = False

        def do_drag(event):
            if self._position_locked:
                return
            self._drag_data["moved"] = True
            x = btn.winfo_x() + (event.x - self._drag_data["x"])
            y = btn.winfo_y() + (event.y - self._drag_data["y"])
            btn.geometry(f"+{x}+{y}")
            self._reposition_status_bubble()

        def on_release(event):
            if self._drag_data.get("moved"):
                self._persist_floating_position()
            else:
                self.show_option_menu(btn)

        canvas.bind("<ButtonPress-1>", start_drag)
        canvas.bind("<B1-Motion>", do_drag)
        canvas.bind("<ButtonRelease-1>", on_release)

        self.floating_btn = btn
        self.build_status_bubble()

    def build_status_bubble(self):
        """Build the compact notification panel beneath the floating button."""
        bubble = tk.Toplevel(self.root)
        bubble.overrideredirect(True)
        bubble.attributes("-topmost", True)
        bubble.withdraw()  # hidden until we have something to show

        frame = tk.Frame(
            bubble, bg=COLOR_BG_CARD, padx=12, pady=10,
            highlightthickness=1, highlightbackground=COLOR_ACCENT,
            width=360,
        )
        frame.pack(fill="both", expand=True)
        self.status_label = tk.Label(frame, text="", font=FONT_TEXT,
                                     bg=COLOR_BG_CARD, fg=COLOR_TEXT,
                                     justify="left", anchor="w", wraplength=330)
        self.status_label.pack(fill="both", expand=True, anchor="w")

        actions = tk.Frame(frame, bg=COLOR_BG_CARD)
        actions.pack(fill="x", pady=(8, 0))
        self.status_copy_button = tk.Button(
            actions, text="Copy details", command=self.copy_status_details,
            font=FONT_SMALL, bg=COLOR_BG_CARD, fg=COLOR_ACCENT,
            activebackground=COLOR_BG_CARD, activeforeground=COLOR_ACCENT_HOVER,
            relief="flat", bd=0, cursor="hand2",
        )
        self.status_copy_button.pack(side="left")
        tk.Button(
            actions, text="Dismiss", command=self.hide_status,
            font=FONT_SMALL, bg=COLOR_BG_CARD, fg=COLOR_SUBTEXT,
            activebackground=COLOR_BG_CARD, activeforeground=COLOR_TEXT,
            relief="flat", bd=0, cursor="hand2",
        ).pack(side="right")
        self.status_copy_button.pack_forget()

        self.status_bubble = bubble
        self._reposition_status_bubble()

    def _reposition_status_bubble(self):
        if not self.status_bubble or not self.floating_btn:
            return
        x = self.floating_btn.winfo_x()
        y = self.floating_btn.winfo_y() + 58
        self.status_bubble.update_idletasks()
        width = self.status_bubble.winfo_reqwidth()
        height = self.status_bubble.winfo_reqheight()
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        x = min(max(0, x), max(0, screen_width - width))
        if y + height > screen_height:
            y = max(0, self.floating_btn.winfo_y() - height - 8)
        self.status_bubble.geometry(f"{width}x{height}+{x}+{y}")

    def _redact_status_text(self, text):
        safe_text = str(text)
        for key in self._known_api_keys:
            safe_text = safe_text.replace(key, "[REDACTED API KEY]")
        return safe_text

    def _refresh_sensitive_values(self, settings):
        self._known_api_keys = {
            key for key in (
                settings.get("api_key", ""),
                *(settings.get(f"{provider}_api_key", "") for provider in DEFAULT_PROVIDER_ORDER),
            ) if key
        }

    def copy_status_details(self):
        try:
            self.status_bubble.clipboard_clear()
            self.status_bubble.clipboard_append(self._status_copy_text)
            self.status_copy_button.config(text="Copied")
            self.root.after(1400, lambda: self.status_copy_button.config(text="Copy details"))
        except tk.TclError as exc:
            _log_exception("Copying error details", exc)

    def show_status(self, text, color=COLOR_TEXT, autohide_ms=None):
        """Thread-safe: schedules the actual UI update on the main thread."""
        def _update():
            safe_text = self._redact_status_text(text)
            self._status_copy_text = safe_text
            self.status_label.config(text=safe_text, fg=color)
            if color == COLOR_ERROR:
                self.status_copy_button.pack(side="left")
                autohide = None
            else:
                self.status_copy_button.pack_forget()
                autohide = autohide_ms
            self.status_bubble.deiconify()
            self.status_bubble.lift()
            self._reposition_status_bubble()
            if self._status_after_id:
                self.root.after_cancel(self._status_after_id)
                self._status_after_id = None
            if autohide:
                self._status_after_id = self.root.after(
                    autohide, self.status_bubble.withdraw
                )
        self.root.after(0, _update)

    def hide_status(self):
        if self.status_bubble:
            self.root.after(0, self.status_bubble.withdraw)

    # =========================================================================
    # Option menu (right-click style popup from the floating button)
    # =========================================================================
    def show_option_menu(self, anchor_widget):
        menu = tk.Menu(self.root, tearoff=0, bg=COLOR_BG_CARD, fg=COLOR_TEXT,
                        activebackground=COLOR_ACCENT, activeforeground="white",
                        bd=0)
        default_action = self.settings.get("default_action", "rewrite_same")
        if default_action not in TEMPLATES:
            default_action = "rewrite_same"
        menu.add_command(
            label=f"Run default: {TEMPLATES[default_action]['label']}",
            command=self._run_default_action,
        )
        menu.add_separator()
        for key, tmpl in TEMPLATES.items():
            menu.add_command(
                label=tmpl["label"],
                command=lambda k=key: self.run_action(k),
            )
        menu.add_separator()
        menu.add_command(
            label="Unlock Position" if self._position_locked else "Lock Position",
            command=self.toggle_position_lock,
        )
        menu.add_command(label="Open App / Settings", command=lambda: self.open_page("home"))
        menu.add_command(label="Open Logs", command=self.open_log_folder)
        menu.add_command(label="Quit", command=self.quit_app)

        x = anchor_widget.winfo_rootx()
        y = anchor_widget.winfo_rooty() + 56
        try:
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    # =========================================================================
    # Core Action: grab selection -> call AI -> paste back
    # =========================================================================
    def run_action(self, template_key):
        label = TEMPLATES[template_key]["label"]

        def worker():
            LOGGER.info("Action started: %s", template_key)
            self.show_status(f"Working: {label}...", COLOR_TEXT)

            # ---- Step 1: get selected text ----
            try:
                text = get_selected_text(target_hwnd=self._last_external_hwnd)
            except ClipboardError as e:
                LOGGER.warning("Clipboard read failed (%s)", type(e).__name__)
                self.show_status(f"Clipboard error: {e}", COLOR_ERROR, autohide_ms=4000)
                return
            except Exception as e:
                _log_exception("Reading selected text", e)
                self.show_status(f"Unexpected error: {e}", COLOR_ERROR, autohide_ms=4000)
                return

            if not text or not text.strip():
                LOGGER.warning(
                    "Selection copy returned empty text (%s); verify target app focus and copy support",
                    template_key,
                )
                self.show_status(
                    "Couldn't copy selected text. Reselect it and try again.",
                    COLOR_WARN, autohide_ms=4000,
                )
                return

            # ---- Step 2: call the AI ----
            settings = load_settings()
            if not configured_providers(settings):
                LOGGER.warning("Action stopped: no API key configured")
                self.show_status(
                    "No API key set. Open Settings and add one.",
                    COLOR_ERROR, autohide_ms=5000,
                )
                return

            self.show_status(
                f"Contacting {provider_label(settings['provider'], settings)}...",
                COLOR_TEXT,
            )
            attempted_providers = []

            def report_provider(provider):
                attempted_providers.append(provider)
                self.show_status(
                    f"Trying {provider_label(provider, settings)}...", COLOR_TEXT
                )

            try:
                result = process_with_fallback(
                    text=text,
                    template_key=template_key,
                    settings=settings,
                    on_provider=report_provider,
                )
            except AIError as e:
                LOGGER.warning(
                    "AI request failed: provider=%s error_type=%s",
                    settings["provider"], type(e).__name__,
                )
                self.show_status(str(e), COLOR_ERROR, autohide_ms=6000)
                return
            except Exception as e:
                _log_exception(f"{settings['provider']} request", e)
                self.show_status(f"Unexpected error: {e}", COLOR_ERROR, autohide_ms=6000)
                return

            if not result or not result.strip():
                LOGGER.warning("AI returned an empty response: provider=%s", settings["provider"])
                self.show_status("AI returned an empty response. Try again.",
                                  COLOR_ERROR, autohide_ms=4000)
                return

            used_provider = attempted_providers[-1] if attempted_providers else settings["provider"]
            if len(attempted_providers) > 1:
                LOGGER.warning(
                    "Provider fallback used: primary=%s selected=%s",
                    settings["provider"], used_provider,
                )

            # ---- Step 3: paste result back ----
            try:
                if settings.get("word_typing_enabled", True):
                    self.show_status(
                        f"Writing with {provider_label(used_provider, settings)}... 0%",
                        COLOR_TEXT,
                    )
                    paste_text_streaming(
                        result,
                        target_hwnd=self._last_external_hwnd,
                        on_progress=lambda progress: self.show_status(
                            f"Writing... {int(progress * 100)}%", COLOR_TEXT
                        ),
                    )
                else:
                    self.show_status(
                        f"Pasting with {provider_label(used_provider, settings)}...",
                        COLOR_TEXT,
                    )
                    paste_text(result, target_hwnd=self._last_external_hwnd)
            except ClipboardError as e:
                LOGGER.warning("Clipboard paste failed (%s)", type(e).__name__)
                self.show_status(f"Paste failed: {e}", COLOR_ERROR, autohide_ms=4000)
                return
            except Exception as e:
                _log_exception("Pasting rewritten text", e)
                self.show_status(f"Unexpected paste error: {e}", COLOR_ERROR, autohide_ms=4000)
                return

            LOGGER.info("Action completed: %s", template_key)
            self.show_status("Done", COLOR_SUCCESS, autohide_ms=2500)

        threading.Thread(target=worker, daemon=True).start()

    # =========================================================================
    # App window with pages: Home / Settings / About - each has a Back button
    # =========================================================================
    def open_page(self, page_name):
        if self.app_win is None or not tk.Toplevel.winfo_exists(self.app_win):
            self._build_app_window()
        self.app_win.deiconify()
        self.app_win.lift()
        self._render_page(page_name)

    def _build_app_window(self):
        win = tk.Toplevel(self.root)
        win.title("Promptify")
        win.geometry("600x760")
        win.configure(bg=COLOR_BG)
        win.minsize(560, 660)
        win.resizable(True, True)
        win.protocol("WM_DELETE_WINDOW", win.withdraw)  # hide, don't quit whole app
        try:
            icon_path = resource_path(ICON_NAME)
            self._app_icon = tk.PhotoImage(file=str(icon_path))
            win.iconphoto(True, self._app_icon)
        except tk.TclError as exc:
            LOGGER.warning("Could not load app window icon (%s)", type(exc).__name__)

        container = tk.Frame(win, bg=COLOR_BG)
        container.pack(fill="both", expand=True)

        self.app_win = win
        self.page_container = container

    def _clear_page(self):
        if self._recording_listener and self._hotkey_recording_context:
            self._finish_hotkey_recording(
                *self._hotkey_recording_context, cancelled=True
            )
        self.root.unbind_all("<MouseWheel>")
        for widget in self.page_container.winfo_children():
            widget.destroy()

    def _page_header(self, title, show_back=True, back_target="home"):
        """Consistent header row with an optional Back button, used on
        every page except Home."""
        header = tk.Frame(self.page_container, bg=COLOR_BG)
        header.pack(fill="x", padx=16, pady=(16, 4))

        if show_back:
            back_btn = tk.Button(
                header, text="<-  Back", font=FONT_LABEL,
                bg=COLOR_BG, fg=COLOR_ACCENT, activebackground=COLOR_BG,
                activeforeground=COLOR_ACCENT_HOVER, relief="flat", bd=0,
                cursor="hand2", command=lambda: self._render_page(back_target),
            )
            back_btn.pack(side="left")
            back_btn.bind("<Enter>", lambda e: back_btn.config(fg=COLOR_ACCENT_HOVER))
            back_btn.bind("<Leave>", lambda e: back_btn.config(fg=COLOR_ACCENT))

        tk.Label(header, text=title, font=FONT_TITLE, bg=COLOR_BG,
                 fg=COLOR_TEXT).pack(side="left", padx=(12 if show_back else 0, 0))

    def _render_page(self, page_name):
        self.current_page = page_name
        if page_name == "settings":
            self.app_win.geometry("600x760")
            self.app_win.minsize(560, 660)
        else:
            self.app_win.geometry("560x600")
            self.app_win.minsize(520, 560)
        self._clear_page()
        if page_name == "home":
            self._render_home_page()
        elif page_name == "settings":
            self._render_settings_page()
        elif page_name == "about":
            self._render_about_page()

    # ---------------- Home page ----------------
    def _render_home_page(self):
        self._page_header("Promptify", show_back=False)

        tk.Label(
            self.page_container,
            text="Select text and click the floating icon to choose an action.\n"
                 "Or use the global shortcut for your selected default action.",
            font=FONT_TEXT, bg=COLOR_BG, fg=COLOR_SUBTEXT, justify="left",
        ).pack(anchor="w", padx=16, pady=(0, 16))

        card = tk.Frame(self.page_container, bg=COLOR_BG_CARD)
        card.pack(fill="x", padx=16, pady=6)
        for key, tmpl in TEMPLATES.items():
            row = tk.Label(card, text="-  " + tmpl["label"], font=FONT_TEXT,
                            bg=COLOR_BG_CARD, fg=COLOR_TEXT, anchor="w")
            row.pack(fill="x", padx=12, pady=6)

        settings = load_settings()
        ready_providers = configured_providers(settings)
        status_ok = bool(ready_providers)
        status_text = (
            f"Ready - primary: {provider_label(settings['provider'], settings)}; "
            f"{len(ready_providers)} provider key(s) configured"
            if status_ok else
            "No provider key set yet"
        )
        status_color = COLOR_SUCCESS if status_ok else COLOR_WARN
        tk.Label(self.page_container, text=status_text, font=FONT_TEXT,
                 bg=COLOR_BG, fg=status_color).pack(anchor="w", padx=16, pady=(16, 4))

        btn_row = tk.Frame(self.page_container, bg=COLOR_BG)
        btn_row.pack(anchor="w", padx=16, pady=16)
        RoundButton(btn_row, "Settings", lambda: self._render_page("settings")
                    ).pack(side="left", padx=(0, 8))
        RoundButton(btn_row, "About", lambda: self._render_page("about"),
                    bg=COLOR_BG_CARD, hover="#3a3a52", fg=COLOR_TEXT
                    ).pack(side="left")

    # ---------------- Settings page ----------------
    def _render_settings_page(self):
        self._page_header("Settings", show_back=True, back_target="home")

        settings = load_settings()
        footer = tk.Frame(self.page_container, bg=COLOR_BG)
        footer.pack(side="bottom", fill="x", padx=16, pady=(4, 12))
        content = tk.Frame(self.page_container, bg=COLOR_BG)
        content.pack(fill="both", expand=True, padx=16, pady=(4, 0))
        canvas = tk.Canvas(content, bg=COLOR_BG, highlightthickness=0)
        scrollbar = ttk.Scrollbar(content, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        body = tk.Frame(canvas, bg=COLOR_BG)
        body_window = canvas.create_window((0, 0), window=body, anchor="nw")

        def update_scroll_region(_event=None):
            canvas.configure(scrollregion=canvas.bbox("all"))

        def fit_content_width(event):
            canvas.itemconfigure(body_window, width=event.width)

        body.bind("<Configure>", update_scroll_region)
        canvas.bind("<Configure>", fit_content_width)
        canvas.bind("<Enter>", lambda _event: canvas.bind_all(
            "<MouseWheel>",
            lambda event: canvas.yview_scroll(
                -int(event.delta / 120) if event.delta else 0, "units"
            ),
        ))
        canvas.bind("<Leave>", lambda _event: canvas.unbind_all("<MouseWheel>"))

        prompt_mode_panel = tk.Frame(body, bg=COLOR_BG)
        prompt_mode_panel.pack(fill="x", anchor="w", pady=(8, 0))
        tk.Label(prompt_mode_panel, text="Prompt mode", font=FONT_LABEL,
                 bg=COLOR_BG, fg=COLOR_TEXT).pack(anchor="w", pady=(0, 4))
        prompt_mode_var = tk.StringVar(
            value=settings.get("prompt_mode", "default")
        )
        mode_row = tk.Frame(prompt_mode_panel, bg=COLOR_BG)
        mode_row.pack(anchor="w")
        for mode, label in (
            ("default", "Default"),
            ("developer", "Developer"),
            ("custom", "Custom"),
        ):
            tk.Radiobutton(
                mode_row, text=label, value=mode, variable=prompt_mode_var,
                indicatoron=False, padx=12, pady=5, bd=0,
                bg=COLOR_BG_CARD, fg=COLOR_TEXT,
                selectcolor=COLOR_ACCENT, activebackground=COLOR_ACCENT,
                activeforeground="white", font=FONT_SMALL, cursor="hand2",
            ).pack(side="left", padx=(0, 6))
        tk.Label(
            prompt_mode_panel,
            text="Default and Developer use built-in prompts. Custom sends your template followed by the selected text.",
            font=FONT_SMALL, bg=COLOR_BG, fg=COLOR_SUBTEXT,
            wraplength=500, justify="left",
        ).pack(anchor="w", pady=(3, 0))

        custom_template_frame = tk.Frame(prompt_mode_panel, bg=COLOR_BG)
        tk.Label(
            custom_template_frame, text="Your template",
            font=FONT_SMALL, bg=COLOR_BG, fg=COLOR_SUBTEXT,
        ).pack(anchor="w", pady=(7, 3))
        custom_template_input_frame = tk.Frame(
            custom_template_frame, bg=COLOR_BG
        )
        custom_template_input_frame.pack(fill="x")
        custom_template_text = tk.Text(
            custom_template_input_frame, height=6, wrap="word",
            bg=COLOR_BG_CARD, fg=COLOR_TEXT, insertbackground=COLOR_TEXT,
            relief="flat", font=FONT_TEXT, padx=7, pady=6,
        )
        custom_template_scrollbar = ttk.Scrollbar(
            custom_template_input_frame, orient="vertical",
            command=custom_template_text.yview,
        )
        custom_template_text.configure(
            yscrollcommand=custom_template_scrollbar.set
        )
        custom_template_text.pack(side="left", fill="x", expand=True)
        custom_template_scrollbar.pack(side="right", fill="y")
        custom_template_text.insert(
            "1.0", settings.get("custom_prompt_template", "")
        )

        def update_prompt_mode(*_):
            if prompt_mode_var.get() == "custom":
                custom_template_frame.pack(fill="x", pady=(0, 4))
            else:
                custom_template_frame.pack_forget()

        prompt_mode_var.trace_add("write", update_prompt_mode)
        update_prompt_mode()

        tk.Label(body, text="Provider", font=FONT_LABEL, bg=COLOR_BG,
                 fg=COLOR_TEXT).pack(anchor="w", pady=(8, 4))
        provider_var = tk.StringVar(value=settings["provider"])
        provider_choices = list(PROVIDER_DEFAULTS)
        provider_names = {
            provider: (
                "OpenRouter / Custom" if provider == "custom"
                else PROVIDER_LABELS[provider]
            )
            for provider in provider_choices
        }
        provider_combo = ttk.Combobox(
            body, textvariable=provider_var,
            values=[provider_names[provider] for provider in provider_choices],
            state="readonly", width=34,
        )
        provider_combo.pack(anchor="w")

        def selected_provider_key():
            selected_label = provider_var.get()
            return next(
                provider for provider, label in provider_names.items()
                if label == selected_label
            )

        provider_var.set(provider_names.get(settings["provider"], provider_names["gemini"]))

        tk.Label(body, text="Model name", font=FONT_LABEL, bg=COLOR_BG,
                 fg=COLOR_TEXT).pack(anchor="w", pady=(14, 4))
        model_values = {
            provider: (
                settings.get("custom_model", "")
                if provider == "custom"
                else settings.get(f"{provider}_model") or (
                    settings.get("model") if provider == settings["provider"]
                    else PROVIDER_DEFAULTS[provider]
                )
            )
            for provider in provider_choices
        }
        model_var = tk.StringVar(value=model_values[settings["provider"]])
        selected_model_provider = {"value": settings["provider"]}

        def update_default_model(*_):
            previous_provider = selected_model_provider["value"]
            next_provider = selected_provider_key()
            model_values[previous_provider] = model_var.get().strip()
            model_var.set(model_values[next_provider])
            selected_model_provider["value"] = next_provider

        provider_var.trace_add("write", update_default_model)
        model_entry = tk.Entry(body, textvariable=model_var, width=52,
                                bg=COLOR_BG_CARD, fg=COLOR_TEXT,
                                insertbackground=COLOR_TEXT, relief="flat")
        model_entry.pack(anchor="w", ipady=5)
        tk.Label(body, text="Use the exact model ID from your provider.",
                 font=FONT_SMALL, bg=COLOR_BG, fg=COLOR_SUBTEXT
                 ).pack(anchor="w", pady=(2, 0))

        custom_frame = tk.LabelFrame(
            body, text="OpenAI-compatible custom provider",
            bg=COLOR_BG, fg=COLOR_TEXT, padx=10, pady=8,
        )
        custom_frame.pack(fill="x", pady=(12, 2))
        tk.Label(
            custom_frame,
            text="For OpenRouter or another compatible API. The key is sent only to this endpoint.",
            font=FONT_SMALL, bg=COLOR_BG, fg=COLOR_SUBTEXT,
            wraplength=460, justify="left",
        ).pack(anchor="w", pady=(0, 6))
        custom_name_var = tk.StringVar(
            value=settings.get("custom_provider_name", "OpenRouter")
        )
        tk.Label(custom_frame, text="Provider name", font=FONT_SMALL,
                 bg=COLOR_BG, fg=COLOR_SUBTEXT).pack(anchor="w")
        tk.Entry(
            custom_frame, textvariable=custom_name_var, width=52,
            bg=COLOR_BG_CARD, fg=COLOR_TEXT, insertbackground=COLOR_TEXT,
            relief="flat",
        ).pack(anchor="w", fill="x", ipady=4, pady=(2, 6))
        custom_url_var = tk.StringVar(
            value=settings.get("custom_base_url", "https://openrouter.ai/api/v1")
        )
        tk.Label(custom_frame, text="API base URL", font=FONT_SMALL,
                 bg=COLOR_BG, fg=COLOR_SUBTEXT).pack(anchor="w")
        tk.Entry(
            custom_frame, textvariable=custom_url_var, width=52,
            bg=COLOR_BG_CARD, fg=COLOR_TEXT, insertbackground=COLOR_TEXT,
            relief="flat",
        ).pack(anchor="w", fill="x", ipady=4, pady=(2, 2))
        tk.Label(
            custom_frame, text="Example: https://openrouter.ai/api/v1",
            font=FONT_SMALL, bg=COLOR_BG, fg=COLOR_SUBTEXT,
        ).pack(anchor="w")

        fallback_var = tk.BooleanVar(value=settings.get("allow_provider_fallback", True))
        tk.Checkbutton(
            body, text="Try other configured providers if the primary fails",
            variable=fallback_var, bg=COLOR_BG, fg=COLOR_TEXT,
            selectcolor=COLOR_BG_CARD, activebackground=COLOR_BG,
            font=FONT_SMALL,
        ).pack(anchor="w", pady=(6, 0))
        tk.Label(
            body,
            text="With fallback on, selected text may go to another provider. Each API key is sent only to its own provider.",
            font=FONT_SMALL, bg=COLOR_BG, fg=COLOR_SUBTEXT, wraplength=500,
            justify="left",
        ).pack(anchor="w", pady=(1, 0))

        tk.Label(body, text="Generation", font=FONT_LABEL, bg=COLOR_BG,
                 fg=COLOR_TEXT).pack(anchor="w", pady=(12, 2))
        typing_var = tk.BooleanVar(value=settings.get("word_typing_enabled", True))
        tk.Checkbutton(
            body, text="Type the result word by word (animation)",
            variable=typing_var, bg=COLOR_BG, fg=COLOR_TEXT,
            selectcolor=COLOR_BG_CARD, activebackground=COLOR_BG,
            font=FONT_SMALL,
        ).pack(anchor="w")

        tk.Label(body, text="Quick actions", font=FONT_LABEL, bg=COLOR_BG,
                 fg=COLOR_TEXT).pack(anchor="w", pady=(12, 3))
        action_keys = list(TEMPLATES)
        action_labels = [TEMPLATES[key]["label"] for key in action_keys]
        default_action = settings.get("default_action", "rewrite_same")
        if default_action not in TEMPLATES:
            default_action = "rewrite_same"
        default_action_var = tk.StringVar(value=TEMPLATES[default_action]["label"])
        action_row = tk.Frame(body, bg=COLOR_BG)
        action_row.pack(fill="x", pady=(0, 4))
        tk.Label(action_row, text="Default action", font=FONT_SMALL,
                 bg=COLOR_BG, fg=COLOR_SUBTEXT).pack(side="left", padx=(0, 8))
        ttk.Combobox(
            action_row, textvariable=default_action_var, values=action_labels,
            state="readonly", width=31,
        ).pack(side="left")

        shortcut_enabled_var = tk.BooleanVar(
            value=settings.get("hotkey_enabled", True)
        )
        tk.Checkbutton(
            body, text="Enable global shortcut",
            variable=shortcut_enabled_var, bg=COLOR_BG, fg=COLOR_TEXT,
            selectcolor=COLOR_BG_CARD, activebackground=COLOR_BG,
            font=FONT_SMALL,
        ).pack(anchor="w", pady=(2, 3))
        shortcut_row = tk.Frame(body, bg=COLOR_BG)
        shortcut_row.pack(fill="x")
        tk.Label(shortcut_row, text="Shortcut", font=FONT_SMALL,
                 bg=COLOR_BG, fg=COLOR_SUBTEXT).pack(side="left", padx=(0, 8))
        saved_hotkey = settings.get("hotkey", DEFAULT_HOTKEY)
        try:
            saved_hotkey = format_hotkey(normalize_hotkey(saved_hotkey))
        except ValueError:
            saved_hotkey = format_hotkey(DEFAULT_HOTKEY)
        hotkey_var = tk.StringVar(value=saved_hotkey)
        hotkey_entry = tk.Entry(
            shortcut_row, textvariable=hotkey_var, width=19,
            bg=COLOR_BG_CARD, fg=COLOR_TEXT, insertbackground=COLOR_TEXT,
            relief="flat",
        )
        hotkey_entry.pack(side="left", ipady=4, padx=(0, 6))
        record_button = tk.Button(
            shortcut_row, text="Record", command=lambda: self.start_hotkey_recording(
                hotkey_var, shortcut_status_label, record_button
            ),
            font=FONT_SMALL, bg=COLOR_BG_CARD, fg=COLOR_TEXT,
            activebackground=COLOR_ACCENT, activeforeground="white",
            relief="flat", bd=0, cursor="hand2", padx=10, pady=5,
        )
        record_button.pack(side="left", padx=(0, 5))

        def use_default_hotkey():
            hotkey_var.set(format_hotkey(DEFAULT_HOTKEY))
            shortcut_status_label.config(
                text="Default shortcut restored. Save to apply.", fg=COLOR_SUBTEXT
            )

        tk.Button(
            shortcut_row, text="Default", command=use_default_hotkey,
            font=FONT_SMALL, bg=COLOR_BG_CARD, fg=COLOR_TEXT,
            activebackground=COLOR_ACCENT, activeforeground="white",
            relief="flat", bd=0, cursor="hand2", padx=10, pady=5,
        ).pack(side="left")
        shortcut_status_label = tk.Label(
            body,
            text="The shortcut runs the selected default action. Example: Ctrl+Alt+F.",
            font=FONT_SMALL, bg=COLOR_BG, fg=COLOR_SUBTEXT,
            wraplength=510, justify="left",
        )
        shortcut_status_label.pack(anchor="w", pady=(2, 0))

        position_locked_var = tk.BooleanVar(value=self._position_locked)
        tk.Checkbutton(
            body, text="Lock floating icon position",
            variable=position_locked_var,
            command=lambda: self.toggle_position_lock(),
            bg=COLOR_BG, fg=COLOR_TEXT, selectcolor=COLOR_BG_CARD,
            activebackground=COLOR_BG, font=FONT_SMALL,
        ).pack(anchor="w", pady=(4, 0))

        tk.Label(body, text="Provider API keys", font=FONT_LABEL, bg=COLOR_BG,
                 fg=COLOR_TEXT).pack(anchor="w", pady=(14, 4))
        key_vars = {}
        for provider in provider_choices:
            key_row = tk.Frame(body, bg=COLOR_BG)
            key_row.pack(anchor="w", fill="x", pady=1)
            label_text = (
                "Custom API key" if provider == "custom"
                else f"{PROVIDER_LABELS[provider]} API key"
            )
            tk.Label(key_row, text=label_text,
                     width=19, anchor="w", font=FONT_SMALL,
                     bg=COLOR_BG, fg=COLOR_SUBTEXT).pack(side="left")
            key_var = tk.StringVar(value=settings.get(
                f"{provider}_api_key",
                settings.get("api_key", "") if provider == settings.get("provider") else "",
            ))
            key_vars[provider] = key_var
            tk.Entry(key_row, textvariable=key_var, width=28,
                     bg=COLOR_BG_CARD, fg=COLOR_TEXT,
                     insertbackground=COLOR_TEXT, relief="flat", show="*").pack(
                         side="left", ipady=4
                     )

        status_label = tk.Label(
            footer, text=settings.get("credential_store_error", ""),
            font=FONT_TEXT, bg=COLOR_BG,
            fg=COLOR_ERROR if settings.get("credential_store_error") else COLOR_TEXT,
            wraplength=390, justify="left", anchor="w",
        )
        status_label.pack(side="left", fill="x", expand=True, padx=(0, 12))

        def save():
            new_settings = dict(settings)
            selected_provider = selected_provider_key()
            model_values[selected_model_provider["value"]] = model_var.get().strip()
            new_settings["provider"] = selected_provider
            new_settings["model"] = model_var.get().strip()
            if selected_provider == "custom":
                new_settings["custom_model"] = model_var.get().strip()
            else:
                new_settings[f"{selected_provider}_model"] = model_var.get().strip()
            current_order = settings.get("provider_order", DEFAULT_PROVIDER_ORDER)
            new_settings["provider_order"] = [selected_provider] + [
                provider for provider in current_order
                if provider != selected_provider
            ]
            new_settings["allow_provider_fallback"] = fallback_var.get()
            new_settings["word_typing_enabled"] = typing_var.get()
            new_settings["prompt_mode"] = prompt_mode_var.get()
            new_settings["custom_prompt_template"] = custom_template_text.get(
                "1.0", "end-1c"
            )
            if (
                new_settings["prompt_mode"] == "custom"
                and not new_settings["custom_prompt_template"].strip()
            ):
                status_label.config(
                    text="Enter your custom prompt template or choose another mode.",
                    fg=COLOR_ERROR,
                )
                return
            new_settings["default_action"] = action_keys[
                action_labels.index(default_action_var.get())
            ]
            new_settings["position_locked"] = self._position_locked
            new_settings["custom_provider_name"] = (
                custom_name_var.get().strip()[:40] or "Custom provider"
            )
            new_settings["custom_base_url"] = custom_url_var.get().strip()
            if self.floating_btn:
                new_settings["floating_x"] = self.floating_btn.winfo_x()
                new_settings["floating_y"] = self.floating_btn.winfo_y()
            old_hotkey = self.settings.get("hotkey", DEFAULT_HOTKEY)
            old_hotkey_enabled = self.settings.get("hotkey_enabled", True)
            try:
                normalized_hotkey = normalize_hotkey(hotkey_var.get())
            except ValueError as e:
                status_label.config(text=str(e), fg=COLOR_ERROR)
                return
            new_settings["hotkey"] = normalized_hotkey
            new_settings["hotkey_enabled"] = shortcut_enabled_var.get()
            for provider, key_var in key_vars.items():
                try:
                    new_settings[f"{provider}_api_key"] = normalize_api_key(
                        key_var.get(), provider_label(provider, new_settings)
                    )
                except AIError as e:
                    status_label.config(text=str(e), fg=COLOR_ERROR)
                    return

            new_settings["api_key"] = new_settings[
                f"{selected_provider}_api_key"
            ]
            custom_key = new_settings["custom_api_key"]
            if (selected_provider == "custom" and custom_key) and not new_settings["custom_model"]:
                status_label.config(
                    text="Enter the exact custom model ID, or choose another provider.",
                    fg=COLOR_ERROR,
                )
                return
            if selected_provider == "custom" or custom_key:
                try:
                    compatible_chat_completions_url(new_settings["custom_base_url"])
                except AIError as e:
                    status_label.config(text=str(e), fg=COLOR_ERROR)
                    return
            if selected_provider != "custom" and not new_settings["model"]:
                status_label.config(text="Model name can't be empty.", fg=COLOR_ERROR)
                return
            try:
                self._configure_global_hotkey(
                    normalized_hotkey, new_settings["hotkey_enabled"]
                )
                save_settings(new_settings)
            except Exception as e:
                try:
                    self._configure_global_hotkey(old_hotkey, old_hotkey_enabled)
                except Exception as restore_exc:
                    _log_exception("Restoring previous shortcut after save failure", restore_exc)
                _log_exception("Saving settings", e)
                status_label.config(
                    text=f"Could not apply settings: {e}", fg=COLOR_ERROR
                )
                return
            self.settings = new_settings
            hotkey_var.set(format_hotkey(normalized_hotkey))
            LOGGER.info(
                "Settings saved: provider=%s model=%s api_key_configured=%s",
                new_settings["provider"], new_settings["model"],
                bool(new_settings["api_key"]),
            )
            self._refresh_sensitive_values(new_settings)
            status_label.config(text="Saved.", fg=COLOR_SUCCESS)
            shortcut_status_label.config(
                text=(
                    f"Shortcut {format_hotkey(normalized_hotkey)} runs "
                    f"{TEMPLATES[new_settings['default_action']]['label']}."
                    if new_settings["hotkey_enabled"]
                    else "Global shortcut is disabled."
                ),
                fg=COLOR_SUCCESS,
            )

        RoundButton(footer, "Save", save, width=112).pack(side="right")

    # ---------------- About page ----------------
    def _render_about_page(self):
        self._page_header("About", show_back=True, back_target="home")

        body = tk.Frame(self.page_container, bg=COLOR_BG)
        body.pack(fill="both", expand=True, padx=16, pady=8)

        text = (
            "Promptify\n\n"
            "Developed by Mr Farman, a professional software developer focused "
            "on practical AI tools, automation, and clean user experiences.\n\n"
            "Promptify turns selected text into polished writing, corrections, "
            "or translations and pastes the result back into the original app.\n\n"
            "- Rewrite (Same Language): polish text in whatever language it's in.\n"
            "- Roman Urdu Rewrite: heavy spelling/grammar correction, stays in Roman Urdu.\n"
            "- Translate + Enhance -> English: convert any language to polished English.\n\n"
            "Each provider receives only its own API key. The selected text is sent\n"
            "to your primary provider and, when fallback is enabled, may also be\n"
            "sent to another provider whose key you added if the primary fails.\n\n"
            "Saved API keys are protected with Windows DPAPI for your Windows account.\n\n"
            "Close this app anytime from the tray icon (Quit), or press\n"
            "Ctrl+C in the terminal it was launched from."
        )
        tk.Label(body, text=text, font=FONT_TEXT, bg=COLOR_BG, fg=COLOR_TEXT,
                 justify="left", wraplength=400).pack(anchor="w")
        tk.Button(
            body, text="Learn More About Developer",
            command=self.open_developer_portfolio,
            font=FONT_LABEL, bg=COLOR_ACCENT, fg="white",
            activebackground=COLOR_ACCENT_HOVER, activeforeground="white",
            relief="flat", bd=0, cursor="hand2", padx=12, pady=8,
        ).pack(anchor="w", pady=(18, 0))

    def open_developer_portfolio(self):
        portfolio_url = "https://fktech.site"
        chrome_candidates = [
            Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
        ]
        chrome_path = next((candidate for candidate in chrome_candidates if candidate.is_file()), None)
        try:
            if chrome_path:
                subprocess.Popen([str(chrome_path), portfolio_url], close_fds=True)
            else:
                os.startfile(portfolio_url)
                self.show_status(
                    "Chrome was not found; opened the portfolio in your default browser.",
                    COLOR_WARN, autohide_ms=5000,
                )
        except Exception as exc:
            _log_exception("Opening developer portfolio", exc)
            self.show_status("Could not open fktech.site.", COLOR_ERROR)

    # =========================================================================
    # Tray Icon
    # =========================================================================
    def build_tray_icon(self):
        def on_open(icon, item):
            self.root.after(0, lambda: self.open_page("home"))

        def on_quit(icon, item):
            self.quit_app()

        menu = pystray.Menu(
            pystray.MenuItem("Open App", on_open, default=True),
            pystray.MenuItem("Open Logs", lambda icon, item: self.open_log_folder()),
            pystray.MenuItem("Quit", on_quit),
        )
        self.tray_icon = pystray.Icon("Promptify", make_tray_image(),
                           "Promptify", menu)

        def run_tray():
            try:
                self.tray_icon.run()
            except Exception as exc:
                _log_exception("System tray", exc)

        threading.Thread(target=run_tray, daemon=True, name="SystemTray").start()

    def run(self):
        try:
            self.root.mainloop()
        except KeyboardInterrupt:
            self.quit_app()


if __name__ == "__main__":
    try:
        is_primary_instance = acquire_single_instance()
    except Exception as exc:
        ctypes.windll.user32.MessageBoxW(
            None,
            f"Promptify could not check whether it is already running.\n\n{exc}",
            "Promptify startup error",
            0x10,
        )
        raise
    if not is_primary_instance:
        ctypes.windll.user32.MessageBoxW(
            None,
            "Promptify is already running. Use the existing floating icon or system tray.",
            "Promptify is already open",
            0x40,
        )
        raise SystemExit(0)

    configure_logging()
    try:
        LOGGER.info("Application starting")
        app = App()
        app.run()
        LOGGER.info("Application stopped")
    finally:
        release_single_instance()
