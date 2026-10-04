# Notices

casper-network-mcp's own code is under the MIT licence in `LICENSE`. The package also ships material that is not
covered by that licence:

- **HPE Aruba Networking's API documents** (30 New Central and 16 ClearPass OpenAPI documents in
  `src/casper_network_mcp/specs/`). They are proprietary HPE material, not open source. Section A and C below say
  on what basis they are redistributed.
- **Juniper Mist's OpenAPI document** (`mist.openapi.json`), under the MIT licence reproduced in section B below.
- **Files built from those documents**: the generated tool lists in
  `src/casper_network_mcp/openapi_gen/manifests/` and find_tool's word index in
  `src/casper_network_mcp/router/index.json` hold operation ids, paths, parameter names and one-line summaries
  taken from them. What came from HPE's documents stays under the terms in sections A and C; what came from
  Mist's document stays under section B's MIT licence.
- **Code adapted from other MIT projects**: see `THIRD_PARTY_NOTICES.md`.

The rest of this file is `src/casper_network_mcp/specs/NOTICE.md`, word for word (a test keeps the two the same).

---

# Third-party notice — bundled OpenAPI documents

`casper_network_mcp/specs/` holds **47 OpenAPI documents from two upstreams under
two different licensing regimes.** They are not interchangeable, and the
difference matters if you redistribute this package.

