#!/usr/bin/env python3
"""A gated platform is importable exactly when the user's token is present.

Covers the guard rail (nothing is offered for import that cannot be fetched)
and the MakerWorld two-hop resolve, with no network and no `orca` host.
"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Enough of the host for the capability class to be defined; nothing here is
# called, so no OrcaSlicer and no network.
orca_stub = types.ModuleType("orca")
orca_stub.script = types.SimpleNamespace(ScriptPluginCapabilityBase=object)
orca_stub.base = object
orca_stub.plugin = lambda cls: cls
orca_stub.register_capability = lambda cls: None
orca_stub.ExecutionResult = types.SimpleNamespace(success=lambda: None)
orca_stub.host = types.SimpleNamespace(ui=types.SimpleNamespace(create_window=None))
sys.modules["orca"] = orca_stub

import search_engine as script  # noqa: E402


def test_importable_follows_the_token():
    assert script._importable("Printables", {}) is True, "Printables needs no token"
    assert script._importable("MakerWorld", {}) is False, "no token, not offered"
    assert script._importable("MakerWorld", {"makerworld_token": "   "}) is False, \
        "whitespace is not a token"
    assert script._importable("MakerWorld", {"makerworld_token": "abc"}) is True
    assert script._importable("Nexprint", {"makerworld_token": "abc"}) is False, \
        "a token for one platform must not unlock another"


def test_makerworld_resolver_needs_a_token():
    try:
        script.MakerWorldSearcher.get_files("https://makerworld.com/en/models/1400373", {})
    except RuntimeError as e:
        assert "makerworld_token" in str(e), "the error must name the config key"
    else:
        raise AssertionError("resolving without a token must raise")


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("HTTP %d" % self.status_code)


def test_listing_profiles_mints_nothing(calls):
    """Listing must stay anonymous and cheap: a design can carry 91 profiles."""
    files = script.MakerWorldSearcher.get_files(
        "https://makerworld.com/en/models/1400373", {"makerworld_token": "TOK"})

    assert [f["name"] for f in files] == ["Plate A.3mf", "Plate B.3mf"], \
        "a hit without a profileId is skipped"
    assert [f["url"] for f in files] == ["", ""], "no URL is minted while listing"
    assert not any("iot-service" in c["url"] for c in calls), \
        "listing must not hit the authenticated download endpoint"
    assert all("Authorization" not in c["headers"] for c in calls), \
        "listing is anonymous, so the token must not leak into it"
    return files


def test_minting_uses_the_alphanumeric_model_id(calls, files):
    del calls[:]
    url = script.MakerWorldSearcher.mint_url(files[0], {"makerworld_token": "TOK"})

    assert url == "https://cdn/a?key=1"
    assert len(calls) == 1, "one call per picked file"
    # The integer designId from the /models/<N> URL is rejected by this endpoint;
    # it must be the alphanumeric modelId taken from the design.
    assert calls[0]["params"] == {"model_id": "US2bb73b106683e5"}, calls[0]["params"]
    assert calls[0]["headers"]["Authorization"] == "Bearer TOK"


def _install_fake_requests():
    """Patch the `requests` module the resolver imports, recording every call."""
    calls = []
    fake = types.ModuleType("requests")

    def get(url, headers=None, params=None, timeout=None):
        calls.append({"url": url, "headers": headers or {}, "params": params})
        if url.endswith("/design/1400373"):
            return _Resp({"modelId": "US2bb73b106683e5"})
        if url.endswith("/instances"):
            return _Resp({"hits": [{"profileId": 11, "title": "Plate A"},
                                   {"profileId": 12, "title": "Plate B"},
                                   {"title": "no profileId, skipped"}]})
        if url.endswith("/profile/11"):
            return _Resp({"url": "https://cdn/a?key=1"})
        if url.endswith("/profile/12"):
            return _Resp({"url": "https://cdn/b?key=2"})
        raise AssertionError("unexpected call: " + url)

    fake.get = get
    sys.modules["requests"] = fake
    return calls


if __name__ == "__main__":
    test_importable_follows_the_token()
    test_makerworld_resolver_needs_a_token()
    calls = _install_fake_requests()
    files = test_listing_profiles_mints_nothing(calls)
    test_minting_uses_the_alphanumeric_model_id(calls, files)
    print("token-gated import: ok")
