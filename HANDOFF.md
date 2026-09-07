# OrcaSlicer Model Search Plugin — Status

**Updated**: 2026-09-07 (v0.2.2, live on the hub)
**Project**: `/home/tommaso/projects/Orca_plugin_Search_Engine/`
**OrcaBelt instance**: behemoth, `--datadir /home/tommaso/.config/OrcaBelt2608-test`
**Plugin path on behemoth**: `~/.config/OrcaBelt2608-test/orca_plugins/search_engine/search_engine.py`
**Orca source** (behemoth): `~/projects/orca/orcaslicer-pr/src-belt-combined/src/`

---

## What works (verified in the running app, 2026-08-09)

- **Search**: Printables only, 30 results for "benchy". Public API, no auth.
- **Thumbnails**: load fine (cross-origin images from a `file://` origin are not blocked).
- **Card clicks**: delegated listener on `#results`, detail panel opens as a fixed overlay.
- **Import into OrcaSlicer**: Printables model → STL downloaded → **lands in Prepare**.
  Verified: `3dbenchy.stl`, 60.001 × 31.004 × 48 mm, 225154 triangles, on the plate.
## v0.2.0 — the two guard rails

1. **No external browser, ever.** `xdg-open` / `open` / `os.startfile` are gone, the
   "Open in browser" button is gone, and nothing in the panel is an `<a href>`: URLs are
   rendered as text so they can neither navigate the webview nor reach a browser.
2. **Everything offered lands in Prepare.** `_do_search` skips any adapter whose
   `PLATFORM` has no `_FILE_RESOLVERS` entry, so a result the plugin cannot fetch is never
   shown. That is why only Printables is listed.

The three gated adapters stay in the source, dormant. Re-checked 2026-09-06, all still
gated — this is not stale inherited knowledge:

| Platform | Search API | File download |
|----------|-----------|---------------|
| MakerWorld (Bambu) | `api.bambulab.com/v1/search-service/select/design2` | ❌ `/instance/<id>/f3mf` → 403 `Please log in to download models.` |
| Nexprint (Elegoo) | `nexprint.com/gateway/api/v1/model-library-server/model-base-info/search` | ❌ detail returns `file_url: ""` without a session |
| Makeronline (Anycubic) | `POST makeronline.com/api/search/model` | ❌ `files[].url` → 403 AccessDenied (private S3) |
| **Printables (Prusa)** | `searchPrints2` GraphQL query | ✅ **public, no auth** |

Disabled: Thingiverse (`/download:ID` → 403 robots), GrabCAD (API retired).

### How the Printables download works
`api.printables.com/graphql/` is open (introspection off, queries fine) and the site's
own download call needs no authentication. Two requests per print: list the files,
then ask for each file's real URL.

```
{print(id:1041594){stls{id name}}}
mutation { getDownloadLink(id: <stlId>, printId: <printId>,
                           fileType: stl, source: model_detail) { ok output { link } } }
```

**Do not derive the URL from `filePreviewPath`.** That was the old recipe — strip the
basename off the preview path, append the file name — and it only holds for prints
stored the old way (`media/prints/<id>/stls/<n>_<uuid>/<name>_preview.png`). Newer
prints keep previews in their own subfolder (`media/prints/<uuid>/previews/<sha>.png`),
where the derived URL 404s and Import fails with nothing on the plate. 3dbenchy (3161)
is an old-layout print, which is why every test passed while real models did not work.

Verified 2026-09-06 against both layouts: Flexi Capy Snek (1041594), 14 files, first is
HTTP 200 `model/stl` 4307484 bytes; 3D Benchy (3161) still HTTP 200, 11285384 bytes.

### How the file reaches Prepare (all three OSes)
The plugin host API is **read-only** — `orca.host.model/mesh/presets/slicing` only
inspect; there is no import/add-object binding. But every running instance listens on
the session bus for the message a second launch would send
(`slic3r/GUI/InstanceCheck.cpp`):

