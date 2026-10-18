# /// script
# requires-python = ">=3.12"
# dependencies = ["requests"]
#
# [tool.orcaslicer.plugin]
# name = "3D Model Search Engine"
# description = "Search 3D models from inside OrcaSlicer and load them straight onto the plate. Printables needs no account. MakerWorld signs in from the plugin’s own window with a six-digit code Bambu emails you, and Makeronline with the Anycubic account you already have — whichever each portal actually offers, asked for in the plugin window and never in a browser. Credentials are sent only to the portal they belong to; just the token it returns is kept, in OrcaSlicer’s plugin config and nowhere else. The licence is always shown before the download, and no external browser is ever opened."
# author = "Tommaso Bianchi"
# version = "0.5.1"
# ///

try:
    import orca
except ImportError:
    orca = None

import json
import re
import subprocess
import sys
import threading
import os
import time
import urllib.parse


def _orca_data_dir():
    """Orca's data directory, or "" when the file is not installed as a plugin.

    Plugins live at <datadir>/orca_plugins/<name>/<file>.py, and cloud-subscribed
    ones a couple of levels deeper, so walk up until orca_plugins is the child.
    """
    path = os.path.dirname(os.path.abspath(__file__))
    while True:
        parent, leaf = os.path.split(path)
        if leaf == "orca_plugins":
            return parent
        if not parent or parent == path:
            return ""
        path = parent


def _download_dir():
    """Where downloaded models are written.

    The CPython audit hook only permits writes under Orca's datadir — the log
    says so on every start ("[AUDIT] Global allowed root: <datadir>"), so
    ~/Downloads raises PermissionError once a build enforces it.
    """
    datadir = _orca_data_dir()
    if datadir:
        return os.path.join(datadir, "model_downloads")
    # Running the file directly: nothing is auditing us, so the old location is fine.
    return os.path.join(os.path.expanduser("~/Downloads"), "OrcaModelSearch")


def _own_instance_hashes():
    """Instance hashes that belong to *this* data directory, newest first.

    instance_check() names its lock file <hash>.lock under <datadir>/cache/ and
    the D-Bus name carries the same hash, so the cache directory identifies which
    of the buses on the session belongs to the app hosting this plugin. Old
    builds leave their locks behind, hence a candidate list rather than one answer.
    """
    datadir = _orca_data_dir()
    if not datadir:
        return []
    cache = os.path.join(datadir, "cache")
    try:
        locks = [f for f in os.listdir(cache) if f.endswith(".lock") and f[:-5].isdigit()]
    except OSError:
        return []
    locks.sort(key=lambda f: os.path.getmtime(os.path.join(cache, f)), reverse=True)
    return [f[:-5] for f in locks]


LICENSE_DESCRIPTIONS = {
    "CC BY": "Share and adapt for any purpose. Must credit the author.",
    "CC BY-SA": "Share and adapt, credit author, same license for remixes.",
    "CC BY-NC": "Share and adapt, non-commercial only. Must credit.",
    "CC BY-NC-SA": "Non-commercial, credit required, share-alike.",
    "CC BY-ND": "Share but do NOT modify. Credit required.",
    "CC BY-NC-ND": "Share only, no mods, no commercial use. Credit required.",
    "CC0": "Public domain. No rights reserved. Free for any use.",
    "PD": "Public domain. No restrictions.",
    "All Rights Reserved": "Personal use only. No redistribution or modification.",
    "Standard Digital File License": "MakerWorld standard license. Personal use, no redistribution.",
    "Exclusive": "MakerWorld exclusive. Check platform terms.",
}


_BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")


def _orca_executable():
    """Absolute path of the running OrcaSlicer binary.

    Each OS has its own way to ask; sys.executable is the embedded interpreter's
    idea of it and is only the last resort.
    """
    if sys.platform == "darwin":
        import ctypes

        buf = ctypes.create_string_buffer(4096)
        size = ctypes.c_uint32(len(buf))
        if ctypes.CDLL(None)._NSGetExecutablePath(buf, ctypes.byref(size)) == 0:
            return os.path.realpath(buf.value.decode())
    elif os.name == "nt":
        import ctypes

        buf = ctypes.create_unicode_buffer(4096)
        if ctypes.windll.kernel32.GetModuleFileNameW(None, buf, len(buf)):
            return buf.value
    else:
        try:
            return os.path.realpath("/proc/self/exe")
        except OSError:
            pass
    return sys.executable or ""


def _instance_payload(paths):
    """The argv list instance_check sends, in unescape_strings_cstyle format:
    semicolon-separated and quoted. argv[0] is skipped as the executable path."""
    argv = ["orca-slicer"] + list(paths)
    return ";".join('"%s"' % a.replace("\\", "\\\\").replace('"', '\\"') for a in argv)


def _handoff_windows(paths):
    """Deliver the files through WM_COPYDATA, the way a second launch would.

    GUI_App registers an MSW handler for WM_COPYDATA with dwData == 1 and passes
    the wide string straight to handle_message(). InstanceCheck finds the target
    by comparing instance hashes, but this code runs *inside* the target process,
    so matching our own PID identifies the window without any hash at all.
    """
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.GetPropW.restype = wintypes.HANDLE
    user32.GetPropW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
    user32.SendMessageW.restype = ctypes.c_longlong
    user32.SendMessageW.argtypes = [wintypes.HWND, ctypes.c_uint,
                                    ctypes.c_void_p, ctypes.c_void_p]

    class COPYDATASTRUCT(ctypes.Structure):
        _fields_ = [("dwData", ctypes.c_void_p),
                    ("cbData", wintypes.DWORD),
                    ("lpData", ctypes.c_void_p)]

    our_pid = kernel32.GetCurrentProcessId()
    found = []

    def _enum(hwnd, _lparam):
        name = ctypes.create_unicode_buffer(256)
        if user32.GetClassNameW(hwnd, name, 256) == 0 or name.value != "wxWindowNR":
            return True
        # Both properties are set on the main frame only (init_windows_properties).
        if not user32.GetPropW(hwnd, "Instance_Hash_Minor"):
            return True
        if not user32.GetPropW(hwnd, "Instance_Hash_Major"):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value != our_pid:
            return True
        found.append(hwnd)
        return False

    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(_enum)
    user32.EnumWindows(proc, 0)
    if not found:
        return False, "no OrcaSlicer main window found in this process"

    buf = ctypes.create_unicode_buffer(_instance_payload(paths))
    data = COPYDATASTRUCT(ctypes.c_void_p(1), ctypes.sizeof(buf),
                          ctypes.cast(buf, ctypes.c_void_p))
    # WM_COPYDATA. SendMessage blocks until the GUI thread has handled it, which
    # is fine from this worker thread and would deadlock only on the GUI thread.
    user32.SendMessageW(found[0], 0x004A, None, ctypes.byref(data))
    return True, ""


