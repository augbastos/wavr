# Third-party notices

Wavr itself is licensed under [AGPL-3.0-or-later](LICENSE).

This file covers the third-party code **vendored into this repository** — copied in as
files and shipped with the product, rather than fetched at build time. Each entry names
what it is, where it came from, which licence it carries, and where the full licence text
lives in this tree.

Everything here is vendored deliberately, for the same reason in every case: Wavr's
privacy claim is that the dashboard and the mobile companion make **no external network
request at runtime**. A CDN `<script src>` would break that claim on the first page load,
and no amount of documentation would make it true again. Self-hosting is what makes the
claim checkable.

Declared *dependencies* — the Python packages in `backend/pyproject.toml`, the npm
packages in `desktop/package.json` and `mobile/package.json`, and the crates in
`desktop/src-tauri/Cargo.toml` — are resolved by their package managers and are not
reproduced here. Their licences travel with the packages themselves.

## Summary

| Component | Version | Licence | Full text |
|---|---|---|---|
| [three.js](#threejs) | r185 (npm `three@0.185.1`) | MIT | `frontend/vendor/three/LICENSE` |
| [jsQR](#jsqr) | see hash below | Apache-2.0 | `mobile/vendor/LICENSE-jsQR` |
| [qrcode-generator](#qrcode-generator) | 2.0.4 / see hash below | MIT | `frontend/vendor/LICENSE-qrcode-generator` |

All three are permissive licences, and combining them into an AGPL-licensed work is the
ordinary direction of travel: the obligations they impose are attribution and preservation
of their notices, which is what this file and the per-file headers exist to satisfy. None
of them is copyleft, so none of them constrains Wavr's own licence. This is a description
of the licences involved, not legal advice.

---

## three.js

- **Files:** `frontend/vendor/three/build/three.core.min.js`,
  `frontend/vendor/three/build/three.module.min.js`,
  `frontend/vendor/three/examples/jsm/controls/OrbitControls.js`
- **Upstream:** https://github.com/mrdoob/three.js
- **Version:** r185 — npm package `three@0.185.1`. Pinned 2026-07-03.
- **Licence:** MIT · SPDX `MIT` · full text in `frontend/vendor/three/LICENSE`
- **Copyright:** Copyright © 2010-2026 three.js authors
- **Modified:** no. Each file carries the upstream `@license` header, plus one added line
  recording the version and that it is self-hosted on purpose.
- **Used for:** the 3D house view (`frontend/js/house3d.js`) and its camera controls.

`OrbitControls.js` is worth a note. Its upstream header was, at one point, *replaced* by a
local vendoring comment — so the file shipped with no attribution and no SPDX identifier at
all, which is precisely the failure this document exists to prevent. The upstream header
has been restored above the vendoring note.

## jsQR

- **File:** `mobile/vendor/jsqr.js`
- **Upstream:** https://github.com/cozmo/jsQR
- **Version:** the bundle declares none, so the honest identifier is a hash: of the
  vendored **body** (everything below the added banner), with line endings normalised
  to LF.
  `sha256 bc40c8a15196236b2314db0856f72ca0b49980cd5413b8c852a7349f5fee0859`
- **Licence:** Apache-2.0 · SPDX `Apache-2.0` · full text in `mobile/vendor/LICENSE-jsQR`
- **Copyright:** upstream ships the stock Apache-2.0 text with its
  `Copyright {yyyy} {name of copyright owner}` field left unfilled, so there is no
  copyright line to carry across. Inventing one would be worse than having none;
  the attribution here is the project name and URL, which is what upstream states.
- **Modified:** no, other than the provenance banner prepended to the file. The banner is
  excluded from the hash above, so the hash still identifies upstream's bytes. Reproduce it
  from the repository root:

  ```bash
  python -c "import hashlib; b = open('mobile/vendor/jsqr.js','rb').read();              i = b.index(bytes([42,47])) + 2;              i += len(b[i:]) - len(b[i:].lstrip(bytes([13,10])));              print(hashlib.sha256(b[i:].replace(bytes([13,10]), bytes([10]))).hexdigest())"
  ```

  Two details are load-bearing and both were got wrong first: `bytes([42,47])` is the
  comment terminator written by code point, because spelling `*/` literally inside the
  banner ended the comment and broke the bundle; and the LF normalisation is what makes
  the number the same on a Windows checkout and a Linux one.
- **Used for:** decoding the hub's pairing QR code **on the phone**. A camera frame must
  never leave the device to reach a decoding service, so this cannot be a hosted API.

This file previously shipped inside the Android APK with **no licence, no copyright and no
provenance of any kind**. Apache-2.0 requires that its notice travel with the code; the
banner and `LICENSE-jsQR` were added to satisfy that, and the licence text was downloaded
from upstream rather than reproduced from memory.

## qrcode-generator

Two copies exist, serving two different surfaces. Both carry the same upstream MIT header
and copyright line.

- **Files:** `frontend/vendor/qrcode.js` (the dashboard), and
  `site/public/assets/qrcode.vendor.js` (the marketing site's download page)
- **Upstream:** https://github.com/kazuhikoarase/qrcode-generator ·
  http://www.d-project.com/
- **Licence:** MIT · SPDX `MIT` · full text in `frontend/vendor/LICENSE-qrcode-generator`
- **Copyright:** Copyright (c) 2009 Kazuhiko Arase
- **Versions:**
  - `site/public/assets/qrcode.vendor.js` — npm `qrcode-generator` v2.0.4 (`dist/qrcode.js`),
    `sha256 869fd34fde6d32890fbb4267ccb82e3e1fdfa53071948c71ba076e8f44c706aa`
  - `frontend/vendor/qrcode.js` — the file declares no version, and it is **not**
    equivalent to the 2.0.4 copy above even after normalising line endings, so it is
    identified by hash rather than by a number that cannot be checked. Same contract as
    jsQR: the body below the banner, normalised to LF.
    `sha256 18ae399f81182bc9de916e9c77b195df20cc58d6f2d55a62b085a299f1bf1780`
- **Modified:** no.
- **Used for:** rendering pairing and download QR codes client-side, with no request to a
  third-party QR service.

**Trademark note, from upstream's own header:** "QR Code" is a registered trademark of
DENSO WAVE INCORPORATED.

---

## What is *not* in this repository

- **YOLO / Ultralytics** (`backend[camera]`) is an optional dependency installed from PyPI,
  not vendored. It is **AGPL-3.0**, and it is the reason the camera path is an *extra*
  rather than a base requirement: a Wavr install without cameras never pulls it in.
- **Model weights** are not committed and not redistributed here. They are downloaded by
  Ultralytics at first use, under their own terms.
- The IEEE OUI data and the device heuristics in `backend/wavr/data/` are compiled from
  public registry data and from this project's own observations, not copied from any
  commercial device-identification product. See `docs/adr/0001` and
  `docs/adr/0004-defensive-only-reject-offensive.md`.

## Reporting an omission

If something here is wrong, or a component is missing its notice, that is a defect and not
a nitpick — open an issue. Attribution that cannot be verified is the same as no
attribution.
