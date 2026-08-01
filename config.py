"""
Connection configuration for the UCEI sprayer controller.

Two profiles are selected by the UCEI_TARGET environment variable:

    UCEI_TARGET=LOCAL   (default)  -> local OctoPrint on 127.0.0.1, Arduino on COM3
    UCEI_TARGET=PI                 -> OctoPrint on the Raspberry Pi, no local Arduino

The LOCAL API key is read from config_local.py, which is git-ignored so real
keys are never committed. Copy config_local.example.py to config_local.py and
fill in your local OctoPrint API key.
"""
import os

# OctoPrint listens on this port in both profiles.
OCTOPRINT_PORT = 5001

_TARGET = os.environ.get("UCEI_TARGET", "LOCAL").strip().upper()

if _TARGET == "LOCAL":
    OCTOPRINT_URL = f"http://127.0.0.1:{OCTOPRINT_PORT}/"
    ARDUINO_PORT = "COM3"
    # Local virtual/dev printer: homing on startup is safe, and interactive
    # motion needs no per-command confirmation.
    ALLOW_STARTUP_MOTION = True
    CONFIRM_MOTION = False
    try:
        from config_local import API_KEY
    except ImportError as exc:
        raise ImportError(
            "UCEI_TARGET=LOCAL requires config_local.py with an API_KEY. "
            "Copy config_local.example.py to config_local.py and set your "
            "local OctoPrint API key (config_local.py is git-ignored)."
        ) from exc

elif _TARGET == "PI":
    OCTOPRINT_URL = f"http://172.31.187.236:{OCTOPRINT_PORT}/"
    # This key belongs to the Pi's OctoPrint instance (private LAN).
    API_KEY = "ErDYaK23QBxF7Ka27f9zHV2sTz8MAHNWF76mROEJiuw"
    ARDUINO_PORT = None
    # Real hardware attached: never move on startup, and require an explicit
    # operator confirmation before any interactive motion command is sent.
    ALLOW_STARTUP_MOTION = False
    CONFIRM_MOTION = True

else:
    raise ValueError(f"Unknown UCEI_TARGET {_TARGET!r}; expected 'LOCAL' or 'PI'.")

# Resolved active profile name (used for logging and motion-confirmation dialogs).
TARGET = _TARGET
