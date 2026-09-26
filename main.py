"""
main.py
Entry point. Creates:
  - A small always-on-top floating button (draggable) on screen
  - A system tray icon (right-click -> Open App / Quit)
  - A single app window with Home / Settings / About pages, each with a
    Back button, instead of separate popups
  - An on-screen status bubble near the floating button showing
    Processing / Done / Error, so the user always sees what's happening
  - Clean shutdown on Ctrl+C in the terminal (PowerShell) as well as from
    the tray menu / window close button

Run with:  python main.py
Stop with: Ctrl+C in the terminal, or Quit from the tray icon
Package with:  pyinstaller --onefile --noconsole --name RomanAIFixer main.py
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
from PIL import Image, ImageDraw, ImageTk

from config import load_settings, save_settings
from ai_provider import (
    DEFAULT_PROVIDER_ORDER,
    TEMPLATES,
    PROVIDER_DEFAULTS,
    PROVIDER_LABELS,
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
        btn.geometry("52x52+40+40")
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
            self._drag_data["moved"] = True
            x = btn.winfo_x() + (event.x - self._drag_data["x"])
            y = btn.winfo_y() + (event.y - self._drag_data["y"])
            btn.geometry(f"+{x}+{y}")
            self._reposition_status_bubble()

        def on_click(event):
            if not self._drag_data.get("moved"):
                self.show_option_menu(btn)

        canvas.bind("<ButtonPress-1>", start_drag)
        canvas.bind("<B1-Motion>", do_drag)
        canvas.bind("<ButtonRelease-1>", on_click)

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
        for key, tmpl in TEMPLATES.items():
            menu.add_command(
                label=tmpl["label"],
                command=lambda k=key: self.run_action(k),
            )
        menu.add_separator()
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

            self.show_status(f"Contacting {PROVIDER_LABELS.get(settings['provider'], settings['provider'])}...", COLOR_TEXT)
            attempted_providers = []

            def report_provider(provider):
                attempted_providers.append(provider)
                self.show_status(
                    f"Trying {PROVIDER_LABELS.get(provider, provider)}...", COLOR_TEXT
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
                        f"Writing with {PROVIDER_LABELS.get(used_provider, used_provider)}... 0%",
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
                        f"Pasting with {PROVIDER_LABELS.get(used_provider, used_provider)}...",
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
        win.geometry("560x600")
        win.configure(bg=COLOR_BG)
        win.minsize(520, 560)
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
            text="Select text anywhere on your PC, click the floating\n"
                 "AI button, and pick an action.",
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
            f"Ready - primary: {PROVIDER_LABELS[settings['provider']]}; "
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
        body = tk.Frame(self.page_container, bg=COLOR_BG)
        body.pack(fill="both", expand=True, padx=16, pady=8)

        tk.Label(body, text="Provider", font=FONT_LABEL, bg=COLOR_BG,
                 fg=COLOR_TEXT).pack(anchor="w", pady=(8, 4))
        provider_var = tk.StringVar(value=settings["provider"])
        prow = tk.Frame(body, bg=COLOR_BG)
        prow.pack(anchor="w")
        provider_choices = ["gemini", "openai", "groq", "deepseek", "xai"]
        for provider in provider_choices:
            tk.Radiobutton(
                prow, text=PROVIDER_LABELS[provider], variable=provider_var,
                value=provider, bg=COLOR_BG, fg=COLOR_TEXT,
                selectcolor=COLOR_BG_CARD, activebackground=COLOR_BG,
                font=FONT_SMALL,
            ).pack(side="left", padx=(0 if provider == "gemini" else 6, 0))

        tk.Label(body, text="Model name", font=FONT_LABEL, bg=COLOR_BG,
                 fg=COLOR_TEXT).pack(anchor="w", pady=(14, 4))
        model_values = {
            provider: settings.get(f"{provider}_model") or (
                settings.get("model") if provider == settings["provider"]
                else PROVIDER_DEFAULTS[provider]
            )
            for provider in provider_choices
        }
        model_var = tk.StringVar(value=model_values[settings["provider"]])
        selected_model_provider = {"value": settings["provider"]}

        def update_default_model(*_):
            previous_provider = selected_model_provider["value"]
            next_provider = provider_var.get()
            model_values[previous_provider] = model_var.get().strip()
            model_var.set(model_values[next_provider])
            selected_model_provider["value"] = next_provider

        provider_var.trace_add("write", update_default_model)
        model_entry = tk.Entry(body, textvariable=model_var, width=40,
                                bg=COLOR_BG_CARD, fg=COLOR_TEXT,
                                insertbackground=COLOR_TEXT, relief="flat")
        model_entry.pack(anchor="w", ipady=5)
        tk.Label(body, text="Primary model; fallback models use sensible defaults.",
                 font=FONT_SMALL, bg=COLOR_BG, fg=COLOR_SUBTEXT
                 ).pack(anchor="w", pady=(2, 0))

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

        tk.Label(body, text="Provider API keys", font=FONT_LABEL, bg=COLOR_BG,
                 fg=COLOR_TEXT).pack(anchor="w", pady=(14, 4))
        key_vars = {}
        for provider in provider_choices:
            key_row = tk.Frame(body, bg=COLOR_BG)
            key_row.pack(anchor="w", fill="x", pady=1)
            tk.Label(key_row, text=f"{PROVIDER_LABELS[provider]:<12}",
                     width=12, anchor="w", font=FONT_SMALL,
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
            body, text=settings.get("credential_store_error", ""),
            font=FONT_TEXT, bg=COLOR_BG,
            fg=COLOR_ERROR if settings.get("credential_store_error") else COLOR_TEXT,
            wraplength=500, justify="left",
        )
        status_label.pack(anchor="w", pady=(10, 0))

        def save():
            new_settings = dict(settings)
            new_settings["provider"] = provider_var.get()
            new_settings["model"] = model_var.get().strip()
            new_settings[f"{provider_var.get()}_model"] = model_var.get().strip()
            new_settings["api_key"] = key_vars[provider_var.get()].get().strip()
            current_order = settings.get("provider_order", DEFAULT_PROVIDER_ORDER)
            new_settings["provider_order"] = [provider_var.get()] + [
                provider for provider in current_order
                if provider != provider_var.get()
            ]
            new_settings["allow_provider_fallback"] = fallback_var.get()
            new_settings["word_typing_enabled"] = typing_var.get()
            for provider, key_var in key_vars.items():
                new_settings[f"{provider}_api_key"] = key_var.get().strip()

            if not new_settings["model"]:
                status_label.config(text="Model name can't be empty.", fg=COLOR_ERROR)
                return
            try:
                save_settings(new_settings)
            except Exception as e:
                _log_exception("Saving settings", e)
                status_label.config(text=f"Could not save: {e}", fg=COLOR_ERROR)
                return
            LOGGER.info(
                "Settings saved: provider=%s model=%s api_key_configured=%s",
                new_settings["provider"], new_settings["model"],
                bool(new_settings["api_key"]),
            )
            self._refresh_sensitive_values(new_settings)
            status_label.config(text="Saved.", fg=COLOR_SUCCESS)

        RoundButton(body, "Save", save).pack(anchor="w", pady=(16, 0))

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
    configure_logging()
    LOGGER.info("Application starting")
    app = App()
    app.run()
    LOGGER.info("Application stopped")
