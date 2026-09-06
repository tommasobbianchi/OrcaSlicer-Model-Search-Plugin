# /// script
# requires-python = ">=3.12"
# dependencies = ["requests"]
#
# [tool.orcaslicer.plugin]
# name = "3D Model Search Engine"
# description = "Search 3D models from inside OrcaSlicer and load them straight onto the plate. Only platforms whose files can be fetched without leaving the app are offered, and the licence is always shown before the download. No external browser is ever opened."
# author = "Tommaso Bianchi"
# version = "0.2.0"
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
                "importable": True,
            })
        return results

    GRAPHQL_URL = "https://api.printables.com/graphql/"

    @staticmethod
    def get_files(model_url):
        """Public STL URLs for a print.

        Printables' GraphQL is open, and files.printables.com serves the STLs
        with no auth. The file itself is not in the schema, but the preview
        image is, and it sits in the same folder as the STL.
        """
        import requests

        m = re.search(r"/model/(\d+)", model_url or "")
        if not m:
            return []
        q = "{print(id:%s){stls{name filePreviewPath}}}" % m.group(1)
        r = requests.post(
            PrintablesSearcher.GRAPHQL_URL,
            json={"query": q},
            headers={"Content-Type": "application/json", "User-Agent": _BROWSER_UA},
            timeout=30,
        )
        r.raise_for_status()
        stls = ((r.json().get("data") or {}).get("print") or {}).get("stls") or []
        files = []
        for s in stls:
            preview, name = s.get("filePreviewPath") or "", s.get("name") or ""
            if not preview or "/" not in preview or not name:
                continue
            folder = preview.rsplit("/", 1)[0]
            files.append({
                "name": name,
                "url": "https://files.printables.com/%s/%s" % (folder, urllib.parse.quote(name)),
            })
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


class MakerWorldSearcher:
    # Matches the "platform" field of this adapter's results, so _do_search can
    # check it against _FILE_RESOLVERS before offering the platform at all.
    PLATFORM = "MakerWorld"

    SEARCH_URL = "https://api.bambulab.com/v1/search-service/select/design2"
    BASE = "https://makerworld.com"

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

