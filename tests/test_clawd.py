"""Clawd's pak: the release download and its pin, the .sig, and that his Lua loads."""
import hashlib
import os
import shutil
import subprocess

import pytest

from crystalize import clawd

REPO = os.path.join(os.path.dirname(__file__), "..")
LUA = shutil.which("lua5.4") or shutil.which("lua")


@pytest.fixture(autouse=True)
def cache(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


@pytest.fixture
def release(tmp_path):
    """A pak served from a file:// URL, as the GitHub release serves the real one."""
    path = tmp_path / "release" / "Clawd_P.pak"
    path.parent.mkdir()
    path.write_bytes(b"clawd" * 1000)
    return path.as_uri(), hashlib.sha256(path.read_bytes()).hexdigest(), path


def test_fetch_downloads_checks_and_caches(release):
    url, digest, source = release
    path = clawd.fetch(url, digest)
    assert open(path, "rb").read() == source.read_bytes()
    source.unlink()  # a second fetch must not need the release
    assert clawd.fetch(url, digest) == path


def test_fetch_refuses_a_different_file(release):
    url, _, _ = release
    with pytest.raises(clawd.ClawdError, match="not the Clawd this Crystalize expects"):
        clawd.fetch(url, "0" * 64)
    assert [f for f in os.listdir(clawd.cache_dir())] == []  # nothing kept, not even partly


def test_fetch_refetches_a_damaged_cache(release):
    url, digest, _ = release
    path = clawd.fetch(url, digest)
    with open(path, "ab") as f:
        f.write(b"damage")
    assert clawd.fetch(url, digest) == path
    assert clawd.sha256(path) == digest


def test_fetch_without_the_release(tmp_path):
    with pytest.raises(clawd.ClawdError, match="could not download"):
        clawd.fetch((tmp_path / "missing.pak").as_uri(), "0" * 64)


def test_release_url_names_the_pinned_tag():
    assert clawd.RELEASE in clawd.URL and clawd.URL.endswith("/Clawd_P.pak")
    assert len(clawd.SHA256) == 64


def test_signature_layout():
    data = clawd.signature(b"x" * (clawd.CHUNK + 1))
    assert data[:4] == clawd.SIG_MAGIC.to_bytes(4, "little")
    assert data[12:12 + 512] == clawd.KNOWN_GOOD_HASH
    assert len(data) == 12 + 512 + 4 + 2 * 4  # header, hash, count, two chunks


@pytest.mark.skipif(LUA is None, reason="needs lua 5.4")
def test_clawd_lua_loads():
    main = os.path.join(REPO, "crystalize", "mod", "Clawd", "Scripts", "main.lua")
    subprocess.run([LUA, "-e", f"assert(loadfile({main!r}))"], check=True)


class FakeGitHub:
    """The release as a private repository serves it: 404 to anyone not signed in, the
    file through the API to a token."""

    def __init__(self, data):
        self.data, self.seen = data, []

    def __call__(self, request, timeout=None):
        import io
        import json
        import urllib.error
        url, auth = request.full_url, request.get_header("Authorization")
        self.seen.append((url, auth))
        if url == clawd.URL:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        if auth != "Bearer secret":
            raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)
        if url.endswith(f"/releases/tags/{clawd.RELEASE}"):
            body = json.dumps({"assets": [{"name": clawd.ASSET, "url": "https://api.example/asset/1"}]})
            return io.BytesIO(body.encode())
        return io.BytesIO(self.data)


def test_private_release_through_the_api(monkeypatch):
    data = b"private clawd" * 100
    github = FakeGitHub(data)
    monkeypatch.setattr(clawd.urllib.request, "urlopen", github)
    monkeypatch.setenv("GITHUB_TOKEN", "secret")
    path = clawd.fetch(clawd.URL, hashlib.sha256(data).hexdigest())
    assert open(path, "rb").read() == data
    assert github.seen[0] == (clawd.URL, None)  # anonymous first
    assert github.seen[-1] == ("https://api.example/asset/1", "Bearer secret")


def test_private_release_without_a_token(monkeypatch):
    monkeypatch.setattr(clawd.urllib.request, "urlopen", FakeGitHub(b""))
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(clawd.shutil, "which", lambda name: None)
    with pytest.raises(clawd.ClawdError, match="gh auth login"):
        clawd.fetch(clawd.URL, "0" * 64)
