# backend/app/updates — private, file-based updates (core)

The release builder emails a signed `FinTrack-X.Y.Z.ftupdate`; FinTrack finds it in the user's
Downloads folder or the user picks it in Settings. (The original design notes are not in the
repo; this file and SPEC.md describe the updater as built.)
`service.py` (`Updater`) and `install.py` (`InstallJob`, the install-root `state.json`
handshake) wire the core into the app (`routers/update.py`, `main.py`, `Vault.unlock`); the
API as built is in SPEC.md, "Private updates (D5)". The other modules are the pure core.

**File format v2 (replaces the first design's "zip with three entries"; no v1 was ever deployed).**
Gmail blocks zips that contain `.js` files, so the file is a plain container, not a zip:
`b"FTUPD\x00\x02"` + three sections, each a uint32 little-endian length then the bytes:
`manifest.json` (<= 64 KB, `schema: 2`), the signature JSON (<= 1 KB, canonical: sorted keys,
no spaces, canonical base64) and the encoded payload (<= 150 MB). The lengths must fill the
file exactly. The signature is Ed25519 over `b"FinTrack update manifest v2\x00" + manifest`.
The payload is `payload.zip` XORed with a fixed public keystream (SHAKE-256 of a constant
label and a block counter; `package.encode_payload`). **That encoding is not encryption**: it
only keeps `PK` signatures, `.js` names and `<script` out of the bytes a mail filter sees.
`payload.sha256`/`size` in the signed manifest cover the encoded bytes; the hash is checked
before decoding, and no zip parsing happens before the signature and hash verified. Full
check order: `package.py` docstring.

| Module | What |
|---|---|
| `semver.py` | strict `MAJOR.MINOR.PATCH`, compare, display (`1.4.0` → `1.4`) |
| `package.py` | `verify_package(path, current_version, *, trusted_keys=None, env=None)` → `VerifiedPackage` or `UpdateRejected`; `extract_payload(...)` → `ExtractedVersion` |
| `requirements.py` | `Environment` + `check()` → help reason (reinstall, python, launcher, skipped_version, runtime_missing, disk_space) |
| `mode.py` | `updater_mode(...)` / `mode_for_settings(settings, ...)` → enabled / git / dev / not_installed; `support_contact()` |
| `store.py` | `UpdateStore(data_dir)`: `data/updates/state.json` (offer, Not-now dates, seen cache, history, failed ids, last_result), launcher result ingest; every id re-validated on load |
| `downloads.py` | `DownloadsScanner` (throttled Downloads scan, copy-then-verify into `data/updates/inbox/`), `accept_picked_file`, Known Folder lookup |
| `install.py` | `InstallJob` (backup → extract → `pending` → exit 75), install-root `state.json` read/write, pruning, rollback targets |
| `service.py` | `Updater`: mode, status, scan, picked files, Not now, install, finish after unlock, FT-UPD-05 rollback request, manual rollback |
| `source.py` | `decide`/`source_for_settings` → `file` / `github` / `off` from the version's release.json (`update_source`, `update_repo`) and `UPDATE_SOURCE`; `valid_repo` |
| `github.py` | GitHub Releases: `latest_release`, `download` (HTTPS + host allowlist on every hop, manual redirects, caps, head signature check before writing), `ERROR_TEXT`; injected transport |
| `../update_keys.py` | `TRUSTED_KEYS` (file source) and `GITHUB_KEYS` (github source, shipped empty); `keys_for(source)` never mixes them; tests use ephemeral keys |

Release tooling: `tools/release/` (keygen, build_update, release.ps1, RELEASE_CHECKLIST.md).

**Iron Owl 2.0.0, updates from GitHub:** a copy whose build wrote `update_source: "github"` checks
GitHub once a day while unlocked (switch in Settings, stored in `state.json` `check`), downloads the
`Iron-Owl-X.Y.Z.ftupdate` asset of the latest release when newer, verifies it with the GitHub keys
only, and offers it (`source: "github"`); the install job is unchanged. Contract: SPEC.md "M6. Updates from GitHub".

## Integration points (done; kept as the map from core to app)

- **Config (B0):** `settings.install_root` (FINTRACK_INSTALL_ROOT), `settings.support_contact`;
  `mode_for_settings` already reads `install_root` via `getattr`. Updater mode must be computed
  once at startup and passed to every call below.
- **Lifespan:** `UpdateStore(settings.data_dir)` on `app.state`; if mode is `enabled`:
  `store.ingest_launcher_result()`, `store.drop_stale_offer(__version__)`. `cleanup_staging`
  already removes `data/updates/.incoming-*` and `data/.update-*`.
- **Scanner:** one `DownloadsScanner(store, current_version=__version__, env=Environment(install_root, data_dir))`
  on `app.state`; call `scanner.scan(mode)` from `POST /api/update/scan` (unlocked) and from
  `automation_loop` (unlocked only). Never at startup or while locked. It is throttled (30 s)
  and single-flight.
- **Router `routers/update.py`:** `status` (build UpdateStatus from `store.offer()` via
  `public_offer`, `store.banner(date.today(), install_running=...)`, `store.last_result()`,
  `store.last_update_at()`), `scan`, `file` (multipart via a factored `backup.receive_file`
  into `downloads.new_incoming_dir(store)`, then `downloads.accept_picked_file`; add the path to
  `security.MULTIPART_PATHS`), `dismiss` (`store.dismiss(id, date.today())`, `StaleOffer` → 404),
  `install`, `progress`, `result/ack` (`store.ack_result`). 409 for every non-enabled mode.
  Responses carry leaf names only; logs carry codes/types only.
- **Install job:** backup (under the vault lock) → `store.offer_package()` →
  `package.extract_payload(path, __version__, install_root/"versions", env=Environment(...),
  runtimes_dir=install_root/"runtimes", compile_bytecode=True)` (it re-verifies and extracts
  from the one in-memory copy it read; `UpdateRejected`/`ExtractError` → FT-UPD-02,
  `NotInstallable` → 409 needs_help) → **compare `extracted.verified.id` and
  `extracted.manifest.version` to the offer's `id`/`version` (mismatch → FT-UPD-02, remove the
  extracted folder)** → write `pending` (with the offer `id`) into the install root
  `state.json` atomically → `request_exit(75)`.
