# OrcaCloud listing — paste into Edit plugin > Description

Kept here because the hub stores this text itself: it does **not** come from the
PEP 723 header in `search_engine.py`, so the two drift apart silently. The listing
sat at the v0.2.2 wording until 0.5.1 shipped. Update both together.

---

Search 3D models from inside OrcaSlicer and load them straight onto the plate.
Nothing leaves the app: the plugin never opens a browser, and nothing ever has to
be copied out of an address bar or a developer console.

## Signing in actually works now — v0.5.1

Every portal is asked for exactly what it really offers, in the plugin's own
Accounts window:

- **Printables** — no account at all. Search and import, straight away.
- **MakerWorld** — your Bambu account, with no password. Type your e-mail, press
  Send code, enter the six digits Bambu mails you. The code login does not use a
  password, so none is asked for and none is stored.
- **Makeronline (Anycubic)** — your Anycubic account. Anycubic's sign-in offers no
  e-mail code, so this one asks for your password; it goes to Anycubic and nowhere
  else, it is never written to disk, and the field is cleared the moment the reply
  arrives.

In every case only the token the portal returns is kept, in OrcaSlicer's plugin
config and nowhere else. Both sign-ins are verified end to end against live
accounts.

The Accounts screen opens on first run and lists each portal with its real state —
connected, not connected, or no account needed — so you are never left guessing why
a download is refused.

## What you get

Type a query and the plugin searches every portal at once: Printables, MakerWorld,
Makeronline, Thingiverse, GrabCAD and Nexprint, with thumbnails, author and licence
side by side.

**The licence is always shown before the download**, with its name, its link and a
plain-language summary of what it allows.

Pick a model and you get its real file list — one entry per printable part or print
profile — and tick only what you want. A download URL is minted at the moment you
press Import, never while browsing, because those links expire within minutes.

Files land in OrcaSlicer's own downloads folder and open on the plate in the window
you already have in front of you. No second instance, no drag and drop.

## Where it is honest about limits

Results from Thingiverse, GrabCAD and Nexprint are shown, but their files cannot be
fetched yet, and those results say so plainly instead of offering an Import button
that fails. Everything the plugin says it can import, it can import.

Makeronline occasionally answers a download with a captcha request; the plugin
reports that in plain words rather than failing silently.
