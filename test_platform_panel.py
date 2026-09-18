#!/usr/bin/env python3
"""The panel must offer every platform whose search works, and be honest about the rest.

Searchable today (verified in HANDOFF.md): Printables, MakerWorld, Nexprint, Makeronline.
Not searchable: Thingiverse (needs a token, download 403) and GrabCAD (API retired) — they must
still be visible, but disabled and with a reason, so the window does not pretend they do not exist.

This reads the panel HTML out of the module, so it needs no browser.
"""
import os
import re
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

orca_stub = types.ModuleType("orca")
orca_stub.script = types.SimpleNamespace(ScriptPluginCapabilityBase=object)
orca_stub.base = object
orca_stub.plugin = lambda cls: cls
orca_stub.register_capability = lambda cls: None
orca_stub.ExecutionResult = types.SimpleNamespace(success=lambda: None)
orca_stub.host = types.SimpleNamespace(ui=types.SimpleNamespace(create_window=None))
sys.modules["orca"] = orca_stub

import search_engine as se

SEARCHABLE = ["printables", "makerworld", "nexprint", "makeronline"]
GATED = ["thingiverse", "grabcad"]


def label_for(html, key):
    """The <label> element that carries data-platform="<key>"."""
    for m in re.finditer(r"<label\b[^>]*>.*?</label>", html, re.S | re.I):
        if f'data-platform="{key}"' in m.group(0):
            return m.group(0)
    return None


def main():
    html = se.PAGE
    problems = []

    block = re.search(r'<div class="platforms">(.*?)</div>', html, re.S)
    if not block:
        print("FAIL: no .platforms block in the panel HTML")
        return 1
    platforms_html = block.group(1)

    for key in SEARCHABLE:
        lab = label_for(platforms_html, key)
        if lab is None:
            problems.append(f"{key}: not offered in the panel")
            continue
        if not re.search(r"<input[^>]*\bchecked\b", lab):
            problems.append(f"{key}: offered but not ticked by default")
        if re.search(r"<input[^>]*\bdisabled\b", lab):
            problems.append(f"{key}: offered but disabled")

    for key in GATED:
        lab = label_for(platforms_html, key)
        if lab is None:
            problems.append(f"{key}: not shown at all (it must be visible but disabled)")
            continue
        if not re.search(r"<input[^>]*\bdisabled\b", lab):
            problems.append(f"{key}: shown as selectable, but its search does not work")
        text = re.sub(r"<[^>]+>", " ", lab)
        if not re.search(r"token|retired|unavailable|no api|not available|login|log in", text, re.I):
            problems.append(f"{key}: no reason given ({' '.join(text.split())!r})")

    # The PEP 723 header describes the plugin in the hub listing; it must not still claim that
    # only importable platforms are offered.
    header = se.__doc__ or ""
    try:
        header += open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "search_engine.py")).read()[:4000]
    except OSError:
        pass
    if re.search(r"Only platforms whose files can be fetched[^\"]*are offered", header):
        problems.append("the plugin description still says only fetchable platforms are offered")

    if problems:
        print("FAIL\n  " + "\n  ".join(problems))
        return 1
    print(f"platform panel: ok ({len(SEARCHABLE)} searchable, {len(GATED)} shown disabled)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
