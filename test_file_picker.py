#!/usr/bin/env python3
"""A multi-part print must be picked before anything is downloaded.

Runs without OrcaSlicer: a stub `orca` module is enough for the plugin's
capability class to be defined, and the resolver and downloader are replaced,
so this makes no network calls.
"""
import os
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

FILES = [{"name": "part%d.stl" % i, "url": "https://x/%d" % i} for i in range(3)]
MODEL = {"platform": "Printables", "url": "https://www.printables.com/model/1-x"}


def run(files_arg, resolved):
    """Drive _do_import with a stubbed resolver/downloader; return posted messages."""
    script = se.SearchEngineScript()
    posted = []
    script._post = posted.append
    downloaded = []
    script._download = lambda url, name, d: downloaded.append(name) or os.path.join(d, name)
    se._FILE_RESOLVERS["Printables"] = lambda url: list(resolved)
    se._download_dir = lambda: "/tmp/picker_test"
    se._load_in_orca = lambda paths: (True, "")
    script._do_import(MODEL, files_arg)
    return posted, downloaded


def main():
    resolver = se._FILE_RESOLVERS["Printables"]
    try:
        # More than one file: ask, download nothing yet.
        posted, downloaded = run(None, FILES)
        assert [m["action"] for m in posted] == ["choose_files"], posted
        assert posted[0]["files"] == FILES
        assert downloaded == [], downloaded

        # The selection comes back: only those files are fetched.
        chosen = [FILES[0], FILES[2]]
        posted, downloaded = run(chosen, FILES)
        assert downloaded == ["part0.stl", "part2.stl"], downloaded
        assert posted[-1] == {"action": "imported", "count": 2, "dir": "/tmp/picker_test"}, posted[-1]

        # A single-file print still imports on one press, no picker.
        posted, downloaded = run(None, FILES[:1])
        assert "choose_files" not in [m["action"] for m in posted], posted
        assert downloaded == ["part0.stl"], downloaded
    finally:
        se._FILE_RESOLVERS["Printables"] = resolver
    print("file picker: ok")


if __name__ == "__main__":
    main()
