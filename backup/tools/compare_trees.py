#!/usr/bin/env python3

import argparse
import hashlib
import os
from dataclasses import dataclass


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

def _log(msg: str, tag: str = "<info>") -> None:
    if "<good>" in tag: tag = good_tag()
    elif "<bad>" in tag or "<error>" in tag: tag = bad_tag()
    elif "<warn>" in tag: tag = warn_tag()
    else: tag = info_tag()
    _log_base("main", tag, msg)
class CompareResult:
    ok: bool
    same: int
    different: int
    missing_left: int
    missing_right: int


def compare_dirs(*, left: str, right: str, max_diffs: int = 20) -> CompareResult:
    left = os.path.abspath(left)
    right = os.path.abspath(right)

    if not os.path.isdir(left):
        raise NotADirectoryError(left)
    if not os.path.isdir(right):
        raise NotADirectoryError(right)

    _log(f"Left:  {left}")
    _log(f"Right: {right}")

    left_files = _walk_files(left)
    right_files = _walk_files(right)

    left_keys = set(left_files.keys())
    right_keys = set(right_files.keys())

    missing_left = sorted(right_keys - left_keys)
    missing_right = sorted(left_keys - right_keys)

    if missing_left:
        _log(f"Missing in LEFT: {len(missing_left)}")
        for rel in missing_left[:max_diffs]:
            _log(f"  - {rel}")
        if len(missing_left) > max_diffs:
            _log(f"  ... and {len(missing_left) - max_diffs} more")

    if missing_right:
        _log(f"Missing in RIGHT: {len(missing_right)}")
        for rel in missing_right[:max_diffs]:
            _log(f"  - {rel}")
        if len(missing_right) > max_diffs:
            _log(f"  ... and {len(missing_right) - max_diffs} more")

    same = 0
    different = 0
    diffs_printed = 0

    common = sorted(left_keys & right_keys)
    _log(f"Common files: {len(common)}")

    for rel in common:
        lpath = left_files[rel]
        rpath = right_files[rel]
        lsha = _sha256_file(lpath)
        rsha = _sha256_file(rpath)
        if lsha == rsha:
            same += 1
        else:
            different += 1
            if diffs_printed < max_diffs:
                _log(f"DIFF {rel}: left={lsha} right={rsha}")
                diffs_printed += 1

    ok = (different == 0 and len(missing_left) == 0 and len(missing_right) == 0)
    _log(
        f"Summary: ok={ok} same={same} different={different} missing_left={len(missing_left)} missing_right={len(missing_right)}"
    )
    return CompareResult(
        ok=ok,
        same=same,
        different=different,
        missing_left=len(missing_left),
        missing_right=len(missing_right),
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Compare two directory trees by SHA256 (integrity compliance check).")
    ap.add_argument("--left", required=True, help="Left directory")
    ap.add_argument("--right", required=True, help="Right directory")
    ap.add_argument("--max-diffs", type=int, default=20, help="Max diff lines to print")
    args = ap.parse_args()

    res = compare_dirs(left=args.left, right=args.right, max_diffs=args.max_diffs)
    return 0 if res.ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