| Files | Upstream | Licence | Section |
| --- | --- | --- | --- |
| 30 New Central documents (everything except `mist.openapi.json` and `clearpass-*.json`) | HPE Aruba Networking developer portal | **Proprietary HPE material. Not open source.** | [A](#a--the-30-new-central-documents) |
| `mist.openapi.json` | `mistsys/mist_openapi` on GitHub | **MIT** — full text reproduced below | [B](#b--mistopenapijson) |
| 16 ClearPass documents (`clearpass-*.json`) | HPE Aruba Networking developer portal | **Proprietary HPE material. Not open source.** | [C](#c--the-16-clearpass-documents) |

Every file is stored as the exact bytes the upstream served. `MANIFEST.json`
beside this file records, for each one, the URL actually fetched, the date,
the SHA-256 of those bytes and the number of API paths.
`scripts/refresh_specs.py` fetches them again; `--check` re-fetches and
confirms every hash without writing anything.

---

## A — the 30 New Central documents

### What they are

30 OpenAPI 3.x documents describing the HPE Aruba Networking **New Central**
REST API — 779 API paths in total. They are the API descriptions the
developer portal itself serves to render its reference pages. They are **not**
documentation we wrote, and they are not derived, summarised or regenerated:
each file is the upstream document byte for byte.

### Who publishes them, and where they came from

Hewlett Packard Enterprise / Aruba Networking, at
<https://developer.arubanetworks.com/>. The portal runs on ReadMe, and each
reference page points at an API-registry document.

They were fetched from HPE's public developer-portal API registry by
`scripts/refresh_specs.py`, straight from
`https://dash.readme.com/api/v1/api-registry/<registry_id>`, which needs no
login. The script names this project in its User-Agent and sends no browser
headers and no cookies. Each file's URL, date and sha256 are in
MANIFEST.json; `reference_page` there is the developer-portal page the
document backs. The registry ids are pinned in `scripts/spec_pins.json`.

### Licence and redistribution basis

**These 30 documents are proprietary HPE Aruba Networking material. They are
not open source, and this repository's MIT licence does not extend to them.**

HPE publishes them without an accompanying licence grant and without an
authentication barrier, as the machine-readable form of public API reference
documentation whose entire purpose is to be consumed by API clients. They are
redistributed here verbatim, with attribution and provenance, so that
`lookup_api` answers exact API questions from a clean clone with no network
access — the same use the publisher intends, moved offline.

This is a good-faith reliance on published-for-integration intent, not a
licence. Specifically:

- No warranty and no endorsement by HPE is claimed or implied.
- "HPE", "Aruba", "Aruba Networking" and "New Central" are marks of Hewlett
  Packard Enterprise, used here only to identify the API being described.
- If HPE asks for these documents to be removed, remove them. Nothing outside
  this directory depends on the files being *committed* —
  `scripts/refresh_specs.py` reproduces them from the upstream portal, and
  `scripts/spec_pins.json` keeps the pointers.

Downstream users redistributing this repository inherit that position and
should make their own assessment.

---

## B — `mist.openapi.json`

### What it is

One OpenAPI 3.1.0 document describing the entire **Juniper Mist** REST API —
756 API paths, `info.version` `2607.1.0`. It is the upstream
file byte for byte.

### Who publishes it, and where it came from

Mist Systems / Juniper Networks, at <https://github.com/mistsys/mist_openapi>.

Pinned to commit
[`315b30ff4fa65c1dc3a2b5c1f27931e1b14ed01e`](https://github.com/mistsys/mist_openapi/commit/315b30ff4fa65c1dc3a2b5c1f27931e1b14ed01e)
and fetched by `scripts/refresh_specs.py` from the immutable raw URL

```
https://raw.githubusercontent.com/mistsys/mist_openapi/315b30ff4fa65c1dc3a2b5c1f27931e1b14ed01e/mist.openapi.json
```

A branch URL is deliberately not used: it changes under you, so it could not
pin anything.

### Licence

MIT, per the `LICENSE` file at the pinned commit. Reproduced in full as MIT
requires:

```
MIT License

Copyright (c) 2020 Thomas Munzer

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

Source: <https://raw.githubusercontent.com/mistsys/mist_openapi/315b30ff4fa65c1dc3a2b5c1f27931e1b14ed01e/LICENSE>.
"Juniper", "Mist" and "Marvis" are marks of Juniper Networks, used here only to
identify the API being described. No endorsement by Juniper is claimed.

---

## C — the 16 ClearPass documents

### What they are

16 OpenAPI 3.0 documents describing the HPE Aruba Networking **ClearPass
Policy Manager** REST API (`info.version` 6.12.7) — 335 API paths in total.
They are the API descriptions the developer portal itself serves to render
its ClearPass reference pages. Like section A's files, each one is the
upstream document byte for byte, not documentation we wrote. Their paths are
relative to the ClearPass server's `/api` base.

### Who publishes them, and where they came from

Hewlett Packard Enterprise / Aruba Networking, at
<https://developer.arubanetworks.com/cppm/reference>.

They were fetched the same way as section A's files: by
`scripts/refresh_specs.py`, straight from
`https://dash.readme.com/api/v1/api-registry/<registry_id>`, which needs no
login, with a User-Agent naming this project and no browser headers or
cookies. Each file's URL, date and sha256 are in MANIFEST.json; the registry
ids are pinned in `scripts/spec_pins.json` under `clearpass`.

### Licence and redistribution basis

**These 16 documents are proprietary HPE Aruba Networking material. They are
not open source, and this repository's MIT licence does not extend to them.**

HPE publishes them without an accompanying licence grant and without an
authentication barrier, as the machine-readable form of public API reference
documentation whose entire purpose is to be consumed by API clients. They are
redistributed here verbatim, with attribution and provenance, so that
`lookup_api` answers exact API questions from a clean clone with no network
access — the same use the publisher intends, moved offline.

This is a good-faith reliance on published-for-integration intent, not a
licence. Specifically:

- No warranty and no endorsement by HPE is claimed or implied.
- "HPE", "Aruba", "Aruba Networking" and "ClearPass" are marks of Hewlett
  Packard Enterprise, used here only to identify the API being described.
- If HPE asks for these documents to be removed, remove them. Nothing outside
  this directory depends on the files being *committed* —
  `scripts/refresh_specs.py` reproduces them from the upstream portal, and
  `scripts/spec_pins.json` keeps the pointers.

Downstream users redistributing this repository inherit that position and
should make their own assessment.