- **Failed ids are never offered again (store side done):** the install job calls
  `store.record_failed(offer_id=..., version=..., code=..., at=...)` only when re-verification
  refused the file itself (signature / hash; `install.file_refused`), never for FT-UPD-01 or a
  disk/file error. The launcher copies `pending.id` into `update_result.json` as `"id"`;
  `ingest_launcher_result` records FT-UPD-03/05 `failed`/`rolled_back` results with an id (and keeps `id` out of
  `last_result`). `set_offer` refuses a failed id; the scan marks such files `failed`; a picked
  copy returns `{"result": "failed", "code", "file_name", "file_version", "current_version"}`
  (**frontend `UpdateCheck` needs this variant**: "This update didn't install before. Ask
  {support contact}."). A newer release is a different file, so a different id, and is offered normally.
- **Unlock hook:** first successful unlock while pending → `pending` cleared first, then
  (best effort) `store.add_history(...)`, `store.set_last_result({outcome: installed, ...})`
  and pruning; a migration/schema failure (not a transient sqlite/OS error) while pending and
  before any successful open in this process → rollback request (FT-UPD-05). Recover opens
  and upgrades before re-keying, so a rollback never leaves the sheet out of step.
- **Offer lock:** picked-file checks, scans and the start of an install are serialized;
  `store.hold_offer` keeps the running install's offer from being replaced.
- **Testing only:** `FINTRACK_DOWNLOADS_DIR` (process environment; the launcher sets it only
  with `--test-downloads-dir`) replaces the Downloads folder.

## Known gaps / notes

- The package is read once (unbuffered) into memory and extraction uses only that verified
  copy, so changing the file on disk mid-install has no effect. Memory use is about one
  payload (<= 150 MB for a full update; an app update is < 10 MB).
- Extraction writes through `\\?\` long paths (Windows' 260-character limit doesn't apply);
  the build also keeps every payload name to 140 characters.
- Downloads files that are any kind of reparse point are skipped, including fully downloaded
  OneDrive files; the user can still pick them in Settings.
- `env=None` in `verify_package` skips the install-root checks (runtime, launcher, disk); the
  install path must always pass an `Environment`.
