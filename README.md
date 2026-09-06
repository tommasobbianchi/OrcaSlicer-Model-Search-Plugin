# 3D Model Search Engine for OrcaSlicer

Search for 3D models from inside OrcaSlicer and load one straight onto the plate.

A Python plugin for OrcaSlicer's plugin system. The licence of every result is shown
before anything is downloaded, and the plugin never opens an external browser.

## Status

**v0.2.0.** Two rules govern the plugin, and everything else follows from them:

1. **It never opens a web browser.** No `xdg-open`, no `open`, no `ShellExecute`. Every
   URL in the interface is text you can read, not a link that navigates anywhere.
2. **Every model it offers lands in Prepare.** The file is downloaded by the plugin and
   handed to the running OrcaSlicer.

That is why only Printables is listed. A platform is offered only if its files can be
fetched without a login, because anything else could be honoured only by sending you to
a browser — which rule 1 forbids.

| Platform | Offered | Why |
|---|---|---|
| **Printables** (Prusa) | **yes** | files are public; import works |
| MakerWorld (Bambu Lab) | no | `Please log in to download models` (403, re-checked 2026-09-06) |
| Nexprint (Elegoo) | no | detail returns an empty `file_url` without a session |
| Makeronline (Anycubic) | no | file URLs are a private S3 bucket, 403 AccessDenied |
| Thingiverse (UltiMaker) | no | developer portal removed in the 2025 migration, no new tokens |
| GrabCAD (Stratasys) | no | public API retired |

The adapters for the login-gated platforms are still in the source. They come back the
day their files can be fetched in-app — adding a `_FILE_RESOLVERS` entry is all it takes.

## Using it

1. **Search** by keyword.
2. **Click a result** — the detail panel shows the licence, its plain-English summary,
   and the URL of the full terms as text.
3. **Import** — the model is downloaded and dropped onto the plate; Orca switches to
   Prepare on its own. It is the only button, and it is the only thing the plugin does.

The licence is on screen before Import can be pressed — the detail panel is the only
route to it — and a notice beside it states that complying with the licence is the
user's own responsibility. Nothing is cached, re-hosted or redistributed, and the
plugin collects no data of any kind.

Files reach the plate through the same channel a second launch would use: the session
bus on Linux, `WM_COPYDATA` on Windows, and an "open documents" Apple Event (`open -a`)
on macOS. Only the Linux path has been run on real hardware; the other two are written
against Orca's own receiving code.

## Install

Copy the plugin into your OrcaSlicer data directory:

```
<datadir>/orca_plugins/search_engine/search_engine.py
```

`<datadir>` is `~/.config/OrcaSlicer` on Linux, `%APPDATA%\OrcaSlicer` on Windows,
`~/Library/Application Support/OrcaSlicer` on macOS.

A side-loaded folder is **not** picked up on its own — Orca also wants an install record
beside the `.py`, which the Plugins dialog writes when you install from there. Creating
it by hand works just as well:

`<datadir>/orca_plugins/search_engine/.install_state.json`

```json
{
  "capabilities": [{ "3D Model Search Engine": true }],
  "enabled": true,
  "installed_from": "local",
  "installed_version": "0.1.0",
  "plugin_name": "3D Model Search Engine"
}
```

Restart Orca, then open it from **Plugins** (side panel on the Home page, or the Tools
menu). The name, description and version shown there come from the PEP 723 header at the
top of `search_engine.py`.

## How the import works

The plugin host API is **read-only** — `orca.host.model`, `mesh`, `presets` and `slicing`
all inspect, none of them add an object. So the plugin sends the model the same way a
second launch of Orca would: over the session bus, to the `AnotherInstance` method the
running instance already listens on (`slic3r/GUI/InstanceCheck.cpp`).

```
name/interface  com.orcaslicer.OrcaSlicer.InstanceCheck.Object<instance_hash>
object          /com/orcaslicer/OrcaSlicer/InstanceCheck/Object<instance_hash>
method          AnotherInstance(string)
```

