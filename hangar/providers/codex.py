"""Link ChatGPT through the OpenAI Codex CLI ("Sign in with ChatGPT", no API key).

Codex isn't in the image. Picking ChatGPT downloads the pinned Codex release from GitHub, checks it
against the sha256 below, and unpacks it under Hangar's data folder, which survives container rebuilds.

Sign-in uses Codex's device code (a link plus a one-time code), which the person must first turn on in
ChatGPT's Security settings. The fallback is Codex's browser sign-in: it redirects to localhost:1455,
which fails to load on another device, so the person pastes that address back and Hangar opens it here.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import tarfile
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

from ..config import settings
from .pty_run import PtyRun, clean_env

VERSION = "0.162.1"
# HANGAR_CODEX_DOWNLOAD_BASE points at a mirror or a local folder (file:///...). The checksum still applies.
DOWNLOAD_BASE = (os.environ.get("HANGAR_CODEX_DOWNLOAD_BASE")
                 or f"https://github.com/openai/codex/releases/download/rust-v{VERSION}").rstrip("/")
SHA256 = {
    "x86_64": "a676f5722aae0d86cbe3764332f08eab1fdd89dc0a300b3ad4ffbf48611fa3e3",
    "aarch64": "53a9ce0b94beb27a63fdeb2f7704d95199e17b3898aa33b60461a1a93ac0d88b",
}
DOWNLOAD_MB = 163
DEVICE_SETTINGS_URL = "https://chatgpt.com/#settings/Security"

_DEVICE_URL = re.compile(r"https://auth\.openai\.com/\S*device\S*")
_DEVICE_CODE = re.compile(r"\b([A-Z0-9]{4}-[A-Z0-9]{4,6})\b")
_BROWSER_URL = re.compile(r"https://auth\.openai\.com/oauth/authorize\?\S+")
_OK = re.compile(r"Successfully logged in", re.I)

_lock = threading.Lock()
_install: dict = {"state": "idle"}
_flow: PtyRun | None = None
_flow_info: dict = {}


# ---------- install ----------

def arch() -> str | None:
    m = platform.machine().lower()
    return {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}.get(m)


def install_dir() -> Path:
    return settings.data_dir / "tools" / "codex" / VERSION


def binary() -> str | None:
    """Hangar's pinned copy, else a codex already on PATH."""
    mine = install_dir() / "bin" / "codex"
    if mine.exists():
        return str(mine)
    return shutil.which("codex")


def install_state() -> dict:
    return dict(_install)


def start_install() -> dict:
    with _lock:
        if binary():
            return {"state": "done"}
        if _install.get("state") in ("downloading", "verifying", "unpacking"):
            return install_state()
        a = arch()
        if not a:
            raise RuntimeError(f"Codex has no Linux build for {platform.machine()}.")
        _install.clear()
        _install.update({"state": "downloading", "done_bytes": 0, "total_bytes": DOWNLOAD_MB * 1_000_000})
        threading.Thread(target=_install_worker, args=(a,), daemon=True).start()
        return install_state()


