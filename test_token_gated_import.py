#!/usr/bin/env python3
"""A gated platform is importable exactly when the user's token is present.

Covers the guard rail (nothing is offered for import that cannot be fetched)
and the MakerWorld two-hop resolve, with no network and no `orca` host.
"""
import json
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Enough of the host for the capability class to be defined; nothing here is
# called, so no OrcaSlicer and no network.
orca_stub = types.ModuleType("orca")
# The host gives every capability get_config()/save_config(); get_config()
# returns a JSON *string*, and save_config() takes one and returns a bool.
class _CapabilityBase:
    _stored = "{}"

    def get_config(self):
        return self._stored

    def save_config(self, config):
        json.loads(config)          # the host refuses anything that is not JSON
        self._stored = config
        return True


orca_stub.script = types.SimpleNamespace(ScriptPluginCapabilityBase=_CapabilityBase)
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


def test_saving_a_token_merges_and_never_echoes_it():
    """Saving one platform's token must not drop another's, or leak the value."""
    cap = script.SearchEngineScript()
    cap._stored = json.dumps({"other_token": "keep-me"})

    posted = []
    cap._post = posted.append
    cap._save_token({"platform": "MakerWorld", "token": "  SECRET  "})
    # A successful save is followed by a refreshed Accounts list, so pick the
    # reply by its action rather than by position.
    saved = [m for m in posted if m["action"] == "token_saved"][-1]

    stored = json.loads(cap.get_config())
    assert stored["makerworld_token"] == "SECRET", "the token is stored, trimmed"
    assert stored["other_token"] == "keep-me", "a merge, not a replace"
    assert saved["ok"] is True and saved["platform"] == "MakerWorld"
    assert "SECRET" not in json.dumps(posted), "no reply may carry the token back"
    assert script._importable("MakerWorld", stored) is True, "import is now offered"

    cap._save_token({"platform": "Nexprint", "token": "x"})
    assert posted[-1]["ok"] is False, "a platform with no token key is refused"
    assert [m for m in posted if m["action"] == "accounts"], "the view is refreshed"


def test_accounts_rows_match_what_is_importable():
    """The setup screen must be derived from the import tables, not restated."""
    rows = {r["platform"]: r for r in script._accounts({})}

    assert rows["Printables"]["state"] == "open", "no account needed"
    assert rows["MakerWorld"]["state"] == "needs_token"
    assert rows["MakerWorld"]["help"], "a gated platform must say how to connect"
    assert rows["Nexprint"]["state"] == "unavailable", "no resolver, so no token helps"
    assert rows["GrabCAD"]["state"] == "unavailable"

    connected = {r["platform"]: r for r in script._accounts({"makerworld_token": "T"})}
    assert connected["MakerWorld"]["state"] == "connected"
    # Every platform the panel can search has a row, or the screen hides one.
    assert set(rows) == {a.PLATFORM for a in script._SEARCHERS.values()}


def test_signing_in_asks_for_a_code_and_never_a_password():
    """The in-window sign-in is Bambu's code login: email + 6 digits, no password."""
    posts = []
    fake = types.ModuleType("requests")

    def post(url, json=None, headers=None, timeout=None):
        posts.append({"url": url, "json": json or {}})
        if url.endswith("/sendemail/code"):
            return _Resp({}, 200)
        if url.endswith("/user/login"):
            if (json or {}).get("code") == "123456":
                return _Resp({"accessToken": "TOK", "refreshToken": "R"}, 200)
            return _Resp({"loginType": "tfa"}, 200)
        raise AssertionError("unexpected call: " + url)

    fake.post = post
    sys.modules["requests"] = fake

    note = script.MakerWorldSearcher.send_code("someone@example.com")
    # Measured against the live API: without this exact type the request is
    # answered as if the account did not exist.
    assert posts[-1]["json"] == {"email": "someone@example.com", "type": "codeLogin"}
    assert "code" in note.lower()

    token = script.MakerWorldSearcher.login_with_code("someone@example.com", "123456")
    assert token == "TOK"
    assert posts[-1]["json"] == {"account": "someone@example.com", "code": "123456"}
    assert "password" not in json.dumps(posts), "no request may carry a password"

    try:
        script.MakerWorldSearcher.login_with_code("someone@example.com", "999999")
    except RuntimeError as e:
        assert "tfa" in str(e), "a token-less success must name the missing step"
    else:
        raise AssertionError("a login with no token must raise")


def test_a_completed_sign_in_stores_the_token():
    cap = script.SearchEngineScript()
    cap._stored = "{}"
    posted = []
    cap._post = posted.append
    cap._do_auth("sign_in", {"platform": "MakerWorld",
                             "email": "someone@example.com", "code": "123456"})

    assert json.loads(cap.get_config())["makerworld_token"] == "TOK"
    assert [m for m in posted if m["action"] == "token_saved" and m["ok"]]

    cap._do_auth("sign_in", {"platform": "Printables"})
    assert posted[-1] == {"action": "auth", "ok": False, "platform": "Printables",
                          "message": "Printables cannot be signed into from here."}


