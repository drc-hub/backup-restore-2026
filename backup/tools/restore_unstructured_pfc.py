#!/usr/bin/env python3

import argparse
import hashlib
import json
import os
import shutil
import zlib


import sys
import datetime

LOG_FILE = "/home/primary/utilities/backup/backup.log"

def _isatty() -> bool:
    try:
        return sys.stdout.isatty()
    except Exception:
        return False

def _color_enabled() -> bool:
    if os.environ.get("NO_COLOR") is not None:
        return False
    return _isatty() and os.environ.get("LOG_COLOR", "1") != "0"

def _c(s: str, code: str) -> str:
    if not _color_enabled():
        return s
    return f"\x1b[{code}m{s}\x1b[0m"

def good_tag() -> str: return _c("<good>", "32")
def bad_tag() -> str: return _c("<error>", "31")
def info_tag() -> str: return _c("<info>", "36")
def warn_tag() -> str: return _c("<warn>", "33")

def _tag_good() -> str: return good_tag()
def _tag_bad() -> str: return bad_tag()
def _tag_info() -> str: return info_tag()

def _log_base(scope: str, tag: str, msg: str) -> None:
    import re
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    clean_tag = ansi_escape.sub('', tag)
    
    if clean_tag not in ["<good>", "<error>", "<info>", "<warn>"]:
        tag = info_tag()
        clean_tag = "<info>"
        
    term_line = f"{scope:<10} {tag:<8} {msg}"
    print(term_line)
    
    try:
        import datetime
        tz = datetime.timezone(datetime.timedelta(hours=7))
        ts = datetime.datetime.now(tz).replace(microsecond=0).isoformat()
        file_line = f"[{ts}] {scope:<10} {clean_tag:<8} {msg}\n"
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(file_line)
    except Exception:
        pass

def _log(prefix: str, msg: str, tag: str = "<info>") -> None:
    if "<good>" in tag: tag = good_tag()
    elif "<bad>" in tag or "<error>" in tag: tag = bad_tag()
    elif "<warn>" in tag: tag = warn_tag()
    else: tag = info_tag()
    _log_base(prefix, tag, msg)
