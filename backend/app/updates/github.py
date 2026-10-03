"""Updates from GitHub Releases (update source ``github``; see ``source.py``).

One check: ask GitHub's REST API for the latest published release of the repo the build
named, pick its ``Iron-Owl-X.Y.Z.ftupdate`` asset, and only when X.Y.Z is newer than this
version download it into a private ``data/updates/.incoming-*`` folder. The caller then
verifies the copy with the GitHub key set (``package.verify_package`` via
``downloads.check_staged_file``) and offers it; the install itself is the same job as for an
emailed file. Nothing is extracted, imported or run here.

Network rules (every request, every redirect hop):
- HTTPS only, to an allowlisted GitHub host (exact name, port 443, no user:password);
  redirects are followed by hand (at most 5) and each new address is checked again.
- No cookies, no auth, nothing about the user or their money: a fixed User-Agent, an Accept
  header and GitHub's API version header. Only the repo name and the release asset address
  are ever sent.
- Timeouts on every socket operation and an overall time limit for the download.
- Size caps while streaming: 1 MB for the API answer, 150 MB (and the asset's declared size)
  for the file. The first block of the file must pass ``package.check_head`` (magic, lengths,
  signature with the GitHub keys) before a single byte is written to disk.
- The API answer is parsed defensively; anything unexpected is "bad answer".

Errors become ``CheckFailed(code)``; ``ERROR_TEXT[code]`` is the plain-words message the
Settings card shows. Exception text (which can hold paths or addresses) is never shown or
logged; logs carry the code and the exception type only. The transport is injected, so tests
never touch the network.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import logging
import re
import socket
import ssl
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from urllib.parse import urljoin, urlsplit

from . import package, semver
from .package import UpdateRejected
from .source import valid_repo

log = logging.getLogger("fintrack.updates")

API_HOST = "api.github.com"
# github.com serves the asset address and redirects it to GitHub's release-asset storage.
ALLOWED_HOSTS = frozenset({
    "api.github.com",
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
    "github-releases.githubusercontent.com",
})
USER_AGENT = "Iron-Owl-updater"
API_VERSION = "2022-11-28"
ASSET_PREFIX = "Iron-Owl-"
ASSET_SUFFIX = ".ftupdate"
MAX_JSON_BYTES = 1024 * 1024
MAX_ASSETS = 100
MAX_REDIRECTS = 5
MAX_URL_CHARS = 4096
TIMEOUT_SECONDS = 30.0
API_SECONDS = 60.0  # the whole "latest release" request, however slowly it trickles in
DOWNLOAD_SECONDS = 20 * 60.0
READ_CHUNK = 64 * 1024
REDIRECTS = frozenset({301, 302, 303, 307, 308})
_TAG_RE = re.compile(r"^v?([0-9]{1,6}\.[0-9]{1,6}\.[0-9]{1,6})$")
# ASCII digits only: str.isdigit() also accepts "²" and other digits int() refuses.
_LENGTH_RE = re.compile(r"^[0-9]{1,12}$")

# Plain words for the Settings card (no codes, no addresses).
ERROR_TEXT: dict[str, str] = {
    "no_keys": "Updates can’t be checked yet. This copy of Iron Owl doesn’t have the key it needs "
               "to make sure an update is real.",
    "offline": "Iron Owl couldn’t reach GitHub to check for updates. It will try again later.",
    "busy": "GitHub asked Iron Owl to wait before checking again. It will try again later.",
    "bad_answer": "GitHub’s answer didn’t look right, so nothing was downloaded. "
                  "Iron Owl will try again later.",
    "too_large": "The new version’s file is too big to be an Iron Owl update, so it wasn’t downloaded.",
    "rejected": "The new version didn’t pass Iron Owl’s safety check, so it wasn’t installed. "
                "Your data wasn’t changed.",
    "disk": "Iron Owl couldn’t save the new version on this PC. It will try again later.",
}
ERROR_CODES = frozenset(ERROR_TEXT)


class CheckFailed(Exception):
    """A check that couldn't finish. ``str(exc)`` is only the code (safe to log)."""

    def __init__(self, code: str) -> None:
        assert code in ERROR_CODES
        self.code = code
        super().__init__(code)


# ------------------------------------------------------------------ transport


class Response(Protocol):
    status: int

    def header(self, name: str) -> str | None: ...

    def read(self, n: int) -> bytes: ...

    def close(self) -> None: ...


Transport = Callable[[str, Mapping[str, str], float], Response]


@dataclass
class _UrllibResponse:
    status: int
    _headers: object
    _body: object

    def header(self, name: str) -> str | None:
        get = getattr(self._headers, "get", None)
        value = get(name) if get else None
        return value if isinstance(value, str) else None

    def read(self, n: int) -> bytes:
        # read1: whatever has arrived (at most n), so a slow trickle can't hold one read() for
        # long; the callers' overall deadlines are checked between reads.
        read1 = getattr(self._body, "read1", None)
        return read1(n) if read1 is not None else self._body.read(n)  # type: ignore[attr-defined]

    def close(self) -> None:
        try:
            self._body.close()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass


def _opener() -> urllib.request.OpenerDirector:
    """HTTPS only, no redirect handler (redirects are followed by hand), no cookie jar."""
    opener = urllib.request.OpenerDirector()
    for handler in (
        urllib.request.ProxyHandler(),  # the system's proxy settings, as a browser would
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        urllib.request.HTTPDefaultErrorHandler(),
        urllib.request.HTTPErrorProcessor(),
        urllib.request.UnknownHandler(),
    ):
        opener.add_handler(handler)
    return opener


def urllib_transport(url: str, headers: Mapping[str, str], timeout: float) -> Response:
    """The real transport (stdlib). A non-2xx answer is returned, not raised."""
    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    try:
        raw = _opener().open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:  # 3xx/4xx/5xx: the caller decides
        return _UrllibResponse(exc.code, exc.headers, exc)
    return _UrllibResponse(raw.status, raw.headers, raw)


# ------------------------------------------------------------------ addresses


def allowed_url(url: object) -> bool:
    """HTTPS to an allowlisted GitHub host on the default port, nothing odd in it."""
    if not isinstance(url, str) or not url or len(url) > MAX_URL_CHARS:
        return False
    if any(ord(ch) < 33 or ord(ch) == 127 for ch in url):
        return False
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    return (
        parts.scheme == "https"
        and parts.username is None
        and parts.password is None
        and (parts.hostname or "").lower() in ALLOWED_HOSTS
        and port in (None, 443)
        and "@" not in parts.netloc
    )


def _get(transport: Transport, url: str, headers: Mapping[str, str]) -> Response:
    """GET with redirects followed by hand; every hop must be ``allowed_url``."""
    for _hop in range(MAX_REDIRECTS + 1):
        if not allowed_url(url):
            log.warning("update check: an address outside GitHub was refused")
            raise CheckFailed("bad_answer")
        try:
            resp = transport(url, headers, TIMEOUT_SECONDS)
        except CheckFailed:
            raise
        except (OSError, socket.timeout, ssl.SSLError, urllib.error.URLError, http.client.HTTPException, ValueError) as exc:
            log.info("update check: GitHub not reached (%s)", type(exc).__name__)
            raise CheckFailed("offline") from None
        if resp.status in REDIRECTS:
            location = resp.header("Location")
            resp.close()
            if not location:
                raise CheckFailed("bad_answer")
            url = urljoin(url, location.strip())
            continue
        return resp
    raise CheckFailed("bad_answer")


def _fail_for_status(status: int) -> CheckFailed:
    if status in (403, 429):
        return CheckFailed("busy")  # rate limit
    if 500 <= status <= 599:
        return CheckFailed("offline")
    return CheckFailed("bad_answer")


# ------------------------------------------------------------------ the latest release


@dataclass(frozen=True)
class Release:
    version: str
    tag: str
    asset_name: str
    asset_id: int
    size: int
    url: str


def _no_constant(_name: str):
    raise ValueError("non-finite number")


def parse_release(raw: bytes, repo: str) -> Release | None:
    """GitHub's "latest release" answer -> the update asset, or None when the release has no
    update file for its version (not an error: e.g. still being uploaded). Raises
    CheckFailed("bad_answer") for anything malformed."""
    try:
        data = json.loads(raw.decode("utf-8"), parse_constant=_no_constant)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise CheckFailed("bad_answer") from None
    if not isinstance(data, dict):
        raise CheckFailed("bad_answer")
    tag = data.get("tag_name")
    m = _TAG_RE.fullmatch(tag) if isinstance(tag, str) else None
    if m is None or not semver.is_valid(m.group(1)):
        raise CheckFailed("bad_answer")
    if data.get("draft") is True or data.get("prerelease") is True:
        return None
    version = m.group(1)
    assets = data.get("assets")
    if not isinstance(assets, list):
        raise CheckFailed("bad_answer")
    want = f"{ASSET_PREFIX}{version}{ASSET_SUFFIX}"
    for asset in assets[:MAX_ASSETS]:
        if not isinstance(asset, dict) or asset.get("name") != want:
            continue
        size, asset_id, url = asset.get("size"), asset.get("id"), asset.get("browser_download_url")
        if (
            not isinstance(size, int) or isinstance(size, bool)
            or not isinstance(asset_id, int) or isinstance(asset_id, bool) or asset_id < 0
            or not isinstance(url, str) or not allowed_url(url)
        ):
            raise CheckFailed("bad_answer")
        parts = urlsplit(url)
        expected_path = f"/{repo}/releases/download/{tag}/{want}"
        if parts.hostname != "github.com" or parts.path.lower() != expected_path.lower() or parts.query:
            raise CheckFailed("bad_answer")
        if size > package.MAX_PACKAGE_BYTES:
            raise CheckFailed("too_large")
        if size < package.MIN_PACKAGE_BYTES:
            raise CheckFailed("bad_answer")
        return Release(version, tag, want, asset_id, size, url)
    return None


