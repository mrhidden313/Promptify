"""
autostart_helper.py
Manages Windows startup registry entry (HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run).
Uses standard library winreg - zero third-party dependencies required.
"""

import os
import sys
import winreg
import logging

LOGGER = logging.getLogger("Promptify")
REG_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_NAME = "Promptify"


def get_executable_command() -> str:
    """Returns the full command line to launch this application on startup."""
    if getattr(sys, "frozen", False):
        # Running as PyInstaller compiled executable
        return f'"{sys.executable}"'
    # Running from Python source
    main_py = os.path.abspath(os.path.join(os.path.dirname(__file__), "main.py"))
    python_exe = sys.executable.replace("python.exe", "pythonw.exe")
    if not os.path.exists(python_exe):
        python_exe = sys.executable
    return f'"{python_exe}" "{main_py}"'


def is_autostart_enabled() -> bool:
    """Check if Promptify is registered in the Windows current user Run registry."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_RUN_KEY, 0, winreg.KEY_READ) as key:
            val, _ = winreg.QueryValueEx(key, APP_NAME)
            return bool(val)
    except FileNotFoundError:
        return False
    except Exception as exc:
        LOGGER.warning("Could not read autostart registry key (%s)", exc)
        return False


def set_autostart(enabled: bool) -> bool:
    """
    Enable or disable Promptify starting automatically when Windows boots.
    Returns True on success, False on error.
    """
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                cmd = get_executable_command()
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, cmd)
                LOGGER.info("Autostart registered in Windows Run registry: %s", cmd)
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                    LOGGER.info("Autostart removed from Windows Run registry")
                except FileNotFoundError:
                    pass
        return True
    except Exception as exc:
        LOGGER.warning("Failed to update Windows autostart registry (%s)", exc)
        return False