The string is an argv list in `unescape_strings_cstyle` format — **semicolon-separated
and quoted** (`"orca-slicer";"/path/file.stl"`), not space-separated, and `argv[0]` is
skipped as the executable path. Paths there reach `EVT_LOAD_MODEL_OTHER_INSTANCE`, which
is the plater. The instance hash is discovered from the bus's `ListNames`, so there is
nothing to configure.

Printables' own file URL is not exposed in its GraphQL schema, but `stls { name
filePreviewPath }` is, and the preview image sits in the same folder as the STL:

```
media/prints/3161/stls/123914_<uuid>/3dbenchy_preview.png
→ https://files.printables.com/media/prints/3161/stls/123914_<uuid>/3dbenchy.stl
```

## Legal

Every adapter calls a public endpoint that the platform's own web frontend calls, with no
authentication bypass and no credential extraction. The reasoning, per platform, is in
[`LEGAL_ANALYSIS.md`](LEGAL_ANALYSIS.md).

### Every download is yours, and your responsibility

This plugin searches. It holds no account anywhere, authenticates on nobody's behalf, and
grants no right to any model. Where a platform puts its files behind a login — MakerWorld,
Nexprint and Makeronline all do — **you** sign in with your own credentials, and the
download happens under the terms that platform extends to **you**.

So the licence and the platform's terms of use bind you, not this plugin. The plugin shows
you the licence before the download button is reachable; honouring it — attribution,
non-commercial limits, no-derivatives, share-alike — is yours to do. Any token you enter is
used only to call that platform: never stored elsewhere, never transmitted anywhere else,
never collected. Nothing is cached, re-hosted or redistributed, and no data of any kind is
gathered about you.

Licence metadata is reproduced as each platform publishes it. Where a platform reports it
wrongly or not at all, the model's own page is the authoritative source, and every result
links to it. As stated in the MIT licence below, the plugin comes with no warranty.

### Copyright — no liability accepted

**The authors and contributors of this plugin disclaim all responsibility and all
liability for any download or use of a copyright-protected design carried out against the
rights holder's terms.**

The plugin searches public endpoints and shows what those platforms publish. It does not
host, mirror or redistribute a single file, it does not moderate what a platform lists,
and it has no way to tell whether a listing was uploaded with the rights holder's
permission — a design can be published on a platform in breach of someone's copyright
without that being visible from the outside.

So choosing and downloading a model is your decision and your act. If the design is
protected and your download, print, modification, sharing or sale exceeds what the rights
holder permits, that liability is yours alone; the authors accept none of it, and none for
any claim a rights holder may bring. Licence metadata is informational — it reflects what
the *uploader* declared, which is not proof the uploader held the rights to declare it.

**Rights holders:** this project stores and serves no model content, so there is nothing
here to take down. Requests about a specific model go to the platform hosting it, which is
the only party able to act on them. If a search adapter is nonetheless implicated, please
open an issue.

Full reasoning in [`LEGAL_ANALYSIS.md`](LEGAL_ANALYSIS.md). None of it is legal advice.

## Development

`SEARCH_ENGINE_AUTORUN=1` makes the plugin open its own window, search and click the first
importable result, so the whole path runs without a mouse.

The webview has no error channel of its own, which made every early diagnosis a guess.
It now pipes `window.onerror` and `jlog()` through `orca.postMessage` into Python's
`sys.stderr`, which the host tees to `<datadir>/log/python_*.log`. Read that file first.

`scratchpad/wkprobe.py` reproduces the plugin's webview outside Orca — same WebKit, same
launcher environment, same `load_html` with a `file://` base URI, on Xvfb. It is much the
faster loop; use it before touching the real app.

## Licence

MIT, see [LICENSE](LICENSE). OrcaSlicer is AGPL-3.0; a Python plugin loaded at runtime
across the `orca` API boundary is a separate work.