- name/interface: `com.orcaslicer.OrcaSlicer.InstanceCheck.Object<instance_hash>`
- object: `/com/orcaslicer/OrcaSlicer/InstanceCheck/Object<instance_hash>`
- method: `AnotherInstance(string)`

The string is an argv list in `unescape_strings_cstyle` format — **semicolon-separated,
quoted** (`"orca-slicer";"/path/file.stl"`), *not* space-separated. `argv[0]` is skipped
as the executable path. File paths there reach `EVT_LOAD_MODEL_OTHER_INSTANCE`, i.e. the
plater, and Orca switches to Prepare on its own. `_load_in_orca_dbus()` discovers the
instance hash from `ListNames`, so it needs no configuration.

**D-Bus is Linux-only**, so `_load_in_orca()` dispatches on the OS:

| OS | Transport | Receiver |
|----|-----------|----------|
| Linux | `dbus-send` `AnotherInstance` | `InstanceCheck.cpp` D-Bus listener |
| Windows | `SendMessageW(hwnd, WM_COPYDATA, …)`, `dwData = 1` | `GUI_App.cpp:675` `MSWRegisterMessageHandler` → `handle_message()` |
| macOS | `open -a <bundle> <file>` | `GUI_App::MacOpenFiles` (`GUI_App.cpp:9002`) |

On Windows the plugin runs **inside** the target process, so the main frame is found by
`EnumWindows` filtered on class `wxWindowNR` + both `Instance_Hash_*` props + our own PID —
no instance hash needs computing. On macOS, `open -a` delivers an "open documents" Apple
Event to the running instance; `MacOpenFiles` only spawns a second slicer for `.3mf`, and
models arrive as `.stl`/`.step`.

**Rejected: relaunching with `--single-instance`.** It looks like the obvious portable
answer — `process_command_line()` does set `should_send = true` on that token — but the
`single_instance` CLI option is commented out in `PrintConfig.cpp:12394`, so `setup()`
rejects the argument and the process dies with `setup params error`, exit 254. Measured on
the belt-2026-08 build, 2026-09-06. Without the flag the hand-off depends on the user's
`single_instance` preference, which is `false` by default.

**Only the Linux transport has been exercised on real hardware.** Windows and macOS are
derived from the receiving code, not observed.

---

## Corrections to the previous handoff

The old "OrcaSlicer webview constraints" list was **wrong on nearly every point**. It was
built by inference during debugging, never verified. Reproduced against the same
`libwebkit2gtk-4.1` (2.52.3) Orca links, with the same launcher env, and confirmed inside
the running app:

| Old claim | Reality |
|-----------|---------|
| Dynamic HTML with inline `onclick` is stripped | False — fires normally |
| `addEventListener` on dynamic elements doesn't work | False — works |
| Webview blocks cross-origin images | False — thumbnails load |
| `webbrowser.open()` / no external browser | False — `xdg-open` works, lands in the user's Chrome session |
| "Card clicks do nothing" | The handler *did* fire; the detail panel opened ~2300 px below the fold, under 10 rows of cards. Nobody scrolled. Now a fixed overlay. |
| Only fix is 40 static template cards | That hack also silently capped results at 40 of 126. Removed. |

`ORCABELT_DISABLE_WEBVIEW=1` in the launcher is genuinely inert (not referenced anywhere
in the source).

**Lesson**: the webview had no error channel, so every diagnosis was a guess. It now pipes
`window.onerror` and `jlog()` through `orca.postMessage` → Python `sys.stderr`, which the
host tees to `<datadir>/log/python_*.log`. Read that file before theorising.

---

## Launching the plugin

The Home page side panel now has a **Plugins** entry (between Recent and OrcaCloud) that
opens the same dialog as Tools ▸ Plugins. Three files in the fork, no new assets:

- `resources/web/homepage/index.html` — the `BtnItem` + inline puzzle SVG (`currentColor`, so it follows the theme)
- `resources/web/homepage/js/home.js` — `OnClickPlugins()` → `SendWXMessage({command:"homepage_plugins"})`
- `src/slic3r/GUI/GUI_App.cpp` — `homepage_plugins` → `CallAfter([this]{ open_plugins_dialog(); })`