def test_makeronline_signs_in_with_a_password_and_keeps_only_the_token():
    """Anycubic's SSO has no e-mail code, so this one really does take a password.

    Measured on the live endpoints: Casdoor answers /api/login with the OAuth
    code in `data`, and only the site token that follows is worth storing.
    """
    posts = []
    fake = types.ModuleType("requests")

    def post(url, json=None, params=None, headers=None, timeout=None):
        posts.append({"url": url, "json": json or {}, "params": params or {}})
        if url.endswith("cas.anycubic.com/api/login"):
            if (json or {}).get("password") == "hunter2":
                return _Resp({"status": "ok", "msg": "", "data": "CODE42"})
            return _Resp({"status": "error", "msg": "password is incorrect"})
        if url.endswith("/api/v2/auth/login"):
            assert (json or {}).get("code") == "CODE42"
            return _Resp({"code": 0, "data": {"token": "JWT"}})
        if url.endswith("/api/file/download"):
            return _Resp({"code": 0, "data": "https://s3/signed.stl"})
        raise AssertionError("unexpected call: " + url)

    fake.post = post
    sys.modules["requests"] = fake

    token = script.MakeronlineSearcher.sign_in("someone@example.com", "hunter2")
    assert token == "JWT", "the stored credential is the site token, not the password"
    # The application and organization are what Casdoor needs to find the
    # account at all; without them it answers with a null application.
    assert posts[0]["json"]["application"] == "ac_maker_online"
    assert posts[0]["json"]["organization"] == "anycubic"
    assert posts[0]["json"]["type"] == "code"
    # The live page sends neither of these; matching it exactly is the whole
    # point of not inventing a request shape.
    assert "autoSignin" not in posts[0]["json"]
    assert "signinMethod" not in posts[0]["json"]
    assert "hunter2" not in json.dumps(posts[1:]), \
        "the password must not travel past Anycubic"

    try:
        script.MakeronlineSearcher.sign_in("someone@example.com", "wrong")
    except RuntimeError as e:
        assert "incorrect" in str(e), "the portal's own words, not an invented one"
    else:
        raise AssertionError("a rejected sign-in must raise")

    # A failed sign-in stores nothing, so the platform never claims to be
    # connected and the import it would offer cannot be a dead end.
    cap = script.SearchEngineScript()
    cap._stored = "{}"
    posted = []
    cap._post = posted.append
    cap._do_auth("sign_in", {"platform": "Makeronline",
                             "email": "someone@example.com", "password": "wrong"})
    assert json.loads(cap.get_config()) == {}, "nothing is stored on failure"
    assert posted[-1]["ok"] is False

    cap._do_auth("send_code", {"platform": "Makeronline", "email": "x@y.z"})
    assert posted[-1]["ok"] is False and "password" in posted[-1]["message"], \
        "a portal with no e-mail code must say so rather than promise a mail"

    cap._do_auth("sign_in", {"platform": "Makeronline",
                             "email": "someone@example.com", "password": "hunter2"})
    assert json.loads(cap.get_config())["makeronline_token"] == "JWT"
    assert "hunter2" not in json.dumps(posted), "no reply may carry the password"

    rows = {r["platform"]: r for r in script._accounts({})}
    assert rows["Makeronline"]["sign_in_mode"] == "password"
    assert rows["MakerWorld"]["sign_in_mode"] == "code"

    url = script.MakeronlineSearcher.mint_url(
        {"_key": "b48e37341606360c43d2807eb07dc2f0", "_mold_id": "151818",
         "name": "Box stl.stl"}, {"makeronline_token": "JWT"})
    assert url == "https://s3/signed.stl"
    # The live endpoint rejects anything else: file_type 1 is the model file and
    # mold_id is the integer id out of the /<id>.html URL.
    assert posts[-1]["json"] == {"key": "b48e37341606360c43d2807eb07dc2f0",
                                 "file_type": 1, "mold_id": 151818,
                                 "is_batch_down": 0}, posts[-1]["json"]


def test_makeronline_resolver_needs_a_token():
    try:
        script.MakeronlineSearcher.get_files(
            "https://www.makeronline.com/model/Box/151818.html", {})
    except RuntimeError as e:
        assert "makeronline_token" in str(e), "the error must name the config key"
    else:
        raise AssertionError("resolving without a token must raise")


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
    test_saving_a_token_merges_and_never_echoes_it()
    test_accounts_rows_match_what_is_importable()
    test_signing_in_asks_for_a_code_and_never_a_password()
    test_a_completed_sign_in_stores_the_token()
    test_makeronline_signs_in_with_a_password_and_keeps_only_the_token()
    test_makeronline_resolver_needs_a_token()
    calls = _install_fake_requests()
    files = test_listing_profiles_mints_nothing(calls)
    test_minting_uses_the_alphanumeric_model_id(calls, files)
    print("token-gated import: ok")
