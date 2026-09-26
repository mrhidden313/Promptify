"""
clipboard_helper.py
Ctrl+C simulate kar ke kisi bhi app mein currently selected text ko grab karta hai, aur Ctrl+V simulate kar ke text wapas paste kar sakta hai.

Yeh wahi trick hai jo Grammarly aur doosre system-wide tools use karte hain - yeh is liye kaam karta hai kyun ke Ctrl+C / Ctrl+V ko OS aur focused app handle karte hain, hum nahi.

Har failure par silently kuch na karne ke bajaye human-readable message ke saath ClipboardError raise karta hai, taake UI isay screen par dikha sakay.
"""

import ctypes
import re
import time
import pyperclip
from pynput.keyboard import Controller, Key

_keyboard = Controller()


class ClipboardError(Exception):
    """Raised when reading/writing the clipboard or simulating keys fails."""
    pass


def _simulate(key_char):
    """Press Ctrl+<key_char>, with clear errors if key simulation fails
    (e.g. some locked-down/admin windows block synthetic input)."""
    try:
        _keyboard.press(Key.ctrl)
        _keyboard.press(key_char)
        _keyboard.release(key_char)
        _keyboard.release(Key.ctrl)
    except Exception as e:
        raise ClipboardError(
            f"Could not simulate Ctrl+{key_char.upper()} "
            f"(the focused window may be blocking simulated input): {e}"
        )


def get_selected_text(timeout=1.0, target_hwnd=None) -> str:
    """
    Copies the current selection to clipboard and returns it.
    Temporarily saves/restores the previous clipboard content so we don't
    permanently overwrite whatever the user had copied before.
    Restores the previously focused external window before simulating Ctrl+C.
    Raises ClipboardError if the clipboard can't be read/written at all.
    """
    try:
        previous_clipboard = pyperclip.paste()
    except Exception:
        previous_clipboard = ""

    if target_hwnd:
        try:
            user32 = ctypes.windll.user32
            user32.IsWindow.argtypes = [ctypes.c_void_p]
            user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
            hwnd = ctypes.c_void_p(target_hwnd)
            if user32.IsWindow(hwnd):
                user32.SetForegroundWindow(hwnd)
                time.sleep(0.12)
        except Exception:
            pass

    # Clear clipboard first so we can tell if the copy actually happened
    try:
        pyperclip.copy("")
    except Exception as e:
        raise ClipboardError(f"Can't access the system clipboard: {e}")

    _simulate('c')

    # Give the OS/app a moment to update the clipboard
    time.sleep(0.15)

    try:
        selected = pyperclip.paste()
    except Exception as e:
        raise ClipboardError(f"Can't read the system clipboard: {e}")

    # If nothing came through, restore what was there before so we don't
    # leave the clipboard empty for no reason.
    if not selected and previous_clipboard:
        try:
            pyperclip.copy(previous_clipboard)
        except Exception:
            pass

    return selected


def paste_text(text: str, target_hwnd=None):
    """
    Puts `text` on the clipboard and simulates Ctrl+V to paste it into
    whatever field currently has focus (replacing the selection).
    Raises ClipboardError if the clipboard can't be written to.
    """
    if target_hwnd:
        try:
            user32 = ctypes.windll.user32
            user32.IsWindow.argtypes = [ctypes.c_void_p]
            user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
            hwnd = ctypes.c_void_p(target_hwnd)
            if user32.IsWindow(hwnd):
                user32.SetForegroundWindow(hwnd)
                time.sleep(0.12)
        except Exception:
            pass

    try:
        pyperclip.copy(text)
    except Exception as e:
        raise ClipboardError(f"Can't write to the system clipboard: {e}")

    time.sleep(0.1)
    _simulate('v')


def paste_text_streaming(text: str, target_hwnd=None, on_progress=None):
    """Replace the selection, then paste short chunks to create a readable stream."""
    if target_hwnd:
        try:
            user32 = ctypes.windll.user32
            user32.IsWindow.argtypes = [ctypes.c_void_p]
            user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
            hwnd = ctypes.c_void_p(target_hwnd)
            if user32.IsWindow(hwnd):
                user32.SetForegroundWindow(hwnd)
                time.sleep(0.12)
        except Exception:
            pass

    chunks = [chunk for chunk in re.split(r"(\s+)", text) if chunk]
    total = max(len(chunks), 1)
    for index, chunk in enumerate(chunks, start=1):
        try:
            pyperclip.copy(chunk)
        except Exception as e:
            raise ClipboardError(f"Can't write to the system clipboard: {e}")
        _simulate('v')
        if on_progress:
            on_progress(index / total)
        time.sleep(0.035)
