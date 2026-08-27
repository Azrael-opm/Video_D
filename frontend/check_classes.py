#!/usr/bin/env python3
"""Hygiene guard: every class used by the frontend must actually exist.

A class that is used in markup/JS but missing from the compiled
tailwind.css (and not defined in a page <style>) silently does nothing —
that's how the "hero glued to navbar" bug shipped. Run this after any
frontend or tailwind change; it exits non-zero when something is missing.

    python3 frontend/check_classes.py
"""
import re
import sys
from pathlib import Path

FE = Path(__file__).parent
PAGES = ("index.html", "recode.html", "admin.html")

css = (FE / "tailwind.css").read_text(encoding="utf-8")

# classes defined inside any page <style> block are legal too
styled = set()
for f in PAGES:
    for block in re.finditer(r"<style>(.*?)</style>", (FE / f).read_text(encoding="utf-8"), re.S):
        styled |= set(re.findall(r"\.([a-zA-Z][\w-]*)", block.group(1)))

# rare intentional customs that live in JS-injected markup only
ALLOW = {"chip", "bulk-dl", "bulk-sel", "bulk-status", "tabpane"}


def in_css(token: str) -> bool:
    # mirror tailwind's CSS escaping (commas become "\2c " in class names)
    out = []
    for c in token:
        if c == ",":
            out.append("\\2c ")
        elif c in ":[]()/.#%!":
            out.append("\\" + c)
        else:
            out.append(c)
    return "." + "".join(out) in css


def collect(path: Path) -> set:
    src = path.read_text(encoding="utf-8")
    blobs = re.findall(r'class="([^"]+)"', src)
    blobs += re.findall(r'className\s*=\s*`([^`]+)`', src)
    blobs = [re.sub(r"\$\{.*?\}", " ", b) for b in blobs]  # drop JS expressions
    blobs = [re.sub(r"\$\{[^}]*", " ", b) for b in blobs]  # …incl. unclosed ${
    out = set()
    for blob in blobs:
        for t in blob.split():
            if "${" in t or "{" in t:
                continue
            out.add(t)
    return out


tokens = set()
for f in PAGES:
    tokens |= collect(FE / f)
tokens |= collect(FE / "app.js")

missing = sorted(
    t for t in tokens
    if t not in styled and t not in ALLOW and not in_css(t)
)

if missing:
    print("MISSING CLASSES (used but not compiled/defined):")
    for t in missing:
        print("  -", t)
    sys.exit(1)
print(f"ok — {len(tokens)} class tokens verified against tailwind.css + page styles")