def _install_worker(a: str) -> None:
    name = f"codex-package-{a}-unknown-linux-musl.tar.gz"
    tools = install_dir().parent
    tools.mkdir(parents=True, exist_ok=True)
    part = tools / f".{name}.part"
    staging = tools / f".{VERSION}.unpacking"
    try:
        digest = hashlib.sha256()
        with urllib.request.urlopen(f"{DOWNLOAD_BASE}/{name}", timeout=60) as r, open(part, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            if total:
                _install["total_bytes"] = total
            while chunk := r.read(1 << 20):
                f.write(chunk)
                digest.update(chunk)
                _install["done_bytes"] += len(chunk)
        _install["state"] = "verifying"
        if digest.hexdigest() != SHA256[a]:
            raise RuntimeError("The download doesn't match the expected checksum, so it wasn't installed.")
        _install["state"] = "unpacking"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir()
        with tarfile.open(part) as t:
            t.extractall(staging, filter="data")
        meta = json.loads((staging / "codex-package.json").read_text())
        if meta.get("version") != VERSION or not (staging / "bin" / "codex").exists():
            raise RuntimeError("The Codex package isn't laid out as expected.")
        (staging / "bin" / "codex").chmod(0o755)
        shutil.rmtree(install_dir(), ignore_errors=True)
        staging.rename(install_dir())
        _install["state"] = "done"
    except Exception as e:  # noqa: BLE001 - shown to the person as is
        _install.update({"state": "error", "error": str(e)})
        shutil.rmtree(staging, ignore_errors=True)
    finally:
        part.unlink(missing_ok=True)


# ---------- sign-in ----------

def _env() -> dict[str, str]:
    return clean_env({"CODEX_HOME": str(settings.codex_home)})


def ensure_config() -> None:
    """Codex must keep its login in a file: the container has no OS keyring."""
    settings.codex_home.mkdir(parents=True, exist_ok=True)
    cfg = settings.codex_home / "config.toml"
    text = cfg.read_text() if cfg.exists() else ""
    if not re.search(r"^\s*cli_auth_credentials_store\s*=", text, re.M):
        # Top-level keys must come before any [table], so put it first.
        cfg.write_text('cli_auth_credentials_store = "file"\n' + text)


def _claims(jwt: str) -> dict:
    """Reads an id_token's payload for display only (Codex itself verifies its tokens)."""
    try:
        body = jwt.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except (IndexError, ValueError):
        return {}


def status() -> dict:
    out = {"installed": bool(binary()), "version": VERSION, "download_mb": DOWNLOAD_MB, "install": install_state(),
           "linked": False, "folder": str(settings.codex_home), "device_settings_url": DEVICE_SETTINGS_URL}
    auth = settings.codex_home / "auth.json"
    try:
        data = json.loads(auth.read_text())
    except (OSError, ValueError):
        data = None
    if data and (data.get("tokens") or data.get("OPENAI_API_KEY")):
        claims = _claims((data.get("tokens") or {}).get("id_token") or "")
        openai = claims.get("https://api.openai.com/auth") or {}
        out.update({"linked": True, "method": data.get("auth_mode") or ("apikey" if data.get("OPENAI_API_KEY") else None),
                    "email": claims.get("email"), "plan": openai.get("chatgpt_plan_type")})
    if out["linked"] and _flow and _flow.done:
        cancel_login()  # finished on its own (device code); nothing left to show
    out["login"] = _flow_state()
    return out


def _scan() -> None:
    """Pulls the link (and device code) out of the CLI's output into _flow_info."""
    text = _flow.text
    if _flow_info["mode"] == "device" and not _flow_info.get("code"):
        d = _DEVICE_URL.search(text)
        c = _DEVICE_CODE.search(text, d.end()) if d else None
        if d and c:
            _flow_info.update({"url": d.group(0), "code": c.group(1)})
            return
    if not _flow_info.get("url") or (_flow_info["mode"] == "device" and not _flow_info.get("code")):
        b = _BROWSER_URL.search(text)
        if b:
            # Without device codes, Codex falls back to the browser sign-in by itself.
            _flow_info.update({"mode": "browser", "url": b.group(0)})


def _flow_state() -> dict | None:
    if not _flow:
        return None
    _scan()
    info = dict(_flow_info)
    text = _flow.text
    info["running"] = not _flow.done
    info["succeeded"] = bool(_OK.search(text)) or (_flow.done and _flow.exit_code == 0)
    if _flow.done and not info["succeeded"]:
        info["error"] = " ".join(ln.strip() for ln in text.splitlines() if ln.strip())[-400:]
    return info


def start_login(mode: str = "device") -> dict:
    global _flow
    if mode not in ("device", "browser"):
        raise ValueError("mode must be device or browser")
    exe = binary()
    if not exe:
        raise RuntimeError("Install Codex first.")
    with _lock:
        cancel_login()
        ensure_config()
        args = [exe, "login", "--device-auth"] if mode == "device" else [exe, "login"]
        _flow = PtyRun(args, _env(), cwd=str(settings.codex_home))
        _flow_info.update({"mode": mode})
        end = time.time() + 30
        while time.time() < end and not _flow.done:
            _scan()
            if _flow_info.get("url") and (_flow_info["mode"] == "browser" or _flow_info.get("code")):
                break
            time.sleep(0.1)
        state = _flow_state()
        if not state.get("url"):
            text = _flow.text
            cancel_login()
            raise RuntimeError("Codex didn't print a sign-in link. " + " ".join(text.split())[-300:])
        return state


def finish_browser_login(pasted: str) -> dict:
    """Opens the localhost:1455 callback address the person copied from their browser."""
    u = urllib.parse.urlsplit((pasted or "").strip())
    q = urllib.parse.parse_qs(u.query)
    if (u.scheme != "http" or u.hostname not in ("localhost", "127.0.0.1") or u.port != 1455
            or u.path != "/auth/callback" or "code" not in q or "state" not in q):
        raise ValueError("Paste the whole address from the page that failed to load. "
                         "It starts with http://localhost:1455/auth/callback?code=")
    with _lock:
        if not _flow or _flow.done or _flow_info.get("mode") != "browser":
            raise RuntimeError("The sign-in expired. Start again.")
        target = f"http://127.0.0.1:1455/auth/callback?{u.query}"
        try:
            urllib.request.urlopen(target, timeout=30).read(2048)
        except Exception:  # noqa: BLE001 - the callback page may redirect or close early; the CLI decides
            pass
        _flow.wait_for(_OK, 30)
        _flow.wait(10)
        state = _flow_state()
        if not state["succeeded"]:
            raise RuntimeError(state.get("error") or "Sign-in didn't finish. Start again.")
        return status()


def cancel_login() -> None:
    global _flow
    if _flow:
        _flow.stop()
    _flow = None
    _flow_info.clear()


def logout() -> dict:
    exe = binary()
    if exe:
        subprocess.run([exe, "logout"], capture_output=True, text=True, timeout=30, env=_env())
    return status()

