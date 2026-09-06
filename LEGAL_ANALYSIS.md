# Legal Analysis — OrcaSlicer 3D Model Search Plugin

## Executive Summary

As of v0.2.0 the plugin offers **one platform, Printables**, over **publicly accessible
endpoints** (no auth, no access control bypass). Search results and license metadata come
from the same public URLs the platform's own web frontend calls, at reasonable rates, with
the license and a responsibility notice displayed before download. Risk: LOW.

The three login-gated platforms (MakerWorld, Nexprint, Makeronline) are no longer offered.
Their adapters used to end in a hand-off to the system browser; v0.2.0 opens no browser at
all, so a platform whose files cannot be fetched in-app has no route left and is not shown.
Their analysis below is retained for the day their files become reachable.

Core safeguards implemented:
1. License metadata displayed BEFORE download (non-negotiable)
2. Public endpoints only — no auth bypass, no credential extraction
3. No caching, redistribution, or re-hosting of model files
4. Responsibility notice shown with the license on every model
5. No data collection or telemetry
6. No external browser is ever launched, and no URL in the interface is a link

Downloading is the user's own act, under the user's own platform account. See
[User Responsibility](#user-responsibility).

---

## Platform-by-Platform Analysis

### 1. MakerWorld (Bambu Lab) — `api.bambulab.com` — **not offered in v0.2.0**

| Aspect | Detail |
|--------|--------|
| Endpoint | `GET api.bambulab.com/v1/search-service/select/design2?keyword=X&limit=30` |
| Auth | None required. Public endpoint. |
| License metadata | `license` field: BY, BY-SA, BY-NC, BY-ND, CC0, Standard Digital File License, Exclusive |
| Files | `designExtension.model_files[]` — model name, type, size |
| Access method | Same API called by makerworld.com frontend. Public CDN endpoint. |
| ToS risk | **LOW** — public endpoint, no auth bypass, no scraping. |

### 2. Nexprint (Elegoo) — `nexprint.com` — **not offered in v0.2.0**

| Aspect | Detail |
|--------|--------|
| Endpoint | `GET nexprint.com/gateway/api/v1/model-library-server/model-base-info/search?keyword=X&pageNo=1&pageSize=30` |
| Auth | None required. Public gateway. |
| License metadata | `licenseType` integer: 0=CC0, 1=BY, 2=BY-SA, 3=BY-NC, 4=BY-NC-SA, 5=BY-ND, 6=BY-NC-ND, 7=ARR |
| Files | `model-base-info/get?id=X` — `modelFileInfoList[]` with fileUrl, fileName |
| Access method | Public REST gateway. Same API called by nexprint.com frontend. |
| ToS risk | **LOW** — public gateway endpoint, no auth bypass. |

### 3. Makeronline (Anycubic) — `makeronline.com` — **not offered in v0.2.0**

| Aspect | Detail |
|--------|--------|
| Endpoint | `POST makeronline.com/api/search/model` with `{keyword, page, page_size, print_type:0, search:1}` |
| Auth | None required. Public endpoint. |
| License metadata | `license` integer. 1=BY, 2=BY-SA, 3=BY-NC, 4=BY-NC-SA, 5=BY-ND, 6=BY-NC-ND, 7=CC0, 8=Standard |
| Files | `api/mold/detail?id=X` — `files[]` with url, file_name |
| Access method | Same POST endpoint called by makeronline.com Nuxt frontend. |
| ToS risk | **LOW** — public endpoint, no auth bypass. |

### 4. Printables (Prusa Research) — `printables.com`

| Aspect | Detail |
|--------|--------|
| Endpoint | `POST api.printables.com/graphql/` — `searchPrints2(query:, limit:)` |
| Auth | None required. Public API. |
| License metadata | `license { name }` returned by the API (`LicenseType` exposes no url) |
| Files | `print(id:){stls{name filePreviewPath}}`; files hosted on files.printables.com. |
| Access method | The same GraphQL query the printables.com frontend issues. No scraping: the HTML search page is behind a Cloudflare challenge and is no longer requested at all. |
| ToS risk | **LOW** — public API, no authentication bypass, no challenge circumvention. |
| Precedent | hiQ v. LinkedIn (9th Cir. 2022): accessing publicly available website data is not a CFAA violation. |

### 5. Thingiverse (UltiMaker)

Disabled. Thingiverse requires OAuth app registration but the developer portal was
removed in the 2025 site migration. API still functions but new app tokens cannot
be obtained. Re-evaluate when developer portal returns.

### 6. GrabCAD (Stratasys)

Disabled. Both v1 and v2 REST APIs return 404. Public API fully retired.

---

## Key Legal Framework

### No CFAA / Computer Misuse Violation

All 4 adapters access **publicly available** endpoints without:
- Circumventing any authentication or access control
- Extracting credentials or session tokens
- Password cracking or credential stuffing
- Exceeding authorized access

The hiQ v. LinkedIn precedent (9th Circuit, 2022) established that accessing publicly
available data on a website is not a violation of the Computer Fraud and Abuse Act (CFAA),
even when the website owner objects. All our endpoints are reachable without authentication.

### No Copyright Infringement

- We do NOT host, cache, mirror, or redistribute any 3D model files
- We display license metadata before download (legal requirement for CC licenses)
- Downloaded files are stored on the user's local machine
- The user is responsible for complying with each model's license

### GDPR / Data Privacy

- No user data is collected, stored, or transmitted
- Search queries go directly from the plugin to the platform's API
- No analytics, no tracking, no telemetry
- No cookies or persistent identifiers

---

## User Responsibility

**Every download is the user's own act, and the user's own responsibility.**

The plugin is a search tool. It does not hold an account on any of these platforms, does
not authenticate on the user's behalf, and does not obtain any right to any model. Where a
platform gates its files behind a login — MakerWorld, Nexprint and Makeronline all do —
the user signs in with their own credentials, under their own account, and the download
happens under the terms that platform extends to *them*. The plugin is not a party to it.

That means, explicitly:

- **The licence binds the user, not the plugin.** Attribution, non-commercial limits,
  no-derivatives clauses and share-alike obligations attach to whoever downloads and uses
  the file. The plugin displays the licence so the user can honour it; it cannot honour it
  for them.
- **The platform's terms of use bind the user.** Signing in creates an agreement between
  the user and the platform. Any account-level consequence of what is done with a
  downloaded file — including suspension — falls on the account holder.
- **Credentials stay with the user.** Any token entered is used only to call that platform
  and is never transmitted anywhere else, collected, or shared.
- **The plugin supplies no warranty and no indemnity.** It is MIT-licensed and, per that
  licence, provided "as is" without warranty of any kind. Licence metadata is reproduced
  as the platform publishes it; where a platform reports it wrongly or not at all, the
  authoritative source is the model's own page, which every result links to.

## Disclaimer of Liability for Copyright Infringement

**The authors and contributors of this plugin disclaim all responsibility and all
liability for any download or use of a copyright-protected design carried out against the
rights holder's terms.**

The plugin performs a keyword search against public endpoints and returns what those
platforms publish. It does not host, mirror, cache, re-host or redistribute any model
file; it does not review, moderate or verify what a platform lists; and it cannot
determine whether a given listing was uploaded with the rights holder's permission. A
design may be listed on a platform in breach of someone's copyright without the plugin
having any means of knowing it.

Accordingly:

- **Selecting and downloading a model is the user's decision and the user's act.** If that
  design is protected by copyright and the download or subsequent use — printing, sharing,
  modifying, selling — exceeds what the rights holder permits, the resulting liability is
  the user's alone.
- **No liability is accepted by the authors or contributors** for infringement, for any
  claim brought by a rights holder, or for any direct, indirect, incidental or
  consequential damages arising from a user's download or use of any model. This is
  consistent with the MIT licence's exclusion of warranty and of liability, which governs
  this software.
- **Licence metadata is informational, not a guarantee of title.** It is reproduced as the
  platform publishes it. It reflects what the *uploader* declared, which is not proof that
  the uploader held the rights to declare it. Where the metadata is absent, wrong, or
  disputed, the model's own page — linked from every result — is authoritative, and the
  rights holder is the only definitive source.
- **Rights holders**: this project stores and serves no model content, so there is nothing
  here to take down. Requests concerning a specific model must be directed to the platform
  hosting it — MakerWorld, Nexprint, Makeronline or Printables — which is the party in a
  position to act. If a search adapter is nonetheless implicated, open an issue on the
  repository.

Nothing in this document is legal advice.

## Implemented Safeguards

1. **License display** — every result shows the license name, its plain-English summary,
   and a link to the full terms
2. **License shown before download** — the detail panel is the only route to the import
   button, so the license and the responsibility notice below it are on screen before it
   can be pressed
3. **Responsibility notice** — stated in the detail panel, next to the license, on every
   model
4. **No caching** — downloads go to the plugin's local directory only
5. **No redistribution** — files are never uploaded or shared
6. **No credential handling** — the plugin never stores, transmits or proxies a user's
   platform login
7. **Rate limiting** — reasonable request intervals (30s timeout, sequential queries)

---

## Risk Matrix

| Risk | Severity | Mitigation | Status |
|------|----------|------------|--------|
| User downloads ARR model without attribution | HIGH | License and responsibility notice displayed before download | Implemented |
| Platform ToS violation (scraping) | MEDIUM | Public APIs only — no HTML scraping anywhere; no auth bypass; reasonable rates | Implemented |
| Login-gated files fetched without a right to them | MEDIUM | Platforms that gate their files are not offered at all; there is no login, no cookie reuse and no browser hand-off | Implemented |
| Mass copyright infringement | MEDIUM | No bulk download; license shown first; liability disclaimer | Implemented |
| Anti-bot blocking | LOW | Rate limiting; user-agent header | Implemented |
| GDPR violation | LOW | No data collection | Verified |
| OrcaSlicer ToS violation | LOW | Plugin is separate work (AGPL-compatible MIT license) | N/A |

---

## License

Plugin licensed under MIT. OrcaSlicer is AGPL-3.0. Python plugins loaded at runtime
via OrcaSlicer's plugin system are separate works — not derivative works. The plugin
imports `orca` as an API boundary (standard plugin model under copyright law).