def _handoff_macos(paths):
    """Hand the files to the running app through LaunchServices.

    `open -a <bundle> <file>` sends an "open documents" Apple Event to the
    instance that is already running, which wxWidgets turns into MacOpenFiles().
    Orca only spawns a second slicer there for .3mf files, and models arrive as
    .stl/.step, so they load into this instance. This avoids reimplementing
    NSDistributedNotificationCenter in ctypes for no gain.
    """
    exe = _orca_executable()
    bundle = exe
    for _ in range(3):  # <bundle>.app/Contents/MacOS/<exe>
        bundle = os.path.dirname(bundle)
    if not bundle.endswith(".app"):
        return False, "not running from an .app bundle (%s)" % exe
    try:
        subprocess.Popen(["/usr/bin/open", "-a", bundle] + list(paths),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        return False, "open -a %s: %s" % (os.path.basename(bundle), e)
    return True, ""


def _load_in_orca(paths):
    """Hand local files to the running OrcaSlicer so they land in Prepare.

    The plugin host API is read-only, but every instance listens for the message
    a second launch would send it (InstanceCheck.cpp); file paths there reach
    EVT_LOAD_MODEL_OTHER_INSTANCE, i.e. the plater. Each OS has its own
    transport, and only the Linux one can be exercised here.
    """
    if os.name == "nt":
        return _handoff_windows(paths)
    if sys.platform == "darwin":
        return _handoff_macos(paths)
    return _load_in_orca_dbus(paths)


def _load_in_orca_dbus(paths):
    """Linux transport: the session-bus message a second launch would send.

    The interface, object and method all carry the instance hash, which is
    discovered from ListNames so nothing needs configuring.
    """
    try:
        names = subprocess.run(
            ["dbus-send", "--session", "--print-reply", "--dest=org.freedesktop.DBus",
             "/org/freedesktop/DBus", "org.freedesktop.DBus.ListNames"],
            capture_output=True, text=True, timeout=15).stdout
    except Exception as e:
        return False, f"dbus-send unavailable: {e}"

    found = re.findall(r"com\.orcaslicer\.OrcaSlicer\.InstanceCheck\.Object(\d+)", names)
    if not found:
        return False, "no running instance on the session bus"

    # found[0] is whichever Orca answered first, which is not necessarily the one
    # hosting this plugin: run OrcaCAD next to a belt build, or leave a second
    # instance open, and the model silently lands on the *other* window's plate.
    # The lock files under our own datadir say which hash is ours.
    own = _own_instance_hashes()
    instance = next((h for h in own if h in found), None)
    if instance is None:
        if len(found) > 1:
            return False, ("%d OrcaSlicer instances are on the session bus and none "
                           "matches this data directory; refusing to guess which one "
                           "should receive the model" % len(found))
        instance = found[0]
    payload = _instance_payload(paths)
    iface = "com.orcaslicer.OrcaSlicer.InstanceCheck.Object" + instance
    try:
        p = subprocess.run(
            ["dbus-send", "--session", "--type=method_call", "--dest=" + iface,
             "/com/orcaslicer/OrcaSlicer/InstanceCheck/Object" + instance,
             iface + ".AnotherInstance", "string:" + payload],
            capture_output=True, text=True, timeout=15)
    except Exception as e:
        return False, str(e)
    if p.returncode != 0:
        return False, (p.stderr or "").strip()[:200]
    return True, ""


def _parse_license(name, url=""):
    name = (name or "").strip()
    summary = LICENSE_DESCRIPTIONS.get(name, "")
    if not summary:
        upper = name.upper()
        # Printables spells them out ("Creative Commons - Attribution - Noncommercial"),
        # so matching only the "CC" abbreviation left every Printables result with no
        # summary at all — the panel said "No license information available" next to a
        # perfectly well-known licence.
        if "CC" in upper or "CREATIVE COMMONS" in upper:
            clauses = []
            if "ATTRIBUTION" in upper or "BY" in upper.split():
                clauses.append("credit the author")
            if "NONCOMMERCIAL" in upper or "NON-COMMERCIAL" in upper or "NC" in upper.split():
                clauses.append("non-commercial use only")
            if "NO DERIVATIVES" in upper or "NODERIV" in upper or "ND" in upper.split():
                clauses.append("no modifications")
            if "SHARE ALIKE" in upper or "SHAREALIKE" in upper or "SA" in upper.split():
                clauses.append("remixes under the same licence")
            if "PUBLIC DOMAIN" in upper or "CC0" in upper:
                summary = "Public domain. No rights reserved. Free for any use."
            elif clauses:
                summary = "Creative Commons: " + ", ".join(clauses) + "."
            else:
                summary = "Creative Commons license. See full text for terms."
        elif "GPL" in upper:
            summary = "GNU General Public License. Share modifications."
    return {"name": name, "url": url, "summary": summary}


def _first_image(images):
    if not images:
        return ""
    if isinstance(images, list) and images:
        img = images[0]
        if isinstance(img, dict):
            return img.get("filePath") or img.get("url") or ""
        return str(img)
    if isinstance(images, dict):
        return images.get("filePath") or images.get("url") or ""
    return str(images)


# ---------------------------------------------------------------------------
# Search adapters
# ---------------------------------------------------------------------------

MAKERONLINE_LICENSES = {
    1: ("CC BY", "https://creativecommons.org/licenses/by/4.0/"),
    2: ("CC BY-SA", "https://creativecommons.org/licenses/by-sa/4.0/"),
    3: ("CC BY-NC", "https://creativecommons.org/licenses/by-nc/4.0/"),
    4: ("CC BY-NC-SA", "https://creativecommons.org/licenses/by-nc-sa/4.0/"),
    5: ("CC BY-ND", "https://creativecommons.org/licenses/by-nd/4.0/"),
    6: ("CC BY-NC-ND", "https://creativecommons.org/licenses/by-nc-nd/4.0/"),
    7: ("CC0", "https://creativecommons.org/publicdomain/zero/1.0/"),
    8: ("Standard Digital File License", ""),
}

MAKERONLINE_BASE = "https://www.makeronline.com"


class MakeronlineSearcher:
    # Matches the "platform" field of this adapter's results, so _do_search can
    # check it against _FILE_RESOLVERS before offering the platform at all.
    PLATFORM = "Makeronline"

    SEARCH_URL = f"{MAKERONLINE_BASE}/api/search/model"

    @staticmethod
    def enabled(tokens):
        return True

    @staticmethod
    def search(query, tokens):
        import requests

        r = requests.post(
            MakeronlineSearcher.SEARCH_URL,
            json={
                "keyword": query,
                "page": 1,
                "page_size": 30,
                "order": "",
                "search": 1,
                "weight": 1,
                "print_type": 0,
                "category_id": "",
                "color": "",
                "license": "",
                "award": "",
                "exclusive": "",
                "is_free": "",
            },
            headers={"Content-Type": "application/json", "User-Agent": "OrcaSlicer/2.0"},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        if data.get("code") != 0:
            return []
        results = []
        for item in data.get("data", {}).get("data") or []:
            lic_name, lic_url = MAKERONLINE_LICENSES.get(
                item.get("license", 0), ("Unknown", "")
            )
            lic_data = _parse_license(lic_name, lic_url)
            thumb = (item.get("mold_image") or "").replace("thumbnail", "400x300")
            results.append({
                "name": item.get("title", "Untitled"),
                "author": item.get("show_user_name") or item.get("user_name", "Unknown"),
                "platform": "Makeronline",
                "thumbnail_url": thumb,
                "license": lic_data["name"],
                "license_url": lic_data["url"],
                "license_summary": lic_data["summary"],
                "download_url": item.get("target_url", ""),
                "url": item.get("target_url", ""),
                "_mold_id": item.get("mold_id"),
            })
        return results

    @staticmethod
    def get_detail(mold_id):
        import requests

        r = requests.get(
            f"{MAKERONLINE_BASE}/api/mold/detail",
            params={"id": mold_id},
            headers={"User-Agent": "OrcaSlicer/2.0"},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        if data.get("code") != 0:
            return None
        detail = data["data"]
        files = detail.get("files") or []
        file_url = files[0].get("url") if files else ""
        return {
            "file_url": file_url,
            "file_name": files[0].get("file_name", "") if files else "",
            "images": [img["url"] for img in (detail.get("images") or [])],
        }


    # Makeronline runs no login of its own: the site signs in through Anycubic's
    # Casdoor SSO, whose settings it publishes in its own bundle (Auth_CLIENT_ID,
    # Auth_SIGN_UP_URL=/signup/ac_maker_online). That application answers
    # enableCodeSignin:false, so unlike Bambu there is no e-mail code to wait
    # for: it really is e-mail + password. Both are typed in the plugin's own
    # window, posted straight to Anycubic, and neither is kept - only the site
    # token that comes back is stored.
    CAS_LOGIN_URL = "https://cas.anycubic.com/api/login"
    CAS_CLIENT_ID = "69ce24b6eaf78e597ac0"
    CAS_APPLICATION = "ac_maker_online"
    CAS_ORGANIZATION = "anycubic"
    # The site exchanges the code with a GET on /api/login, not with the
    # api/v2/auth/login its bundle also defines: that one answers "Server
    # internal error" exactly as unknown routes do. This one answers
    # invalid_grant / invalid_request, i.e. it really runs the exchange.
    EXCHANGE_URL = f"{MAKERONLINE_BASE}/api/login"
    DOWNLOAD_URL = f"{MAKERONLINE_BASE}/api/file/download"

    @staticmethod
    def _headers():
        return {"Content-Type": "application/json", "User-Agent": "OrcaSlicer/2.0"}

    @staticmethod
    def sign_in(email, password):
        """Anycubic account -> OAuth code -> Makeronline token, in two calls.

        Nothing here opens a browser: Casdoor's /api/login takes the credentials
        directly and hands back the authorisation code the redirect would
        otherwise carry in the URL.
        """
        import requests

        r = requests.post(
            MakeronlineSearcher.CAS_LOGIN_URL,
            params={"clientId": MakeronlineSearcher.CAS_CLIENT_ID,
                    "responseType": "code",
                    "redirectUri": MAKERONLINE_BASE,
                    "scope": "read",
                    "state": MakeronlineSearcher.CAS_APPLICATION},
            # These are the fields the site's own sign-in sends, observed on the
            # wire: no autoSignin, no signinMethod. It also sends a 32-char
            # riskToken from its bot-risk SDK, which cannot be produced outside
            # the page; omitting it is accepted at least as far as the account
            # lookup, and a refusal here surfaces Casdoor's own wording.
            json={"application": MakeronlineSearcher.CAS_APPLICATION,
                  "organization": MakeronlineSearcher.CAS_ORGANIZATION,
                  "username": email, "password": password,
                  "remember": True, "isRecMarEmail": False,
                  "type": "code"},
            headers=MakeronlineSearcher._headers(), timeout=30)
        if r.status_code != 200:
            raise RuntimeError(_api_error(r, "Anycubic would not accept the sign-in"))
        body = r.json() or {}
        # Casdoor says so in its own words ("The user ... doesn't exist",
        # "password is incorrect"), which beats any wording invented here.
        if (body.get("status") or "") != "ok":
            raise RuntimeError(body.get("msg") or "Anycubic rejected the sign-in.")
        data = body.get("data")
        code = data if isinstance(data, str) else (data or {}).get("code") or ""
        if not code:
            raise RuntimeError("Anycubic signed in but returned no authorisation "
                               "code, so Makeronline cannot be reached.")
        return MakeronlineSearcher._exchange(code)

    @staticmethod
    def _exchange(code):
        """Trade the Casdoor code for the token Makeronline's own API wants."""
        import requests

        r = requests.get(MakeronlineSearcher.EXCHANGE_URL,
                         params={"code": code,
                                 "state": MakeronlineSearcher.CAS_APPLICATION},
                         headers={"User-Agent": "OrcaSlicer/2.0"}, timeout=30)
        if r.status_code != 200:
            raise RuntimeError(_api_error(r, "Makeronline would not accept the sign-in"))
        body = r.json() or {}
        if body.get("code") != 0:
            # Its words are OAuth's, so say what they mean here.
            msg = body.get("message") or ""
            if msg == "invalid_grant":
                msg = ("Anycubic's sign-in code was refused \u2014 it is used once "
                       "and expires in moments. Try signing in again.")
            raise RuntimeError(msg or "Makeronline refused the Anycubic sign-in.")
        token = (body.get("data") or {}).get("access_token") or ""
        if not token:
            raise RuntimeError("Makeronline accepted the sign-in but returned no "
                               "token, so downloads would still be refused.")
        return token

    @staticmethod
    def get_files(model_url, tokens=None):
        """The model's own files. Anonymous: only the download needs the token.

        The URLs are not minted here - /api/file/download issues a signed S3 link
        per file, and listing a model with a dozen parts would spend a dozen
        authenticated calls on files the user may not pick.
        """
        import requests

        token = ((tokens or {}).get("makeronline_token") or "").strip()
        if not token:
            raise RuntimeError(
                "Makeronline serves files only to an account. "
                + _TOKEN_HELP["Makeronline"]
                + ' Use the Accounts button in the plugin window, or set '
                + '"makeronline_token" in Plugins > 3D Model Search > Config.')
        m = re.search(r"/(\d+)\.html", model_url or "")
        if not m:
            return []
        mold_id = m.group(1)
        r = requests.get(f"{MAKERONLINE_BASE}/api/mold/detail",
                         params={"id": mold_id},
                         headers={"User-Agent": "OrcaSlicer/2.0"}, timeout=30)
        r.raise_for_status()
        body = r.json() or {}
        if body.get("code") != 0:
            raise RuntimeError(body.get("message") or "Makeronline returned no detail.")
        files = []
        for f in (body.get("data") or {}).get("files") or []:
            key = f.get("key")
            if not key:
                continue
            files.append({
                "name": f.get("file_name") or ("file-%s" % f.get("file_id", "")),
                "url": "",
                "_key": key,
                "_mold_id": mold_id,
            })
        return files

    @staticmethod
    def mint_url(entry, tokens=None):
        """Turn one picked file into a signed S3 URL, valid for minutes."""
        import requests

        token = ((tokens or {}).get("makeronline_token") or "").strip()
        if not token:
            raise RuntimeError("Makeronline needs your Anycubic account token.")
        headers = dict(MakeronlineSearcher._headers(), Authorization=token)
        r = requests.post(
            MakeronlineSearcher.DOWNLOAD_URL,
            json={"key": entry["_key"], "file_type": 1,
                  "mold_id": int(entry["_mold_id"]), "is_batch_down": 0},
            headers=headers, timeout=30)
        r.raise_for_status()
        body = r.json() or {}
        if body.get("code") == 1003:
            # The site answers 1003 when its risk check wants a captcha, which
            # only its own page can show. Say so plainly instead of failing blank.
            raise RuntimeError("Makeronline is asking for a captcha for this "
                               "download, which the plugin window cannot show. "
                               "Try again later.")
        if body.get("code") != 0:
            raise RuntimeError(body.get("message") or
                               "Makeronline refused the download.")
        link = body.get("data")
        if not isinstance(link, str) or not link:
            raise RuntimeError("Makeronline returned no download URL for %s."
                               % entry.get("name", "this file"))
        return link


NEXPRINT_LICENSES = {
    0: ("CC0", "https://creativecommons.org/publicdomain/zero/1.0/"),
    1: ("CC BY", "https://creativecommons.org/licenses/by/4.0/"),
    2: ("CC BY-SA", "https://creativecommons.org/licenses/by-sa/4.0/"),
    3: ("CC BY-NC", "https://creativecommons.org/licenses/by-nc/4.0/"),
    4: ("CC BY-NC-SA", "https://creativecommons.org/licenses/by-nc-sa/4.0/"),
    5: ("CC BY-ND", "https://creativecommons.org/licenses/by-nd/4.0/"),
    6: ("CC BY-NC-ND", "https://creativecommons.org/licenses/by-nc-nd/4.0/"),
    7: ("All Rights Reserved", ""),
    8: ("Standard Digital File License", ""),
}

NEXPRINT_BASE = "https://www.nexprint.com"


class NexprintSearcher:
    # Matches the "platform" field of this adapter's results, so _do_search can
    # check it against _FILE_RESOLVERS before offering the platform at all.
    PLATFORM = "Nexprint"

    SEARCH_URL = f"{NEXPRINT_BASE}/gateway/api/v1/model-library-server/model-base-info/search"

    @staticmethod
    def enabled(tokens):
        return True

    @staticmethod
    def search(query, tokens):
        import requests

        r = requests.get(
            NexprintSearcher.SEARCH_URL,
            params={"keyword": query, "pageNo": "1", "pageSize": "30"},
            headers={"User-Agent": "OrcaSlicer/2.0"},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        if data.get("code") != 0:
            return []
        results = []
        for item in (data.get("data", {}).get("pageResult", {}).get("list") or []):
            lic_id = item.get("licenseType", 0)
            lic_name, lic_url = NEXPRINT_LICENSES.get(lic_id, ("Unknown", ""))
            lic_data = _parse_license(lic_name, lic_url)
            results.append({
                "name": item.get("modelName", "Untitled"),
                "author": item.get("authorName")
                or ((item.get("author") or {}).get("nickname", "Unknown")),
                "platform": "Nexprint",
                "thumbnail_url": item.get("coverImgUrl", ""),
                "license": lic_data["name"],
                "license_url": lic_data["url"],
                "license_summary": lic_data["summary"],
                "download_url": f"{NEXPRINT_BASE}/models/{item.get('modelId', '')}",
                "url": f"{NEXPRINT_BASE}/models/{item.get('modelId', '')}",
                "_model_id": item.get("modelId"),
            })
        return results

    @staticmethod
    def get_detail(model_id):
        import requests

        r = requests.get(
            f"{NEXPRINT_BASE}/gateway/api/v1/model-library-server/model-base-info/get",
            params={"id": model_id},
            headers={"User-Agent": "OrcaSlicer/2.0"},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        if data.get("code") != 0:
            return None
        detail = data["data"]
        files = detail.get("modelFileInfoList") or []
        file_url = files[0].get("fileUrl") if files else ""
        return {
            "file_url": file_url,
            "file_name": files[0].get("fileName", "") if files else "",
            "images": [p.get("fileUrl") for p in (detail.get("modelPicList") or [])],
        }


class PrintablesSearcher:
    # Matches the "platform" field of this adapter's results, so _do_search can
    # check it against _FILE_RESOLVERS before offering the platform at all.
    PLATFORM = "Printables"

    # printables.com/search/models is behind a Cloudflare JS challenge — it answers
    # 403 "Just a moment..." to any client that cannot run the challenge script, no
    # matter the User-Agent. So ask the API the same thing the site's own frontend
    # asks: searchPrints2, which needs no authentication. LicenseType carries only
    # a name, no url, which the mapping below already tolerates.
    SEARCH_QUERY = (
        "query Search($query: String!, $limit: Int) {"
        " searchPrints2(query: $query, limit: $limit) {"
        " items { id name slug image { filePath } license { name }"
        " user { publicUsername } } } }"
    )

    @staticmethod
    def enabled(tokens):
        return True

    @staticmethod
    def search(query, tokens):
        import requests

        r = requests.post(
            PrintablesSearcher.GRAPHQL_URL,
            json={"query": PrintablesSearcher.SEARCH_QUERY,
                  "variables": {"query": query, "limit": 30}},
            headers={"Content-Type": "application/json", "User-Agent": _BROWSER_UA},
            timeout=30,
        )
        r.raise_for_status()
        payload = r.json()
        if payload.get("errors"):
            raise RuntimeError(payload["errors"][0].get("message", "GraphQL error"))
        items = ((payload.get("data") or {}).get("searchPrints2") or {}).get("items") or []
        results = []
        for item in items:
            img = item.get("image") or {}
            file_path = img.get("filePath", "")
            thumb = f"https://media.printables.com/{file_path}" if file_path else ""
            lic = item.get("license") or {}
            lic_data = _parse_license(
                lic.get("name", "") if isinstance(lic, dict) else "",
                lic.get("url", "") if isinstance(lic, dict) else "",
            )
            if not lic_data["name"]:
                lic_data = {
                    "name": "Unknown",
                    "url": "",
                    "summary": "Check on Printables.com for license details.",
                }
            user = item.get("user") or {}
            results.append({
                "name": item.get("name", "Untitled"),
                "author": user.get("publicUsername") or user.get("handle", "Unknown"),
                "platform": "Printables",
                "thumbnail_url": thumb,
                "license": lic_data["name"],
                "license_url": lic_data["url"],
                "license_summary": lic_data["summary"],
                "download_url": f"https://www.printables.com/model/{item.get('id','')}-{item.get('slug','')}",
                "url": f"https://www.printables.com/model/{item.get('id','')}-{item.get('slug','')}",
            })
        return results

    GRAPHQL_URL = "https://api.printables.com/graphql/"

    # The site's own download call. It needs no authentication and returns the
    # real file URL, which replaces the old trick of deriving the STL path from
    # the preview image's folder: that only held for prints stored the old way
    # (media/prints/<id>/stls/<n>_<uuid>/<name>_preview.png). Newer prints keep
    # previews in their own subfolder (media/prints/<uuid>/previews/<sha>.png),
    # where the derived path 404s and Import failed with nothing on the plate.
    DOWNLOAD_MUTATION = (
        "mutation Link($id: ID!, $printId: ID!) {"
        " getDownloadLink(id: $id, printId: $printId, fileType: stl,"
        " source: model_detail) { ok output { link } } }"
    )

    @staticmethod
    def get_files(model_url, tokens=None):
        """Public STL URLs for a print, one getDownloadLink call per file.

        Takes `tokens` only to match the resolver signature; Printables needs none.
        """
        import requests

        m = re.search(r"/model/(\d+)", model_url or "")
        if not m:
            return []
        print_id = m.group(1)
        headers = {"Content-Type": "application/json", "User-Agent": _BROWSER_UA}
        r = requests.post(
            PrintablesSearcher.GRAPHQL_URL,
            json={"query": "{print(id:%s){stls{id name}}}" % print_id},
            headers=headers, timeout=30,
        )
        r.raise_for_status()
        stls = ((r.json().get("data") or {}).get("print") or {}).get("stls") or []
        files = []
        for stl in stls:
            stl_id, name = stl.get("id"), stl.get("name") or ""
            if not stl_id or not name:
                continue
            lr = requests.post(
                PrintablesSearcher.GRAPHQL_URL,
                json={"query": PrintablesSearcher.DOWNLOAD_MUTATION,
                      "variables": {"id": str(stl_id), "printId": print_id}},
                headers=headers, timeout=30,
            )
            lr.raise_for_status()
            payload = lr.json()
            if payload.get("errors"):
                raise RuntimeError(payload["errors"][0].get("message", "GraphQL error"))
            got = ((payload.get("data") or {}).get("getDownloadLink") or {})
            link = (got.get("output") or {}).get("link")
            if got.get("ok") and link:
                files.append({"name": name, "url": link})
        return files


class ThingiverseSearcher:
    # Matches the "platform" field of this adapter's results, so _do_search can
    # check it against _FILE_RESOLVERS before offering the platform at all.
    PLATFORM = "Thingiverse"

    BASE = "https://api.thingiverse.com"

    @staticmethod
    def enabled(tokens):
        return False

    @staticmethod
    def search(query, tokens):
        import requests

        url = f"{ThingiverseSearcher.BASE}/search/{urllib.parse.quote(query)}"
        params = {
            "type": "things",
            "per_page": 20,
            "access_token": tokens["thingiverse_token"],
        }
        r = requests.get(url, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        results = []
        for hit in data.get("hits") or []:
            lic = _parse_license(hit.get("license", ""))
            results.append({
                "name": hit.get("name", "Untitled"),
                "author": (hit.get("creator") or {}).get("name", "Unknown"),
                "platform": "Thingiverse",
                "thumbnail_url": hit.get("thumbnail", ""),
                "license": lic["name"] or "Unknown",
                "license_url": lic["url"],
                "license_summary": lic["summary"],
                "download_url": (hit.get("public_url") or "") + "/zip",
                "url": hit.get("public_url", ""),
            })
        return results


# Telling the user to "paste your Bambu Cloud token" without saying where to find
# one is what made this platform unusable in practice: no MakerWorld screen shows
# a token. The bearer is minted by POST /v1/user-service/user/login (accessToken,
# top level) and the site attaches it to every download, so reading it off one of
# those requests is instruction-proof — it survives any change to where the site
# keeps the session.
def _api_error(resp, fallback):
    """The server's own words when it has any, so the user sees the real reason."""
    try:
        body = resp.json() or {}
        msg = body.get("error") or body.get("message") or ""
    except Exception:
        msg = (resp.text or "").strip()[:200]
    return msg or ("%s (HTTP %d)" % (fallback, resp.status_code))


_TOKEN_HELP = {
    "MakerWorld":
        "Sign in with your Bambu Lab account. Enter your email, press Send code, "
        "and type the six digits Bambu emails you. No password is asked for or "
        "stored \u2014 the code login does not use one.",
    "Makeronline":
        "Sign in with your Anycubic account \u2014 the same e-mail and password "
        "you use on makeronline.com. Anycubic offers no e-mail code, so the "
        "password is asked for here; it goes straight to Anycubic and is never "
        "stored. Only the token it returns is kept.",
}


class MakerWorldSearcher:
    # Matches the "platform" field of this adapter's results, so _do_search can
    # check it against _FILE_RESOLVERS before offering the platform at all.
    PLATFORM = "MakerWorld"

    SEARCH_URL = "https://api.bambulab.com/v1/search-service/select/design2"
    BASE = "https://makerworld.com"
    DESIGN_URL = "https://api.bambulab.com/v1/design-service/design/%s"
    INSTANCES_URL = "https://api.bambulab.com/v1/design-service/design/%s/instances"
    # The download the site performs for itself, on the api.bambulab.com backend
    # rather than on Cloudflare-protected makerworld.com. It takes the same
    # bearer the user already signs in with and mints the signed CDN URL the
    # browser would otherwise obtain from its session cookies. The old
    # /instance/<id>/f3mf shape is dead, which is why this platform looked like
    # a hard login wall when it was really the wrong endpoint.
    PROFILE_DOWNLOAD_URL = "https://api.bambulab.com/v1/iot-service/api/user/profile/%s"

    @staticmethod
    def enabled(tokens):
        return True

    LICENSES = {
        "CC0": ("CC0", "https://creativecommons.org/publicdomain/zero/1.0/"),
        "BY": ("CC BY", "https://creativecommons.org/licenses/by/4.0/"),
        "BY-SA": ("CC BY-SA", "https://creativecommons.org/licenses/by-sa/4.0/"),
        "BY-NC": ("CC BY-NC", "https://creativecommons.org/licenses/by-nc/4.0/"),
        "BY-NC-SA": ("CC BY-NC-SA", "https://creativecommons.org/licenses/by-nc-sa/4.0/"),
        "BY-ND": ("CC BY-ND", "https://creativecommons.org/licenses/by-nd/4.0/"),
        "BY-NC-ND": ("CC BY-NC-ND", "https://creativecommons.org/licenses/by-nc-nd/4.0/"),
        "Standard Digital File License": ("Standard Digital File License", ""),
        "MakerWorld Exclusive License": ("MakerWorld Exclusive License", ""),
    }

    @staticmethod
    def search(query, tokens):
        import requests

        r = requests.get(
            MakerWorldSearcher.SEARCH_URL,
            params={"keyword": query, "limit": "30"},
            headers={"User-Agent": "OrcaSlicer/2.0"},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        results = []
        for hit in data.get("hits") or []:
            lic_str = hit.get("license", "Unknown")
            lic_name, lic_url = MakerWorldSearcher.LICENSES.get(lic_str, (lic_str, ""))
            lic_data = _parse_license(lic_name, lic_url)
            creator = hit.get("designCreator") or {}
            results.append({
                "name": hit.get("title", "Untitled"),
                "author": creator.get("name", "Unknown"),
                "platform": "MakerWorld",
                "thumbnail_url": hit.get("cover", ""),
                "license": lic_data["name"],
                "license_url": lic_data["url"],
                "license_summary": lic_data["summary"],
                "download_url": f"{MakerWorldSearcher.BASE}/en/models/{hit.get('id', '')}",
                "url": f"{MakerWorldSearcher.BASE}/en/models/{hit.get('id', '')}",
                "_model_id": hit.get("id"),
            })
        return results

    @staticmethod
    def get_detail(model_id):
        import requests

        r = requests.get(
            f"https://api.bambulab.com/v1/design-service/design/{model_id}",
            headers={"User-Agent": "OrcaSlicer/2.0"},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        ext = data.get("designExtension") or {}
        files = ext.get("model_files") or []
        pics = ext.get("design_pictures") or []
        return {
            "file_url": files[0].get("modelUrl") if files else "",
            "file_name": files[0].get("modelName", "") if files else "",
            "file_size": files[0].get("modelSize", 0) if files else 0,
            "file_type": files[0].get("modelType", "") if files else "",
            "images": [p.get("url", "") for p in pics],
        }

    @staticmethod
    def _headers():
        return {"User-Agent": _BROWSER_UA, "Referer": MakerWorldSearcher.BASE + "/"}

    # Bambu's own passwordless sign-in: ask for a six-digit code by email, then
    # exchange it for the bearer. Measured 2026-10-18 against the live API:
    # sendemail/code REQUIRES type="codeLogin" (without it the reply is "This
    # account is not registered"; a wrong type is refused outright), and login
    # with {account, code} is accepted on the code path. Deliberately NOT the
    # password flow: the plugin never asks for, sees or stores a password.
    SEND_CODE_URL = "https://api.bambulab.com/v1/user-service/user/sendemail/code"
    LOGIN_URL = "https://api.bambulab.com/v1/user-service/user/login"

    @staticmethod
    def send_code(email):
        import requests

        r = requests.post(MakerWorldSearcher.SEND_CODE_URL,
                          json={"email": email, "type": "codeLogin"},
                          headers=MakerWorldSearcher._headers(), timeout=30)
        if r.status_code != 200:
            raise RuntimeError(_api_error(r, "Bambu would not send a code"))
        # 200 for an unknown address too, by design: the API does not reveal
        # whether an account exists, and neither does this message.
        return "Bambu has emailed a six-digit code to %s." % email

    @staticmethod
    def login_with_code(email, code):
        import requests

        r = requests.post(MakerWorldSearcher.LOGIN_URL,
                          json={"account": email, "code": code},
                          headers=MakerWorldSearcher._headers(), timeout=30)
        if r.status_code != 200:
            raise RuntimeError(_api_error(r, "Bambu rejected the sign-in"))
        data = r.json() or {}
        token = data.get("accessToken") or ""
        if not token:
            # A login that succeeds without a token means Bambu wants another
            # factor; say which one rather than reporting a blank success.
            raise RuntimeError(
                "Bambu needs a further step (%s) that this window cannot do."
                % (data.get("loginType") or "unknown"))
        return token

    @staticmethod
    def get_files(model_url, tokens=None):
        """The design's print profiles, one per plate, with no URL yet.

        Both calls here are anonymous; the token is checked up front only so a
        user without one is told what to do before two pointless requests.

        The URLs are deliberately NOT minted here. A popular design carries
        dozens of profiles (Benchy Bambu PLA Basic: 91, measured 2026-09-20) and
        a signed URL lives about five minutes, so minting at list time would fire
        one authenticated call per profile and most would expire before the user
        finished choosing. `mint_url` runs for the picked entries only.
        """
        import requests

        token = ((tokens or {}).get("makerworld_token") or "").strip()
        if not token:
            raise RuntimeError(
                "MakerWorld serves files only to an account. "
                + _TOKEN_HELP["MakerWorld"]
                + ' Use the Accounts button in the plugin window, or set '
                + '"makerworld_token" in Plugins > 3D Model Search > Config.')
        m = re.search(r"/models/(\d+)", model_url or "")
        if not m:
            return []
        design_id = m.group(1)
        headers = MakerWorldSearcher._headers()

        # model_id is MakerWorld's alphanumeric id (e.g. US2262720d55a8f6), NOT
        # the integer designId in the /models/<N> URL: the download rejects the
        # latter.
        d = requests.get(MakerWorldSearcher.DESIGN_URL % design_id,
                         headers=headers, timeout=30)
        d.raise_for_status()
        model_id = (d.json() or {}).get("modelId") or ""

        i = requests.get(MakerWorldSearcher.INSTANCES_URL % design_id,
                         headers=headers, timeout=30)
        i.raise_for_status()

        files = []
        for hit in (i.json() or {}).get("hits") or []:
            profile_id = hit.get("profileId")
            if not profile_id:
                continue
            files.append({
                "name": (hit.get("title") or ("profile-%s" % profile_id)) + ".3mf",
                "url": "",
                "_profile_id": profile_id,
                "_model_id": model_id,
            })
        return files

    @staticmethod
    def mint_url(entry, tokens=None):
        """Turn one picked profile into a signed CDN URL, valid for minutes."""
        import requests

        token = ((tokens or {}).get("makerworld_token") or "").strip()
        if not token:
            raise RuntimeError("MakerWorld needs your Bambu Cloud token.")
        headers = dict(MakerWorldSearcher._headers(), Authorization="Bearer " + token)
        r = requests.get(
            MakerWorldSearcher.PROFILE_DOWNLOAD_URL % entry["_profile_id"],
            params={"model_id": entry.get("_model_id", "")},
            headers=headers, timeout=30)
        if r.status_code == 401:
            raise RuntimeError("Bambu Cloud rejected the token. Sign in again and "
                               "paste a fresh one into the plugin's config.")
        r.raise_for_status()
        link = (r.json() or {}).get("url")
        if not link:
            raise RuntimeError("MakerWorld returned no download URL for %s."
                               % entry.get("name", "this profile"))
        return link


class GrabcadSearcher:
    # Matches the "platform" field of this adapter's results, so _do_search can
    # check it against _FILE_RESOLVERS before offering the platform at all.
    PLATFORM = "GrabCAD"

    BASE = "https://api.grabcad.com/api/v1"

    @staticmethod
    def enabled(tokens):
        return False

    @staticmethod
    def search(query, tokens):
        import requests

        url = f"{GrabcadSearcher.BASE}/search"
        params = {"query": query}
        headers = {}
        token = tokens.get("grabcad_token", "")
        if token:
            headers["Authorization"] = "Bearer " + token
        try:
            r = requests.get(url, params=params, headers=headers, timeout=30)
            r.raise_for_status()
            data = r.json()
        except Exception:
            return []
        results = []
        for item in data.get("results") or data.get("items") or []:
            proj = item.get("project") or item.get("model") or {}
            thumb = proj.get("thumbnail") or proj.get("image", {}).get("url", "") or ""
            lic = _parse_license(proj.get("license", ""))
            results.append({
                "name": proj.get("name") or item.get("name", "Untitled"),
                "author": (proj.get("user") or {}).get("name")
                or (proj.get("author") or {}).get("name", "Unknown"),
                "platform": "GrabCAD",
                "thumbnail_url": thumb,
                "license": lic["name"] or "Unknown",
                "license_url": lic["url"],
                "license_summary": lic["summary"],
                "download_url": proj.get("downloadUrl")
                or f"https://grabcad.com/library/{proj.get('slug','')}",
                "url": f"https://grabcad.com/library/{proj.get('slug','')}",
            })
        return results


_SEARCHERS = {
    "printables": PrintablesSearcher,
    "nexprint": NexprintSearcher,
    "makeronline": MakeronlineSearcher,
    "thingiverse": ThingiverseSearcher,
    "makerworld": MakerWorldSearcher,
    "grabcad": GrabcadSearcher,
}

# Keyed on the "platform" field of a result. Printables serves files to anyone;
# MakerWorld serves them to an account. Nexprint (401) and Makeronline (403)
# still have no route the plugin can take.
_FILE_RESOLVERS = {
    "Printables": PrintablesSearcher.get_files,
    "MakerWorld": MakerWorldSearcher.get_files,
    "Makeronline": MakeronlineSearcher.get_files,
}

# Platforms that serve files only to an account, and the config key holding the
# user's own token for each. Without the token the platform stays searchable but
# is not offered for import, so the guard rail holds: everything the panel says
# it can import, it can import.
_TOKEN_KEYS = {"MakerWorld": "makerworld_token",
               "Makeronline": "makeronline_token"}

# Platforms whose file list carries no URL, because minting one costs an
# authenticated call that expires in minutes. Called once per picked file, at
# download time. A platform absent here has usable URLs already.
_URL_MINTERS = {"MakerWorld": MakerWorldSearcher.mint_url,
                "Makeronline": MakeronlineSearcher.mint_url}

# Platforms that can be signed into from inside the plugin window:
# (request a code, exchange it for a token). A None first element means the
# portal has no e-mail code at all, so the second call takes the password
# instead - Anycubic's SSO answers enableCodeSignin:false, and pretending
# otherwise would leave the user waiting for a mail that never arrives.
# A platform absent here can still be connected by pasting a token.
_AUTH = {"MakerWorld": (MakerWorldSearcher.send_code,
                        MakerWorldSearcher.login_with_code),
         "Makeronline": (None, MakeronlineSearcher.sign_in)}


def _accounts(tokens):
    """One row per platform the panel searches, for the Accounts view.

    Built from the same tables the import path consults, so the setup screen
    cannot drift from what is actually importable.
    """
    rows = []
    for adapter in _SEARCHERS.values():
        name = getattr(adapter, "PLATFORM", "")
        if not name:
            continue
        key = _TOKEN_KEYS.get(name)
        if key:
            state = "connected" if _importable(name, tokens) else "needs_token"
        elif name in _FILE_RESOLVERS:
            state = "open"          # no account needed at all
        else:
            state = "unavailable"   # nothing the plugin can fetch, with or without one
        rows.append({"platform": name, "state": state, "key": key or "",
                     "help": _TOKEN_HELP.get(name, ""),
                     "can_sign_in": name in _AUTH,
                     "sign_in_mode": ("code" if (_AUTH.get(name) or (None,))[0]
                                      else "password") if name in _AUTH else ""})
    rows.sort(key=lambda r: ({"needs_token": 0, "connected": 1, "open": 2,
                              "unavailable": 3}[r["state"]], r["platform"]))
    return rows


def _importable(platform, tokens):
    """True when this platform's files can actually be fetched right now."""
    if platform not in _FILE_RESOLVERS:
        return False
    key = _TOKEN_KEYS.get(platform)
    return not key or bool(((tokens or {}).get(key) or "").strip())


# ---------------------------------------------------------------------------
# Plugin capability
# ---------------------------------------------------------------------------

PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>3D Model Search</title></head>
<body>
<style>
  * { box-sizing:border-box; margin:0; padding:0; }
  body { font-family:var(--orca-font,sans-serif); background:var(--orca-bg,#1e1e1e); color:var(--orca-fg,#eee); padding:16px; }
  input,select,button { font-family:inherit; }
  .search-row { display:flex; gap:8px; margin-bottom:12px; }
  .search-row input { flex:1; padding:8px 12px; border:1px solid var(--orca-border,#444); border-radius:6px; background:var(--orca-bg,#1e1e1e); color:var(--orca-fg,#eee); font-size:0.95em; }
  .search-row button { padding:8px 20px; border:none; border-radius:6px; background:var(--orca-accent,#4a9eff); color:var(--orca-accent-fg,#fff); cursor:pointer; font-size:0.95em; }
  .platforms { display:flex; gap:16px; margin-bottom:12px; flex-wrap:wrap; }
  .platforms label { display:flex; align-items:center; gap:6px; color:var(--orca-muted,#aaa); font-size:0.85em; cursor:pointer; }
  #results { display:grid; grid-template-columns:repeat(auto-fill,minmax(180px,1fr)); gap:10px; }
  .card { border:1px solid var(--orca-border,#444); border-radius:8px; padding:10px; cursor:pointer; transition:border-color .15s; }
  .card:hover { border-color:var(--orca-accent,#4a9eff); }
  .card img { width:100%; height:110px; object-fit:cover; border-radius:4px; margin-bottom:6px; background:var(--orca-border,#333); }
  .card h3 { font-size:0.9em; margin-bottom:2px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .card .author { font-size:0.78em; color:var(--orca-muted,#888); }
  .license-badge { display:inline-block; padding:1px 7px; border-radius:3px; font-size:0.72em; margin-top:4px; }
  .license-cc  { background:#1a5c2a; color:#6f6; }
  .license-arr { background:#5c3a1a; color:#fa3; }
  .license-unk { background:#444; color:#aaa; }
  /* Fixed overlay: the panel used to sit below ~10 rows of cards, so opening it
     looked like nothing happened. */
  .detail-panel { position:fixed; left:50%; bottom:52px; transform:translateX(-50%);
    width:min(620px,calc(100% - 32px)); max-height:72vh; overflow:auto; z-index:10;
    padding:14px 34px 14px 14px; border:1px solid var(--orca-border,#444); border-radius:8px;
    background:var(--orca-bg,#1e1e1e); box-shadow:0 6px 28px rgba(0,0,0,.55); display:none; }
  .detail-panel.active { display:block; }
  .detail-panel .det-close { position:absolute; margin:0; top:6px; right:8px; background:none; border:none;
    color:var(--orca-muted,#888); font-size:1.4em; line-height:1; cursor:pointer; padding:2px 6px; }
  .detail-panel h2 { margin-bottom:6px; font-size:1.1em; }
  .detail-panel p { margin-bottom:6px; color:var(--orca-muted,#aaa); font-size:0.88em; }
  .detail-panel a { color:var(--orca-accent,#4a9eff); }
  .detail-panel button { margin-top:10px; padding:8px 20px; border:none; border-radius:6px; background:var(--orca-accent,#4a9eff); color:var(--orca-accent-fg,#fff); cursor:pointer; font-size:0.95em; }
  .detail-panel button:disabled { opacity:0.35; cursor:not-allowed; }
  /* The file picker makes the panel taller than its 60vh, and this button is the
     last child: without sticky it scrolls out of sight, which is the same
     below-the-fold trap that made Import look dead once already. */
  #det-import-btn { position:sticky; bottom:0; }
  .detail-panel button.secondary { background:transparent; border:1px solid var(--orca-border,#444);
    color:var(--orca-fg,#eee); margin-left:8px; }
  /* Fixed, not in the flow: it used to sit under 30 result cards, thousands of
     pixels below the fold, so every error and every progress message was
     invisible. Pressing Import and seeing nothing happen was exactly this. */
  #status { position:fixed; left:0; right:0; bottom:0; z-index:20; padding:6px 14px;
    background:var(--orca-bg,#1e1e1e); border-top:1px solid var(--orca-border,#444);
    color:var(--orca-muted,#888); font-size:0.8em; }
  #status.error { color:#ff6b6b; border-top-color:#ff6b6b; }
  /* Keep the last row of cards clear of the fixed status bar. */
  body { padding-bottom:44px; }
  /* Always visible next to the licence: the detail panel is the only route to a download,
     so this is the notice every user passes through. */
  .license-url { color:var(--orca-muted,#888); font-size:0.8em; word-break:break-all; }
  /* A print can hold dozens of parts; without a choice every one of them lands on
     the plate at once. Hidden until the resolver reports more than one file. */
  .file-picker { display:none; margin:8px 0 0; max-height:150px; overflow:auto;
    border:1px solid var(--orca-border,#444); border-radius:6px; padding:6px 8px; }
  .file-picker.active { display:block; }
  .file-picker label { display:block; font-size:0.85em; padding:2px 0; word-break:break-all; }
  .file-picker .hint { color:var(--orca-muted,#888); font-size:0.8em; margin-bottom:4px; }
  /* Shown only for a result whose platform needs an account the user has not
     given a token for. The button stays visible but disabled: hiding it would
     read as "this model has no files", which is the wrong diagnosis. */
  .gate { display:none; font-size:0.85em; margin:8px 0 4px; color:var(--orca-warn,#e0a030); }
  .gate.active { display:block; }
  .tokenbox { display:none; gap:6px; margin:4px 0 10px; }
  .tokenbox.active { display:flex; }
  .tokenbox input { flex:1; min-width:0; }
  .acct { border-bottom:1px solid var(--orca-border,#333); padding:10px 0; }
  .acct:last-child { border-bottom:none; }
  .acct h3 { font-size:0.95em; display:flex; align-items:center; gap:8px; }
  .acct p { font-size:0.84em; margin:4px 0 0; }
  .pill { font-size:0.72em; padding:1px 7px; border-radius:3px; }
  .pill-ok { background:#1a5c2a; color:#6f6; }
  .pill-need { background:#5c3a1a; color:#fa3; }
  .pill-off { background:#444; color:#aaa; }
  .acct .row { display:flex; gap:6px; margin-top:8px; }
  .acct .row input { flex:1; min-width:0; }
  .acct details { margin-top:8px; }
  .acct summary { font-size:0.8em; color:var(--orca-muted,#888); cursor:pointer; }
  .acct .note { font-size:0.8em; margin-top:6px; }
  .acct .note.bad { color:var(--orca-warn,#e0a030); }
  .file-picker .hint button { font-size:0.9em; padding:1px 6px; margin-left:6px; }
  .responsibility { margin:10px 0 0; padding:8px 10px; border-left:3px solid var(--orca-border,#444);
    color:var(--orca-muted,#888); font-size:0.8em; line-height:1.45; }
</style>
<h1>&#128269; 3D Model Search</h1>
<div class="search-row">
  <input id="query" type="text" placeholder="Search for 3D models..." />
  <button id="search-btn" onclick="doSearch()">Search</button>
  <button class="secondary" onclick="showAccounts()">Accounts</button>
</div>
<div class="platforms">
  <!-- All four searchable platforms are offered ticked by default. Thingiverse
       and GrabCAD are shown disabled with the short reason, so the window does
       not pretend they do not exist. Importing needs a file the plugin can
       fetch: Printables serves one to anyone, MakerWorld to the account whose
       token is in the plugin's config. A result that is neither says so on its
       own panel. -->
  <label><input type="checkbox" checked data-platform="printables"> Printables</label>
  <label><input type="checkbox" checked data-platform="makerworld"> MakerWorld</label>
  <label><input type="checkbox" checked data-platform="nexprint"> Nexprint</label>
  <label><input type="checkbox" checked data-platform="makeronline"> Makeronline</label>
  <label><input type="checkbox" disabled data-platform="thingiverse"> Thingiverse (API token needed)</label>
  <label><input type="checkbox" disabled data-platform="grabcad"> GrabCAD (API retired)</label>
</div>
<div id="accounts" class="detail-panel">
  <button class="det-close" onclick="$('accounts').classList.remove('active')">&times;</button>
  <h2>Accounts</h2>
  <p>Connect the portals you already have an account on. The plugin never asks for a
     password and never opens a browser: you sign in at the portal yourself, and it
     keeps only the token you hand it, in OrcaSlicer's own plugin config.</p>
  <div id="acct-rows"></div>
</div>
<div id="results"></div>
<div id="detail" class="detail-panel">
  <button id="det-close" class="det-close" title="Close">&times;</button>
  <h2 id="det-name"></h2>
  <p id="det-author"></p>
  <p id="det-platform"></p>
  <p id="det-url"></p>
  <p id="det-license"></p>
  <p id="det-summary"></p>
  <p class="responsibility">You download this model under your own account and on your own
    responsibility. Complying with its licence and with the platform's terms of use is
    yours, not this plugin's — it neither hosts nor redistributes any file.
    If a design is protected by copyright, downloading or using it against the rights
    holder's terms is your act alone, and the authors of this plugin accept no liability
    for it.</p>
  <div id="det-files" class="file-picker"></div>
  <p id="det-gate" class="gate"></p>
  <div id="det-tokenbox" class="tokenbox">
    <input id="det-token" type="password" placeholder="Paste the token here" autocomplete="off">
    <button id="det-token-save" onclick="saveToken()">Save</button>
  </div>
  <button id="det-import-btn" onclick="doImport()">Import into OrcaSlicer</button>
</div>
<div id="status">Ready. Type a keyword and press Search.</div>
<script>
  // ponytail: no JS console in the wx webview -> pipe errors/probes to Python stderr.
  function jlog(m) { try { orca.postMessage({action:"log", msg:String(m)}); } catch(e) {} }
  // Asked for by the page rather than pushed at window creation: the host builds
  // the window before the document is listening, so a push would race the load.
  try { orca.postMessage({action:"accounts"}); } catch (e) {}
  window.onerror = function(m, s, l, c, e) {
    jlog("JSERR " + m + " @" + l + ":" + c + (e && e.stack ? " | " + e.stack : ""));
  };

  var selectedModel = null;
  var searching = false;
  var $ = function(id) { return document.getElementById(id); };

  function doSearch() {
    if (searching) return;
    var q = $("query").value.trim();
    if (!q) return;
    searching = true;
    var btn = $("search-btn");
    btn.disabled = true;
    btn.textContent = "Searching...";
    var platforms = [];
    var cbs = document.querySelectorAll(".platforms input:checked");
    for (var i = 0; i < cbs.length; i++) platforms.push(cbs[i].dataset.platform);
    $("status").textContent = "Searching...";
    $("detail").classList.remove("active");
    selectedModel = null;
    orca.postMessage({action:"search", query:q, platforms:platforms});
  }

  $("query").addEventListener("keydown", function(e) {
    if (e.key === "Enter") doSearch();
  });

  function esc(s) { return String(s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;").replace(/'/g,"&#39;"); }

  function renderResults(models) {
    window._results = models;
    var html = "";
    for (var i = 0; i < models.length; i++) {
      var m = models[i];
      html += '<div class="card" data-idx="' + i + '">'
            + '<img src="' + esc(m.thumbnail_url || "") + '" alt="">'
            + '<h3 title="' + esc(m.name) + '">' + esc(m.name) + '</h3>'
            + '<div class="author">' + esc(m.author) + ' \u00b7 ' + esc(m.platform) + '</div>'
            + '<span class="license-badge ' + licenseClass(m.license) + '">'
            + esc(m.license || "Unknown") + '</span>'
            + '</div>';
    }
    $("results").innerHTML = html;
    $("status").textContent = models.length + " result(s)";
  }

  function showDetail(model) {
    selectedModel = model;
    $("det-name").textContent = model.name;
    $("det-author").innerHTML = "<strong>Author:</strong> " + esc(model.author);
    $("det-platform").innerHTML = "<strong>Platform:</strong> " + esc(model.platform);
    // The licence URL stays visible as text: an <a> would either navigate this
    // webview away (killing the plugin UI) or need an external browser, and the
    // plugin no longer opens one.
    $("det-license").innerHTML = "<strong>License:</strong> <span class=\"license-badge " + licenseClass(model.license) + "\">" + esc(model.license || "Unknown") + "</span>"
      + (model.license_url ? " <span class=\"license-url\">" + esc(model.license_url) + "</span>" : "");
    $("det-summary").textContent = model.license_summary || "No license information available.";
    hideFilePicker();
    resetImportBtn();
    showGate(model);
    // Shown as plain text, never as a link: nothing in this panel may navigate the
    // webview or reach the system browser.
    $("det-url").textContent = model.url ? model.platform + ": " + model.url : "";
    $("detail").classList.add("active");
  }

  // Filled from the Accounts message, so the help text has one home (Python's
  // _TOKEN_HELP) instead of a copy here that can drift from it.
  var TOKEN_HELP = {}, ACCOUNTS = [];

  function showAccounts() {
    var box = $("acct-rows"), html = "";
    ACCOUNTS.forEach(function (a, i) {
      var pill = {connected:["pill-ok","Connected"], needs_token:["pill-need","Not connected"],
                  open:["pill-ok","No account needed"], unavailable:["pill-off","Not importable"]}[a.state];
      html += '<div class="acct"><h3>' + esc(a.platform)
           +  ' <span class="pill ' + pill[0] + '">' + pill[1] + '</span></h3>';
      if (a.state === "needs_token" || a.state === "connected") {
        html += '<p>' + esc(a.help) + '</p>';
        if (a.can_sign_in) {
          html += '<div class="row"><input type="email" autocomplete="off" data-email="' + i + '"'
               +  ' placeholder="Your ' + esc(a.platform) + ' account email">';
          if (a.sign_in_mode === "password") {
            // No code to send: the portal's own SSO has none, so asking for one
            // would leave the user waiting for a mail that never arrives.
            html += '</div><div class="row">'
                 +  '<input type="password" autocomplete="off" data-password="' + i + '"'
                 +  ' placeholder="Your ' + esc(a.platform) + ' password">'
                 +  '<button onclick="signIn(' + i + ')">Sign in</button></div>';
          } else {
            html += '<button data-send="' + i + '" onclick="sendCode(' + i + ')">Send code</button></div>'
                 +  '<div class="row" data-codebox="' + i + '" style="display:none">'
                 +  '<input type="text" inputmode="numeric" autocomplete="one-time-code" maxlength="6"'
                 +  ' data-code="' + i + '" placeholder="Six-digit code from the email">'
                 +  '<button onclick="signIn(' + i + ')">Sign in</button></div>';
          }
          html += '<p class="note" data-note="' + i + '"></p>'
               +  '<details><summary>Or paste a token instead</summary>';
        }
        html += '<div class="tokenbox active">'
             +  '<input type="password" autocomplete="off" data-acct="' + i + '" placeholder="'
             +  (a.state === "connected" ? "Replace the saved token" : "Paste the token here") + '">'
             +  '<button onclick="saveAcct(' + i + ')">Save</button></div>';
        if (a.can_sign_in) html += '</details>';
      } else if (a.state === "unavailable") {
        html += '<p>Searchable, but it serves no file the plugin can fetch yet.</p>';
      } else {
        html += '<p>Files download without signing in to anything.</p>';
      }
      html += '</div>';
    });
    box.innerHTML = html;
    $("accounts").classList.add("active");
  }

  function acctEl(kind, i) {
    return document.querySelector('#acct-rows [data-' + kind + '="' + i + '"]');
  }

  function acctNote(i, text, bad) {
    var n = acctEl("note", i);
    if (!n) return;
    n.textContent = text || "";
    n.classList.toggle("bad", !!bad);
  }

  function sendCode(i) {
    var e = acctEl("email", i), v = e && e.value.trim();
    if (!v) { acctNote(i, "Enter your account email first.", true); return; }
    acctEl("send", i).disabled = true;
    acctNote(i, "Asking " + ACCOUNTS[i].platform + " to email a code\u2026");
    orca.postMessage({action:"send_code", platform:ACCOUNTS[i].platform, email:v});
  }

  function signIn(i) {
    var pw = ACCOUNTS[i].sign_in_mode === "password";
    var f = acctEl(pw ? "password" : "code", i), v = f && f.value;
    if (!pw) v = v && v.trim();
    if (!v) {
      acctNote(i, pw ? "Enter your password." : "Enter the six-digit code.", true);
      return;
    }
    var e = acctEl("email", i), em = e && e.value.trim();
    if (!em) { acctNote(i, "Enter your account email first.", true); return; }
    acctNote(i, "Signing in\u2026");
    var m = {action:"sign_in", platform:ACCOUNTS[i].platform, email:em};
    // The password never goes in the "code" field: _do_auth reads them apart,
    // and only the token that comes back is ever stored.
    if (pw) m.password = v; else m.code = v;
    orca.postMessage(m);
  }

  function saveAcct(i) {
    var el = document.querySelector('#acct-rows input[data-acct="' + i + '"]');
    var t = el && el.value.trim();
    if (!t) return;
    orca.postMessage({action:"save_token", platform:ACCOUNTS[i].platform, token:t});
  }

  // A result the plugin cannot fetch must say why on the panel, not on the
  // press: pressing a button that then reports a login wall is the dead end
  // this replaces.
  function showGate(model) {
    var gate = $("det-gate"), btn = $("det-import-btn");
    if (model.importable) {
      gate.classList.remove("active");
      gate.textContent = "";
      $("det-tokenbox").classList.remove("active");
      return;
    }
    // Only MakerWorld has a token route today; the others are gated because no
    // resolver exists for them at all, and a recipe would be a lie there.
    gate.textContent = model.platform + " serves files only to an account."
      + (TOKEN_HELP[model.platform] || " The plugin cannot fetch its files yet.");
    gate.classList.add("active");
    // A disabled button under an instruction is a dead end: the user is told what
    // to do and given nothing to do it with. The field is the action.
    $("det-tokenbox").classList.toggle("active", !!TOKEN_HELP[model.platform]);
    btn.disabled = true;
    btn.textContent = TOKEN_HELP[model.platform]
      ? "Save your token to import" : "Not importable";
  }

  function saveToken() {
    if (!selectedModel) return;
    var t = $("det-token").value.trim();
    if (!t) return;
    $("det-token-save").disabled = true;
    orca.postMessage({action:"save_token", platform:selectedModel.platform, token:t});
  }

  function doImport() {
    if (!selectedModel) return;
    // null on the first press: Python resolves the files and, for a multi-part
    // print, sends them back to be picked before anything is downloaded.
    var files = chosenFiles();
    if (files && files.length === 0) {
      $("status").textContent = "Select at least one file to import.";
      return;
    }
    var btn = $("det-import-btn");
    btn.disabled = true;
    btn.textContent = "Importing...";
    $("status").textContent = files ? "Downloading..." : "Resolving files...";
    orca.postMessage({action:"import", model:selectedModel, files:files});
  }

  // A print's parts are few and usually all wanted, so they start ticked. A
  // MakerWorld design's plates are not: one Benchy carries 91 profiles, and
  // starting those ticked turns a single press into 91 downloads. Above the
  // threshold the list starts empty and says so.
  var PRETICK_LIMIT = 8;

  function showFilePicker(files) {
    window._files = files;
    var many = files.length > PRETICK_LIMIT;
    var html = '<div class="hint">' + files.length + (many
               ? ' files \u2014 tick the ones you want on the plate.'
               : ' files in this print \u2014 untick what you do not want on the plate.')
             + '<button type="button" onclick="setAllFiles(true)">All</button>'
             + '<button type="button" onclick="setAllFiles(false)">None</button></div>';
    for (var i = 0; i < files.length; i++) {
      html += '<label><input type="checkbox"' + (many ? '' : ' checked') + ' data-fidx="' + i + '"> '
            + esc(files[i].name) + '</label>';
    }
    var p = $("det-files");
    p.innerHTML = html;
    p.classList.add("active");
    resetImportBtn();
  }

  function hideFilePicker() {
    var p = $("det-files");
    p.classList.remove("active");
    p.innerHTML = "";
    window._files = null;
  }

  function setAllFiles(on) {
    var boxes = $("det-files").querySelectorAll("input[type=checkbox]");
    for (var i = 0; i < boxes.length; i++) boxes[i].checked = on;
  }

  function chosenFiles() {
    var p = $("det-files");
    if (!p.classList.contains("active")) return null;
    var out = [], boxes = p.querySelectorAll("input[type=checkbox]");
    for (var i = 0; i < boxes.length; i++) {
      if (boxes[i].checked) out.push(window._files[parseInt(boxes[i].dataset.fidx, 10)]);
    }
    return out;
  }

  function resetImportBtn() {
    var btn = $("det-import-btn");
    btn.disabled = false;
    btn.textContent = $("det-files").classList.contains("active")
      ? "Import selected" : "Import into OrcaSlicer";
  }

  function licenseClass(lic) {
    if (!lic) return "license-unk";
    if (/CC|Creative Commons|CC0|Public Domain/i.test(lic)) return "license-cc";
    if (/All Rights Reserved|ARR|Standard Digital File/i.test(lic)) return "license-arr";
    return "license-unk";
  }

  // Delegated on the container, which is never itself replaced.
  $("results").addEventListener("click", function(e) {
    var card = e.target.closest ? e.target.closest(".card") : null;
    if (!card) return;
    var m = window._results[parseInt(card.dataset.idx, 10)];
    if (m) showDetail(m);
  });
  $("det-close").addEventListener("click", function() {
    $("detail").classList.remove("active");
    hideFilePicker();
    selectedModel = null;
  });

  orca.onMessage(function(msg) {
    if (msg && msg.action === "test_bridge") {
      orca.postMessage({action:"test_ack"});
      return;
    }
    var btn = document.getElementById("search-btn");
    if (msg && msg.action === "results") {
      searching = false;
      btn.disabled = false;
      btn.textContent = "Search";
      if (msg.results.length === 0) {
        document.getElementById("status").textContent = "No results found.";
      } else {
        document.getElementById("status").textContent = msg.results.length + " result(s)";
        renderResults(msg.results);
      }
    } else if (msg && msg.action === "status") {
      $("status").textContent = msg.message;
      $("status").classList.remove("error");
    } else if (msg && msg.action === "choose_files") {
      showFilePicker(msg.files);
      $("status").textContent = msg.files.length
        + " files in this print - choose, then press Import selected.";
      $("status").classList.remove("error");
    } else if (msg && msg.action === "imported") {
      resetImportBtn();
      $("status").textContent = "Imported " + msg.count + " file(s) into OrcaSlicer - see Prepare.";
    } else if (msg && msg.action === "download_done") {
      document.getElementById("status").textContent = "Downloaded: " + msg.name;
      document.getElementById("detail").innerHTML +=
        "<p style=\"color:#4a4;margin-top:8px\">Saved to:<br><code>" + esc(msg.path) + "</code></p>"
        + "<p style=\"color:var(--orca-muted)\">Open this file from the OrcaSlicer File menu.</p>";
    } else if (msg && msg.action === "accounts") {
      ACCOUNTS = msg.rows || [];
      TOKEN_HELP = {};
      ACCOUNTS.forEach(function (a) { if (a.help) TOKEN_HELP[a.platform] = " " + a.help; });
      // First run: show the setup instead of letting the user meet the gate
      // later, on a model they already decided they wanted.
      if (msg.first_run) showAccounts();
    } else if (msg && msg.action === "auth") {
      var ai = -1;
      ACCOUNTS.forEach(function (a, k) { if (a.platform === msg.platform) ai = k; });
      if (ai < 0) return;
      var send = acctEl("send", ai);
      if (send) send.disabled = false;
      var secret = acctEl("password", ai);
      if (secret) secret.value = "";
      if (!msg.ok) { acctNote(ai, msg.message, true); return; }
      if (msg.stage === "code_sent") {
        var box = document.querySelector('#acct-rows [data-codebox="' + ai + '"]');
        if (box) box.style.display = "flex";
        var c = acctEl("code", ai); if (c) c.focus();
      }
      acctNote(ai, msg.message || "");
    } else if (msg && msg.action === "token_saved") {
      $("det-token-save").disabled = false;
      if (!msg.ok) { $("status").textContent = "Could not save the token: " + (msg.message || ""); return; }
      $("det-token").value = "";
      // Every result from that platform becomes importable, not just this one:
      // the token is the platform's gate, not the model's.
      (window._results || []).forEach(function (r) {
        if (r.platform === msg.platform) r.importable = true;
      });
      if (selectedModel && selectedModel.platform === msg.platform) {
        selectedModel.importable = true;
        showGate(selectedModel);
        resetImportBtn();
      }
      ACCOUNTS.forEach(function (a) { if (a.platform === msg.platform) a.state = "connected"; });
      if ($("accounts").classList.contains("active")) showAccounts();
      $("status").textContent = msg.platform + " token saved. You can import now.";
    } else if (msg && msg.action === "error") {
      searching = false;
      btn.disabled = false;
      btn.textContent = "Search";
      resetImportBtn();
      var st = document.getElementById("status");
      st.textContent = "Error: " + msg.message;
      st.classList.add("error");
    }
  });
</script>
</body></html>"""


if orca is not None:

    class SearchEngineScript(orca.script.ScriptPluginCapabilityBase):
        win = None

        def get_name(self):
            return "3D Model Search"

        def get_default_config(self):
            # Seeds the Config tab's JSON editor with the key to fill in, so
            # "which key?" is answered by the editor itself rather than by docs.
            return {"makerworld_token": ""}

        def on_load(self):
            # get_config() works only on the instance the host builds, which is
            # this one. Logging the key names (never the values) here is also the
            # answer to the only support question this feature can raise: did the
            # user's token actually reach the plugin?
            print("[search_engine] config keys: %s" % (sorted(self._tokens().keys()),),
                  file=sys.stderr, flush=True)

        def _tokens(self):
            """The user's own platform tokens, from this capability's config.

            The host stores and returns it; the plugin never writes a credential
            file of its own, and never reads the app's (the audit hook denies
            OrcaSlicer.conf and orca_refresh_token.sec to plugins by design).
            """
            try:
                return json.loads(self.get_config() or "{}")
            except Exception as e:
                print("[search_engine] config unreadable, treating as empty: %r"
                      % (e,), file=sys.stderr, flush=True)
                return {}

        def execute(self):
            if self.win is not None and self.win.is_open():
                self.win.close()

            html = PAGE
            if os.environ.get("SEARCH_ENGINE_AUTORUN"):
                # ponytail: debug-only self-drive, so the probes run without a mouse.
                html = html.replace(
                    "</script>\n</body></html>",
                    "setTimeout(function(){ $(\"query\").value = "
                    + ("\"%s\"" % os.environ.get("SEARCH_ENGINE_AUTORUN_QUERY", "benchy"))
                    + "; doSearch(); }, 1500);"
                    "setTimeout(function(){ var i = -1;"
                    " for (var k = 0; k < window._results.length; k++) if (window._results[k].importable) { i = k; break; }"
                    " var c = i >= 0 ? document.querySelector('#results .card[data-idx=\"' + i + '\"]') : null;"
                    " jlog('AUTORUN importable idx=' + i + ' card=' + !!c); if (c) c.click(); }, 9000);"
                    "setTimeout(function(){ jlog('AUTORUN import click'); doImport(); }, 12000);"
                    # A multi-part print stops at the picker, so press again.
                    "setTimeout(function(){ if ($('det-files').classList.contains('active'))"
                    " { jlog('AUTORUN picker confirm'); doImport(); } }, 16000);"
                    "\n</script>\n</body></html>")

            self.win = orca.host.ui.create_window(
                html=html,
                title="3D Model Search",
                width=940,
                height=680,
                on_message=self.on_message,
                on_close=self.on_close,
            )
            return orca.ExecutionResult.success()

        def on_message(self, msg):
            msg = msg or {}
            action = msg.get("action", "")
            if action == "log":
                # stderr is teed by the host to <datadir>/log/python_*.log
                print("[search_engine/js] " + str(msg.get("msg", "")), file=sys.stderr, flush=True)
            elif action == "search":
                threading.Thread(target=self._do_search, args=(msg,), daemon=True).start()
            elif action == "accounts":
                self._post_accounts()
            elif action in ("send_code", "sign_in"):
                threading.Thread(target=self._do_auth, args=(action, msg),
                                 daemon=True).start()
            elif action == "save_token":
                self._save_token(msg)
            elif action == "import":
                model = msg.get("model") or {}
                if model:
                    threading.Thread(target=self._do_import,
                                     args=(model, msg.get("files")), daemon=True).start()

        def _post_accounts(self):
            # get_config_version() is "" until something has been saved, which is
            # the first-run signal without inventing a config key for it.
            try:
                first_run = not (self.get_config_version() or "")
            except Exception:
                first_run = False
            self._post({"action": "accounts", "rows": _accounts(self._tokens()),
                        "first_run": first_run})

        def _do_auth(self, action, msg):
            """Run one step of a platform's in-window sign-in, off the UI thread."""
            platform = msg.get("platform", "")
            pair = _AUTH.get(platform)
            if not pair:
                self._post({"action": "auth", "ok": False, "platform": platform,
                            "message": "%s cannot be signed into from here." % platform})
                return
            send_code, login = pair
            try:
                if action == "send_code":
                    if send_code is None:
                        raise RuntimeError(
                            "%s has no e-mail code; sign in with your password."
                            % platform)
                    note = send_code((msg.get("email") or "").strip())
                    self._post({"action": "auth", "ok": True, "platform": platform,
                                "stage": "code_sent", "message": note})
                    return
                # Same two-argument call either way: a code for the portals
                # that mail one, the password for the portals that do not.
                token = login((msg.get("email") or "").strip(),
                              (msg.get("code") or msg.get("password") or ""))
            except Exception as e:
                self._post({"action": "auth", "ok": False, "platform": platform,
                            "message": str(e) or repr(e)})
                return
            # Straight into the same store the pasted token uses, so there is one
            # code path holding a credential and one place it can go wrong.
            self._save_token({"platform": platform, "token": token})

        def _save_token(self, msg):
            """Store one platform's token in the config the host keeps for us.

            Merged into the existing config rather than replacing it, so saving a
            second platform's token never drops the first.
            """
            key = _TOKEN_KEYS.get(msg.get("platform", ""))
            if not key:
                self._post({"action": "token_saved", "ok": False,
                            "message": "unknown platform"})
                return
            cfg = self._tokens()
            cfg[key] = (msg.get("token") or "").strip()
            try:
                ok = bool(self.save_config(json.dumps(cfg)))
                err = "" if ok else "the host refused to write the config"
            except Exception as e:
                ok, err = False, repr(e)
            # Never echo the token back, not even in the failure path.
            print("[search_engine] save_config(%s) -> %s %s" % (key, ok, err),
                  file=sys.stderr, flush=True)
            self._post({"action": "token_saved", "ok": ok,
                        "platform": msg.get("platform", ""), "message": err})
            if ok:
                self._post_accounts()

        def on_close(self):
            self.win = None

        def _do_search(self, msg):
            query = msg.get("query", "")
            platforms = msg.get("platforms", [])
            tokens = self._tokens()
            results = []
            errors = []
            for platform in platforms:
                adapter = _SEARCHERS.get(platform)
                if not adapter:
                    continue
                if not adapter.enabled(tokens):
                    continue
                # Searching covers every listed platform; only the import step
                # needs a resolver, and for a gated platform also a token.
                try:
                    found = adapter.search(query, tokens)
                    for r in found:
                        r["importable"] = _importable(r.get("platform", ""), tokens)
                    results.extend(found)
                except Exception as e:
                    # Posting the error here used to be pointless: the results
                    # message below arrived straight after and overwrote the
                    # status line with "No results found", so a failed search was
                    # indistinguishable from a search that found nothing — an
                    # empty dark window with no explanation.
                    errors.append("%s: %s" % (platform, e))
                    print("[search_engine] %s search failed: %r"
                          % (platform, e), file=sys.stderr, flush=True)
            if errors and not results:
                self._post({"action": "error", "message": "; ".join(errors)})
                return
            if errors:
                self._post({"action": "status",
                            "message": "%d result(s); %s" % (len(results), "; ".join(errors))})
            self.win.post({"action": "results", "results": results})

        def _do_import(self, model, files=None):
            """Download the model's files and load them into the running plater.

            `files` is None on the first press: the print's files are resolved and,
            if there is more than one, handed back to the UI to be picked. A print
            can carry dozens of parts, and importing all of them unasked buries the
            plate.
            """
            if files is None:
                platform = model.get("platform", "")
                resolver = _FILE_RESOLVERS.get(platform)
                if resolver is None:
                    # Defensive only: the panel disables Import for these.
                    self._post({"action": "error", "message":
                                "%s serves no file the plugin can fetch, so it cannot be "
                                "imported." % (platform or "This platform")})
                    return
                tokens = self._tokens()
                try:
                    # A missing token for a gated platform surfaces here, as the
                    # resolver's own message naming the config key to fill.
                    files = resolver(model.get("url", ""), tokens)
                except Exception as e:
                    self._post({"action": "error", "message": f"Could not list files: {e}"})
                    return
                if not files:
                    self._post({"action": "error", "message": "No downloadable files found."})
                    return
                if len(files) > 1:
                    self._post({"action": "choose_files", "files": files})
                    return
                self._import_files(model, files, tokens)
            else:
                self._import_files(model, files, self._tokens())

        def _import_files(self, model, files, tokens=None):
            minter = _URL_MINTERS.get(model.get("platform", ""))
            dest_dir = _download_dir()
            try:
                os.makedirs(dest_dir, exist_ok=True)
            except PermissionError as e:
                self._post({"action": "error", "message":
                            "Cannot write to %s (%s). Orca's plugin audit hook only "
                            "allows writes under its data directory." % (dest_dir, e)})
                return
            paths = []
            for i, f in enumerate(files, 1):
                self._post({"action": "status",
                            "message": "Downloading %d/%d: %s" % (i, len(files), f["name"])})
                try:
                    # Minted here, not at list time: the URL expires in minutes,
                    # so it must be fetched immediately after it is issued.
                    url = f.get("url") or (minter(f, tokens) if minter else "")
                    paths.append(self._download(url, f["name"], dest_dir))
                except Exception as e:
                    self._post({"action": "error", "message": f"{f['name']}: {e}"})
                    return

            ok, detail = _load_in_orca(paths)
            if ok:
                self._post({"action": "imported", "count": len(paths), "dir": dest_dir})
            else:
                self._post({"action": "error", "message":
                            "Downloaded to %s but could not reach OrcaSlicer (%s)." % (dest_dir, detail)})

        @staticmethod
        def _download(url, name, dest_dir):
            import requests

            safe = re.sub(r"[^\w.\- ]", "_", name).strip() or "model.stl"
            path = os.path.join(dest_dir, safe)
            with requests.get(url, stream=True, timeout=180,
                              headers={"User-Agent": _BROWSER_UA}) as r:
                r.raise_for_status()
                with open(path, "wb") as fh:
                    for chunk in r.iter_content(chunk_size=262144):
                        if chunk:
                            fh.write(chunk)
            return path

        def _post(self, msg):
            if self.win is not None and self.win.is_open():
                self.win.post(msg)

    @orca.plugin
    class SearchEnginePlugin(orca.base):
        def register_capabilities(self):
            orca.register_capability(SearchEngineScript)
            if os.environ.get("SEARCH_ENGINE_AUTORUN"):
                def _autorun():
                    time.sleep(12)
                    try:
                        # create_window is CallAfter-marshaled, so off-thread is safe.
                        SearchEngineScript().execute()
                    except Exception as e:
                        print("[search_engine] autorun failed: %r" % (e,), file=sys.stderr, flush=True)
                threading.Thread(target=_autorun, daemon=True).start()
