"""
config.py
Handles reading/writing user settings (API keys, provider, model) to a local
JSON file stored next to the app, so settings persist between runs.
"""

import json
import os
import base64
import tempfile
import win32crypt

# Settings file lives in the user's home folder so it survives reinstalls
# of the .exe and doesn't need admin rights to write.
CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".roman_ai_fixer")
CONFIG_FILE = os.path.join(CONFIG_DIR, "settings.json")
SUPPORTED_PROVIDERS = ("gemini", "openai", "deepseek", "groq", "xai", "custom")
DEFAULT_PROVIDER_ORDER = ["gemini", "openai", "deepseek", "groq", "xai", "custom"]

DEFAULT_SETTINGS = {
    "provider": "gemini",
    "api_key": "",
    "model": "gemini-3.5-flash-lite",
    "provider_order": DEFAULT_PROVIDER_ORDER,
    "custom_provider_name": "OpenRouter",
    "custom_base_url": "https://openrouter.ai/api/v1",
    "custom_model": "",
    "allow_provider_fallback": True,
    "word_typing_enabled": True,
    "default_action": "rewrite_same",
    "hotkey": "<ctrl>+<alt>+f",
    "hotkey_enabled": True,
    "position_locked": False,
    "floating_x": 40,
    "floating_y": 40,
}

CREDENTIAL_FIELDS = (
    "api_key", *(f"{provider}_api_key" for provider in SUPPORTED_PROVIDERS)
)
ENCRYPTED_CREDENTIALS_FIELD = "encrypted_api_keys"
DPAPI_ENTROPY = b"Promptify/credentials/v1"


class CredentialStoreError(RuntimeError):
    pass


def _protect_credential(value):
    try:
        encrypted = win32crypt.CryptProtectData(
            value.encode("utf-8"), "Promptify", DPAPI_ENTROPY, None, None, 0
        )
        return base64.b64encode(encrypted).decode("ascii")
    except Exception as exc:
        raise CredentialStoreError("Windows could not protect an API key with DPAPI.") from exc


def _unprotect_credential(value):
    try:
        encrypted = base64.b64decode(value, validate=True)
        _, cleartext = win32crypt.CryptUnprotectData(
            encrypted, DPAPI_ENTROPY, None, None, 0
        )
        return cleartext.decode("utf-8")
    except Exception as exc:
        raise CredentialStoreError(
            "Saved API keys are locked to the Windows account that created them. Re-enter them in Settings."
        ) from exc


def ensure_config_dir():
    if not os.path.exists(CONFIG_DIR):
        os.makedirs(CONFIG_DIR, exist_ok=True)


def load_settings():
    """Load settings from disk, falling back to defaults for any missing key."""
    ensure_config_dir()
    if not os.path.exists(CONFIG_FILE):
        save_settings(DEFAULT_SETTINGS)
        return dict(DEFAULT_SETTINGS)

    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        data = {}
    if not isinstance(data, dict):
        data = {}

    # Merge with defaults so new fields introduced later don't break old configs
    merged = dict(DEFAULT_SETTINGS)
    merged.update(data)
    encrypted_credentials = data.get(ENCRYPTED_CREDENTIALS_FIELD, {})
    credential_store_error = ""
    if isinstance(encrypted_credentials, dict):
        for key in CREDENTIAL_FIELDS:
            encrypted_value = encrypted_credentials.get(key)
            if isinstance(encrypted_value, str) and encrypted_value:
                try:
                    merged[key] = _unprotect_credential(encrypted_value)
                except CredentialStoreError as exc:
                    merged[key] = ""
                    credential_store_error = str(exc)
    if merged["provider"] not in SUPPORTED_PROVIDERS:
        merged["provider"] = "gemini"
    saved_order = merged.get("provider_order")
    if not isinstance(saved_order, list):
        saved_order = DEFAULT_PROVIDER_ORDER
    merged["provider_order"] = list(dict.fromkeys(
        provider for provider in saved_order
        if isinstance(provider, str) and provider in SUPPORTED_PROVIDERS
    ))
    merged["provider_order"] = [
        *merged["provider_order"],
        *(provider for provider in DEFAULT_PROVIDER_ORDER if provider not in merged["provider_order"]),
    ]
    if not isinstance(merged.get("allow_provider_fallback"), bool):
        merged["allow_provider_fallback"] = True
    if not isinstance(merged.get("word_typing_enabled"), bool):
        merged["word_typing_enabled"] = True
    if merged.get("default_action") not in (
        "rewrite_same", "roman_urdu", "translate_enhance"
    ):
        merged["default_action"] = DEFAULT_SETTINGS["default_action"]
    if not isinstance(merged.get("hotkey"), str):
        merged["hotkey"] = DEFAULT_SETTINGS["hotkey"]
    if not isinstance(merged.get("hotkey_enabled"), bool):
        merged["hotkey_enabled"] = DEFAULT_SETTINGS["hotkey_enabled"]
    if not isinstance(merged.get("position_locked"), bool):
        merged["position_locked"] = DEFAULT_SETTINGS["position_locked"]
    for coordinate in ("floating_x", "floating_y"):
        value = merged.get(coordinate)
        if isinstance(value, bool) or not isinstance(value, int):
            merged[coordinate] = DEFAULT_SETTINGS[coordinate]
    for key in (
        "api_key", "model", "custom_provider_name", "custom_base_url",
        "custom_model", *(f"{provider}_api_key" for provider in SUPPORTED_PROVIDERS),
    ):
        if not isinstance(merged.get(key), str):
            merged[key] = (
                DEFAULT_SETTINGS[key]
                if key in DEFAULT_SETTINGS
                else ""
            )
    merged["custom_provider_name"] = (
        merged["custom_provider_name"].strip()[:40]
        or DEFAULT_SETTINGS["custom_provider_name"]
    )
    if merged["model"] == "gemini-flash-latest":
        merged["model"] = "gemini-3.5-flash-lite"
    merged["credential_store_error"] = credential_store_error

    has_legacy_credentials = any(
        isinstance(data.get(key), str) and data.get(key)
        for key in CREDENTIAL_FIELDS
    )
    if has_legacy_credentials and not credential_store_error:
        save_settings(merged)
    return merged


def save_settings(settings: dict):
    ensure_config_dir()
    data = {
        key: value for key, value in settings.items()
        if key not in CREDENTIAL_FIELDS and key not in (
            ENCRYPTED_CREDENTIALS_FIELD, "credential_store_error"
        )
    }
    credentials = {}
    for key in CREDENTIAL_FIELDS:
        value = settings.get(key, "")
        if isinstance(value, str) and value:
            credentials[key] = _protect_credential(value)
    data[ENCRYPTED_CREDENTIALS_FIELD] = credentials

    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=CONFIG_DIR, delete=False
        ) as f:
            temp_path = f.name
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(temp_path, CONFIG_FILE)
    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)
