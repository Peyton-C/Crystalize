"""Clawd's model: getting the pak from Crystalize's GitHub release, and its .sig.

The pak is a release download rather than part of the repo: it is 8 MB and is replaced
whenever Clawd is rebuilt. Crystalize pins the release's SHA-256, so each version
installs the Clawd it was tested with and refuses any other file under that name.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import struct
import subprocess
import tempfile
import urllib.error
import urllib.request
import zlib

REPO = "Peyton-C/Crystalize"
RELEASE = "clawd-v1"
ASSET = "Clawd_P.pak"
URL = f"https://github.com/{REPO}/releases/download/{RELEASE}/{ASSET}"
SHA256 = "2a6bcff71ae16e5f7b56cfa416bf0675a0b9d7430bbceb1403c49f4ad9c1ca01"

# The .sig FUSER reads beside a pak, as FuserSongLoader writes its own. The loader lets a
# pak with _P in its name past FUSER's signature check, but FUSER still reads the .sig: a
# magic number, a version, a 512-byte hash every custom song shares, then a CRC-32 for
# each 64 KB of the pak. The hash is from FuserSongLoader's uasset.h (MIT,
# github.com/NarrikSynthfox/FuserSongLoader)
SIG_MAGIC, SIG_VERSION = 0x73832DAA, 1
CHUNK = 64 * 1024

KNOWN_GOOD_HASH = bytes.fromhex(
    "98fed51309e7b985b1937f81c9205f81799b3b2e0541778452b0d2e71377029d5923d32f080690ec16c10b10"
    "9cc31aee093276eab8e7af9f19d000a636577d87719286d524e02201ede5b6b337bd5858a5b76f2160bd2471"
    "86b86b92b803270c70f006995d1023be0b500786ad18881249e16aa6ec1d55ab1ac2327554107c1a19e129f4"
    "c3940eb78da40bc689d952830b94b8425b0d14b2086920a50931837dbd86bc0b353d5703beaaec26eec0783c"
    "3899247bcac5a3bc655a9b0e2c99308b8bd219fd56d96cf0f8feb4f87ed6bc6ba2d3ef933f881973c102d5f6"
    "cebd7d2b0e75f7b209781c1cd732018ae8165848943a7b2c3f873cc04b5ee4e8b06e1e445a1abf7244277a27"
    "49f244dcbb8716ffafcd63d1af48ff078098ef227dee225fcb70518b76065394ae76320a826fb17db2970654"
    "9c634a02d0bad5592c019a16a00e573faff4bdbe75073a76d3389a26c697bace2341c68ed80c25e01ea11a42"
    "6250c24df279b832b9bd309fe2ff62a67208df44f8c2fa9ba0e96b6980ef4491bc8b9448d3b056c74d54eaf9"
    "32614d9a5b5d7f9d98a09c9a6d1101bfa6ecd97e4e716ac732d5102658c23abda94d78203ffedb6eea06afa6"
    "aaaf56c6953a0e89a3529009ec63da752065aff9bc6cd95f00ee222f554c9c2efe6a1f3dcccf24d518259b81"
    "0525ad2421c26cc4148b52ea2b2c32575db4de99350649fb457f3aca"
)


class ClawdError(Exception):
    """The pak could not be had, or is not the one Crystalize was built against."""


def signature(pak: bytes) -> bytes:
    chunks = [zlib.crc32(pak[i:i + CHUNK]) for i in range(0, len(pak), CHUNK)]
    return (struct.pack("<III", SIG_MAGIC, SIG_VERSION, len(KNOWN_GOOD_HASH)) + KNOWN_GOOD_HASH
            + struct.pack(f"<I{len(chunks)}I", len(chunks), *chunks))


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def cache_dir() -> str:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "crystalize")


def fetch(url: str = URL, expected: str = SHA256) -> str:
    """The released pak, downloaded once and kept in the cache after its hash checks out."""
    folder = cache_dir()
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"Clawd_P-{expected[:12]}.pak")
    if os.path.exists(path) and sha256(path) == expected:
        return path

    fd, tmp = tempfile.mkstemp(dir=folder, suffix=".part")
    try:
        with os.fdopen(fd, "wb") as out:
            try:
                _download(urllib.request.Request(url), out)
            except urllib.error.HTTPError as e:
                # A private repository's release answers 404 to anyone not signed in. The
                # API serves the same file to a token that can see the repository
                token = _token() if e.code == 404 and url == URL else None
                if token is None:
                    raise ClawdError(_failed(url, e)) from e
                out.seek(0)
                out.truncate()
                try:
                    _download(_api_request(token), out)
                except (OSError, ClawdError) as e2:
                    raise ClawdError(_failed(url, e2)) from e2
            except OSError as e:
                raise ClawdError(_failed(url, e)) from e
        got = sha256(tmp)
        if got != expected:
            raise ClawdError(f"the download from {url} is not the Clawd this Crystalize expects: "
                             f"SHA-256 {got}, not {expected}")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return path


def _download(request, out) -> None:
    with urllib.request.urlopen(request, timeout=60) as response:
        while block := response.read(1 << 20):
            out.write(block)


def _failed(url: str, error) -> str:
    return (f"could not download Clawd from {url}: {error}. If the repository is private, set "
            f"GITHUB_TOKEN or sign in with `gh auth login`")


def _token() -> str | None:
    """A GitHub token from the environment, or from the GitHub CLI if it is signed in."""
    for name in ("GITHUB_TOKEN", "GH_TOKEN"):
        if os.environ.get(name):
            return os.environ[name]
    if shutil.which("gh"):
        result = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    return None


def _api_request(token: str) -> urllib.request.Request:
    """The release asset, asked for through the API, which honours the token."""
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    with urllib.request.urlopen(urllib.request.Request(
            f"https://api.github.com/repos/{REPO}/releases/tags/{RELEASE}", headers=headers),
            timeout=60) as response:
        release = json.load(response)
    asset = next((a for a in release.get("assets", []) if a.get("name") == ASSET), None)
    if asset is None:
        raise ClawdError(f"release {RELEASE} has no {ASSET}")
    return urllib.request.Request(asset["url"], headers={
        "Authorization": f"Bearer {token}", "Accept": "application/octet-stream"})
