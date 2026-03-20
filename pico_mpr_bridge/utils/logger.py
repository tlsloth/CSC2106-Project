# utils/logger.py — Simple serial logger with severity levels

import time

_LEVELS = {"DEBUG": 0, "INFO": 1, "WARN": 2, "ERROR": 3}
_level = 0  # default: DEBUG


def set_level(level_name):
    global _level
    _level = _LEVELS.get(level_name, 0)


def _log(level_name, tag, msg):
    if _LEVELS.get(level_name, 0) >= _level:
        t = time.ticks_ms()
        print("[{:>5}] {:010d} [{}] {}".format(level_name, t, tag, msg))


def debug(tag, msg):
    _log("DEBUG", tag, msg)


def info(tag, msg):
    _log("INFO", tag, msg)


def warn(tag, msg):
    _log("WARN", tag, msg)


def error(tag, msg):
    _log("ERROR", tag, msg)