The label is plain text, not `trans`/`tid`, because a new tid would need a localization entry.

## Testing

**Standalone webview harness** (`scratchpad/wkprobe.py`) — reproduces the plugin webview
outside Orca: same WebKit, same env vars, same `load_html` + `file://` base URI, on Xvfb
so `xdotool` clicks are reliable and the desktop is untouched. This is the fast loop; use
it before touching the real app.

**In-app autorun** — `SEARCH_ENGINE_AUTORUN=1` makes the plugin open its own window,
search "benchy" and click the first importable card, so the probes run with no mouse:

```bash
ssh behemoth 'pkill -f "[O]rcaBelt2608"'   # note the [O] — a plain pattern kills your own ssh
ssh behemoth 'export DISPLAY=:0 XAUTHORITY=/run/user/1000/.mutter-Xwaylandauth.G7VCT3 SEARCH_ENGINE_AUTORUN=1;
              setsid nohup /home/tommaso/bin/orca-belt-2026-08 </dev/null >/tmp/orca_test.log 2>&1 &'
ssh behemoth 'tail -20 "$(ls -t ~/.config/OrcaBelt2608-test/log/python_*.log|head -1)"'
```

Screenshots: `scrot` returns black under Xwayland; capture the window instead —
`import -window $(xdotool search --name "3D Model Search"|head -1) /tmp/x.png`.

---

## Next

0. **Published as v0.2.2 on 2026-09-07** (hub listing `3e89402d-6dfb-4520-a210-b5eec04eb34a`,
   588 subscribers). Seven platform-suffixed copies of `search_engine.py`, the rewritten
   description, the changelog and the tags are live. **0.2.1 is a burned release number** —
   it was saved with the old 0.1.1 files still attached; 0.2.2 is the first release that
   carries this code.

   How the upload actually works, because none of it is obvious:

   - The hub has no publish API (`OrcaCloudServiceAgent` is consume-only). It is the web form
     at cloud.orcaslicer.com ▸ Plugins ▸ the plugin ▸ **Edit plugin**, signed in to OrcaCloud.
   - **A release is created from the VERSION field, so changing the files without bumping the
     version fails silently** — the dialog simply stays open, with no error and no network
     request. Bump the PEP 723 header, push, and upload that.
   - The form **ignores a file whose name matches one already attached**. Remove all seven
     current entries first, then add the seven new ones.
   - `.py` files must carry a target suffix (`_linux_x86_64`, `_macosx_universal`, …); the
     listing keeps one entry per target.
   - Uploading through the browser's own file chooser is not automatable here: the GNOME
     portal dialog is invisible to X11 tooling and CDP's `DOM.setFileInputFiles` never reaches
     this dropzone. What works is `Page.setBypassCSP` + reload, then `fetch()` the file from a
     **commit-pinned** raw.githubusercontent URL inside the page, build `File` objects, assign
     them to the dropzone's `input.files` and dispatch `change`. A branch URL is not enough —
     raw.githubusercontent served the previous commit's body for several minutes.

1. **More importable platforms** — all three others gate files behind a login. Options:
   reuse the browser session's cookies, or add per-platform auth. Nothing else is scrapeable.
2. **Multi-file prints — done.** A print with more than one file now stops at a picker:
   the resolver's list comes back to the panel as a checkbox per file (All / None
   buttons, all ticked), and only the ticked ones are downloaded. A single-file print
   still imports on one press. `test_file_picker.py` covers the three cases with a stub
   `orca` module and no network.

   Note what a big print costs regardless: Flexi Capy Snek (1041594) is 14 files, and
   Orca then raises its own modal per part that is too small to be millimetres (the
   Teeth-Brim / Teeth-Joiner pieces) plus a "multiple parts detected" prompt. Ticking
   everything means clicking through all of them.
3. **`orcaslicer://` deep links** — `GUI_App::start_download()` exists and the
   `AnotherInstance` payload accepts `orcaslicer://open?file=<url>`, letting Orca do the
   download itself. Unused; the plugin fetches with `requests` for control over headers.
