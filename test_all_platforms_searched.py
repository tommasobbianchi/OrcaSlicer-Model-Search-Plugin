#!/usr/bin/env python3
"""Every platform the user ticks is searched, and its results are shown.

The plugin used to drop any platform whose files it cannot fetch, so only Printables ever
appeared. Searching is allowed everywhere; only importing is restricted. Each result must say
which of the two it is, through an `importable` flag.

No network: the adapters are replaced with stubs, and a stub `orca` module is enough for the
capability class to be defined.
"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

orca_stub = types.ModuleType("orca")
# The host gives every capability get_config(); it returns a JSON *string*.
class _CapabilityBase:
    def get_config(self):
        return "{}"


orca_stub.script = types.SimpleNamespace(ScriptPluginCapabilityBase=_CapabilityBase)
orca_stub.base = object
orca_stub.plugin = lambda cls: cls
orca_stub.register_capability = lambda cls: None
orca_stub.ExecutionResult = types.SimpleNamespace(success=lambda: None)
orca_stub.host = types.SimpleNamespace(ui=types.SimpleNamespace(create_window=None))
sys.modules["orca"] = orca_stub

import search_engine as se

WANT = {"printables": "Printables", "makerworld": "MakerWorld",
        "nexprint": "Nexprint", "makeronline": "Makeronline"}


def stub_adapter(platform):
    class Stub:
        PLATFORM = platform

        @staticmethod
        def enabled(tokens):
            return True

        @staticmethod
        def search(query, tokens):
            return [{"name": f"{platform} thing", "platform": platform,
                     "url": f"https://example.invalid/{platform.lower()}/1", "author": "x"}]
    return Stub


class Win:
    def __init__(self, sink):
        self.sink = sink

    def is_open(self):
        return True

    def post(self, msg):
        self.sink.append(msg)


def main():
    script = se.SearchEngineScript()
    posted = []
    script._post = posted.append
    script.win = Win(posted)

    se._SEARCHERS = {key: stub_adapter(plat) for key, plat in WANT.items()}
    # Only Printables serves files without a login; that is what `importable` must reflect.
    se._FILE_RESOLVERS = {"Printables": lambda url: [{"name": "a.stl", "url": "https://x/a"}]}

    script._do_search({"query": "bracket", "platforms": list(WANT)})

    results = None
    for msg in posted:
        if msg.get("action") == "results":
            results = msg["results"]
    if results is None:
        print("FAIL: no results message was posted; got actions",
              [m.get("action") for m in posted])
        return 1

    problems = []
    seen = {r.get("platform") for r in results}
    for plat in WANT.values():
        if plat not in seen:
            problems.append(f"{plat}: searched but its results were dropped")
    for r in results:
        if "importable" not in r:
            problems.append(f"{r.get('platform')}: result has no 'importable' flag")
            continue
        want = r.get("platform") == "Printables"
        if bool(r["importable"]) is not want:
            problems.append(f"{r.get('platform')}: importable={r['importable']!r}, expected {want}")
    if problems:
        print("FAIL\n  " + "\n  ".join(problems))
        return 1
    print(f"all platforms searched: ok ({len(results)} results, {len(seen)} platforms)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