def _read_capped(resp: Response, cap: int, *, past_deadline: Callable[[], bool] = lambda: False) -> bytes:
    out = bytearray()
    while True:
        if past_deadline():
            log.info("update check: GitHub's answer took too long")
            raise CheckFailed("offline")
        block = resp.read(READ_CHUNK)
        if not block:
            return bytes(out)
        out += block
        if len(out) > cap:
            raise CheckFailed("bad_answer")


def latest_release(
    transport: Transport,
    repo: str,
    *,
    clock: Callable[[], float] | None = None,
    deadline_seconds: float = API_SECONDS,
) -> Release | None:
    """The newest published release's update asset (None: no release, or no update file).
    The whole answer must arrive within ``deadline_seconds`` (else "offline")."""
    if not valid_repo(repo):
        raise CheckFailed("bad_answer")
    clock = clock or time.monotonic
    started = clock()
    url = f"https://{API_HOST}/repos/{repo}/releases/latest"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": API_VERSION,
    }
    resp = _get(transport, url, headers)
    try:
        if resp.status == 404:
            return None  # no published release yet
        if resp.status != 200:
            raise _fail_for_status(resp.status)
        try:
            raw = _read_capped(resp, MAX_JSON_BYTES, past_deadline=lambda: clock() - started > deadline_seconds)
        except (OSError, socket.timeout, ssl.SSLError, http.client.HTTPException, ValueError) as exc:
            log.info("update check: answer cut off (%s)", type(exc).__name__)
            raise CheckFailed("offline") from None
    finally:
        resp.close()
    return parse_release(raw, repo)


# ------------------------------------------------------------------ the download


@dataclass
class Downloaded:
    path: Path
    size: int
    sha256: str


@dataclass
class _Head:
    """The first bytes, held back until the signature check passed."""

    want: int
    buf: bytearray = field(default_factory=bytearray)
    checked: bool = False


def download(
    transport: Transport,
    release: Release,
    dest: Path,
    trusted_keys: Mapping[str, str],
    *,
    clock: Callable[[], float] = time.monotonic,
    deadline_seconds: float = DOWNLOAD_SECONDS,
) -> Downloaded:
    """Stream the asset into ``dest`` (a new file). Nothing is written before the head
    (magic, section lengths, signature with ``trusted_keys``) checked out; the size may not
    pass the asset's declared size nor 150 MB. Raises CheckFailed; a partial file is the
    caller's to delete (it lives in a private folder)."""
    headers = {"Accept": "application/octet-stream", "User-Agent": USER_AGENT}
    started = clock()
    cap = min(release.size, package.MAX_PACKAGE_BYTES)
    resp = _get(transport, release.url, headers)
    try:
        if resp.status != 200:
            raise _fail_for_status(resp.status)
        length = resp.header("Content-Length")
        if length is not None:
            length = length.strip()
            if not _LENGTH_RE.fullmatch(length):
                raise CheckFailed("bad_answer")
            if int(length) > package.MAX_PACKAGE_BYTES:
                raise CheckFailed("too_large")
            if int(length) != release.size:
                raise CheckFailed("bad_answer")
        head = _Head(min(release.size, package.HEAD_PREFIX_BYTES))
        digest, total = hashlib.sha256(), 0
        try:
            with open(dest, "xb") as out:
                while True:
                    if clock() - started > deadline_seconds:
                        raise CheckFailed("offline")
                    block = resp.read(READ_CHUNK)
                    if not block:
                        break
                    total += len(block)
                    if total > cap:
                        raise CheckFailed("too_large" if total > package.MAX_PACKAGE_BYTES else "bad_answer")
                    digest.update(block)
                    if not head.checked:
                        head.buf += block
                        if len(head.buf) < head.want:
                            continue
                        _check_head(bytes(head.buf[:head.want]), release.size, trusted_keys)
                        head.checked = True
                        block, head.buf = bytes(head.buf), bytearray()
                    out.write(block)
                if not head.checked:  # shorter than its head: never valid
                    raise CheckFailed("bad_answer")
                out.flush()
        except CheckFailed:
            raise
        except (socket.timeout, ssl.SSLError, urllib.error.URLError, http.client.HTTPException,
                ConnectionError, TimeoutError) as exc:
            log.info("update download stopped (%s)", type(exc).__name__)
            raise CheckFailed("offline") from None
        except OSError as exc:  # our disk (the transport's errors are caught above)
            log.warning("update download not saved (%s)", type(exc).__name__)
            raise CheckFailed("disk") from None
    finally:
        resp.close()
    if total != release.size:
        raise CheckFailed("bad_answer")
    return Downloaded(dest, total, digest.hexdigest())


def _check_head(prefix: bytes, size: int, trusted_keys: Mapping[str, str]) -> None:
    try:
        package.check_head(prefix, size, trusted_keys)
    except UpdateRejected as exc:
        log.warning("update from GitHub refused before download (%s)", exc.code)
        raise CheckFailed("too_large" if exc.reason == "too_large" else "rejected") from None