# Keyed on the "platform" field of a result. Only Printables serves model files
# without a login; MakerWorld ("Please log in to download models"), Nexprint
# (401) and Makeronline (403) all gate the file behind an account.
_FILE_RESOLVERS = {
    "Printables": PrintablesSearcher.get_files,
}


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
  .detail-panel { position:fixed; left:50%; bottom:16px; transform:translateX(-50%);
    width:min(620px,calc(100% - 32px)); max-height:60vh; overflow:auto; z-index:10;
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
  .detail-panel button.secondary { background:transparent; border:1px solid var(--orca-border,#444);
    color:var(--orca-fg,#eee); margin-left:8px; }
  #status { margin-top:10px; color:var(--orca-muted,#888); font-size:0.8em; }
  /* Always visible next to the licence: the detail panel is the only route to a download,
     so this is the notice every user passes through. */
  .license-url { color:var(--orca-muted,#888); font-size:0.8em; word-break:break-all; }
  .responsibility { margin:10px 0 0; padding:8px 10px; border-left:3px solid var(--orca-border,#444);
    color:var(--orca-muted,#888); font-size:0.8em; line-height:1.45; }
</style>
<h1>&#128269; 3D Model Search</h1>
<div class="search-row">
  <input id="query" type="text" placeholder="Search for 3D models..." />
  <button id="search-btn" onclick="doSearch()">Search</button>
</div>
<div class="platforms">
  <!-- Only platforms with an entry in _FILE_RESOLVERS belong here. Everything listed
       must import onto the plate; a result the plugin cannot fetch has no route left,
       because handing it to the system browser is no longer permitted. -->
  <label><input type="checkbox" checked data-platform="printables"> Printables</label>
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
  <button id="det-import-btn" onclick="doImport()">Import into OrcaSlicer</button>
</div>
<div id="status">Ready. Type a keyword and press Search.</div>
<script>
  // ponytail: no JS console in the wx webview -> pipe errors/probes to Python stderr.
  function jlog(m) { try { orca.postMessage({action:"log", msg:String(m)}); } catch(e) {} }
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
    resetImportBtn();
    // Shown as plain text, never as a link: nothing in this panel may navigate the
    // webview or reach the system browser.
    $("det-url").textContent = model.url ? model.platform + ": " + model.url : "";
    $("detail").classList.add("active");
  }

  function doImport() {
    if (!selectedModel) return;
    var btn = $("det-import-btn");
    btn.disabled = true;
    btn.textContent = "Importing...";
    $("status").textContent = "Resolving files...";
    orca.postMessage({action:"import", model:selectedModel});
  }

  function resetImportBtn() {
    var btn = $("det-import-btn");
    btn.disabled = false;
    btn.textContent = "Import into OrcaSlicer";
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
    } else if (msg && msg.action === "imported") {
      resetImportBtn();
      $("status").textContent = "Imported " + msg.count + " file(s) into OrcaSlicer - see Prepare.";
    } else if (msg && msg.action === "download_done") {
      document.getElementById("status").textContent = "Downloaded: " + msg.name;
      document.getElementById("detail").innerHTML +=
        "<p style=\"color:#4a4;margin-top:8px\">Saved to:<br><code>" + esc(msg.path) + "</code></p>"
        + "<p style=\"color:var(--orca-muted)\">Open this file from the OrcaSlicer File menu.</p>";
    } else if (msg && msg.action === "error") {
      searching = false;
      btn.disabled = false;
      btn.textContent = "Search";
      resetImportBtn();
      document.getElementById("status").textContent = "Error: " + msg.message;
    }
  });
</script>
</body></html>"""


if orca is not None:

    class SearchEngineScript(orca.script.ScriptPluginCapabilityBase):
        win = None

        def get_name(self):
            return "3D Model Search"

        def execute(self):
            if self.win is not None and self.win.is_open():
                self.win.close()

            html = PAGE
            if os.environ.get("SEARCH_ENGINE_AUTORUN"):
                # ponytail: debug-only self-drive, so the probes run without a mouse.
                html = html.replace(
                    "</script>\n</body></html>",
                    "setTimeout(function(){ $(\"query\").value = \"benchy\"; doSearch(); }, 1500);"
                    "setTimeout(function(){ var i = -1;"
                    " for (var k = 0; k < window._results.length; k++) if (window._results[k].importable) { i = k; break; }"
                    " var c = i >= 0 ? document.querySelector('#results .card[data-idx=\"' + i + '\"]') : null;"
                    " jlog('AUTORUN importable idx=' + i + ' card=' + !!c); if (c) c.click(); }, 9000);"
                    "setTimeout(function(){ jlog('AUTORUN import click'); doImport(); }, 12000);"
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
            elif action == "import":
                model = msg.get("model") or {}
                if model:
                    threading.Thread(target=self._do_import, args=(model,), daemon=True).start()

        def on_close(self):
            self.win = None

        def _do_search(self, msg):
            query = msg.get("query", "")
            platforms = msg.get("platforms", [])
            results = []
            errors = []
            for platform in platforms:
                adapter = _SEARCHERS.get(platform)
                if not adapter:
                    continue
                if not adapter.enabled({}):
                    continue
                # A result the plugin cannot fetch is a dead end: the only action
                # left for it would be the system browser, which this plugin no
                # longer opens. So it is never shown.
                if adapter.PLATFORM not in _FILE_RESOLVERS:
                    continue
                try:
                    results.extend(adapter.search(query, {}))
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

        def _do_import(self, model):
            """Download the model's files and load them into the running plater."""
            resolver = _FILE_RESOLVERS.get(model.get("platform", ""))
            if resolver is None:
                # Defensive only: _do_search filters these out before they reach the UI.
                self._post({"action": "error", "message":
                            "%s does not serve files without a login, so it cannot be "
                            "imported." % model.get("platform", "This platform")})
                return
            try:
                files = resolver(model.get("url", ""))
            except Exception as e:
                self._post({"action": "error", "message": f"Could not list files: {e}"})
                return
            if not files:
                self._post({"action": "error", "message": "No downloadable files found."})
                return

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
                    paths.append(self._download(f["url"], f["name"], dest_dir))
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
