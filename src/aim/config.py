"""Paths, identity, and the on-disk config for one machine."""

from __future__ import annotations

import json
import os
import secrets
import socket
import sys
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from aim import __version__

HTTP_PORT = 4777
DISCOVERY_PORT = 4778
AGENTS = ("claude", "grok", "codex")
VERSION = __version__


def home_dir() -> Path:
    return resolve_home(Path.home(), os.environ)


def resolve_home(user_home: Path, env: Mapping[str, str]) -> Path:
    """Profile directory. An earlier install lives in .intravo-messenger until it is moved."""
    override = env.get("AIM_HOME") or env.get("IVM_HOME")
    if override:
        return Path(override)
    current = user_home / ".aim"
    if current.exists():
        return current
    legacy = user_home / ".intravo-messenger"
    if legacy.is_dir():
        return legacy
    return current


def config_path() -> Path:
    return home_dir() / "config.json"


def os_label() -> str:
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        try:
            text = Path("/etc/os-release").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return "linux"
        for line in text.splitlines():
            if line.startswith("ID="):
                return line.split("=", 1)[1].strip().strip('"').lower() or "linux"
        return "linux"
    return sys.platform


def default_name() -> str:
    raw = socket.gethostname().split(".")[0].lower()
    cleaned = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in raw)
    cleaned = cleaned.strip("-")
    return cleaned or "node"


def detect_github_root() -> Path:
    home = Path.home()
    candidates = [
        home / "Documents" / "GitHub",
        home / "Documents" / "github",
        home / "GitHub",
        home / "github",
        home / "source" / "repos",
        home / "Projects",
        home / "projects",
        home / "code",
        home / "src",
    ]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return home / "Documents" / "GitHub"


def new_secret() -> str:
    return secrets.token_hex(32)


@dataclass
class Config:
    node_id: str
    name: str
    github_root: str
    secret: str
    http_port: int = HTTP_PORT
    discovery_port: int = DISCOVERY_PORT
    bind_host: str = "0.0.0.0"
    exec_enabled: bool = True
    agents: list[str] = field(default_factory=lambda: list(AGENTS))
    max_file_bytes: int = 4 * 1024 * 1024 * 1024
    task_timeout_s: int = 600
    max_concurrent_tasks: int = 2
    max_queued_tasks: int = 20
    allow_public: bool = False
    discovery_enabled: bool = True
    pinned_peers: list[dict] = field(default_factory=list)

    def validate(self) -> None:
        if not isinstance(self.secret, str) or len(self.secret) < 32:
            raise ValueError("config secret must be at least 32 characters")
        if not self.name or len(self.name) > 64:
            raise ValueError("config name is missing or too long")
        root = Path(self.github_root)
        if not root.is_dir():
            raise ValueError(f"github_root is not a directory: {root}")
        if not 1 <= int(self.http_port) <= 65535:
            raise ValueError("http_port out of range")
        if not 1 <= int(self.discovery_port) <= 65535:
            raise ValueError("discovery_port out of range")

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "name": self.name,
            "github_root": self.github_root,
            "secret": self.secret,
            "http_port": self.http_port,
            "discovery_port": self.discovery_port,
            "bind_host": self.bind_host,
            "exec_enabled": self.exec_enabled,
            "agents": list(self.agents),
            "max_file_bytes": self.max_file_bytes,
            "task_timeout_s": self.task_timeout_s,
            "max_concurrent_tasks": self.max_concurrent_tasks,
            "max_queued_tasks": self.max_queued_tasks,
            "allow_public": self.allow_public,
            "discovery_enabled": self.discovery_enabled,
            "pinned_peers": list(self.pinned_peers),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Config":
        known = {field_name for field_name in cls.__dataclass_fields__}
        picked = {key: value for key, value in data.items() if key in known}
        return cls(**picked)


def load_config() -> Config:
    path = config_path()
    if not path.is_file():
        raise FileNotFoundError(
            f"No config at {path}. Run: aim install"
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Config is not an object: {path}")
    cfg = Config.from_dict(data)
    cfg.validate()
    return cfg


def save_config(cfg: Config) -> Path:
    cfg.validate()
    home = home_dir()
    home.mkdir(parents=True, exist_ok=True)
    path = config_path()
    payload = json.dumps(cfg.to_dict(), indent=2) + "\n"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(path)
    _tighten(path)
    return path


def _tighten(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def create_config(
    *,
    name: str | None = None,
    github_root: str | None = None,
    secret: str | None = None,
    http_port: int | None = None,
) -> Config:
    """Create a config, or update name / github root / secret on an existing one.

    An existing secret is kept unless a new one is passed. Re-running install
    must not rotate the key the other machines already have.
    """
    path = config_path()
    if path.is_file():
        cfg = load_config()
        if name:
            cfg.name = name
        if github_root:
            cfg.github_root = str(Path(github_root))
        if secret:
            cfg.secret = secret
        if http_port:
            cfg.http_port = http_port
        save_config(cfg)
        return cfg

    root = Path(github_root) if github_root else detect_github_root()
    root.mkdir(parents=True, exist_ok=True)
    cfg = Config(
        node_id=str(uuid.uuid4()),
        name=name or default_name(),
        github_root=str(root),
        secret=secret or new_secret(),
        http_port=http_port or HTTP_PORT,
    )
    save_config(cfg)
    return cfg


def agent_name(explicit: str | None) -> str:
    picked = (explicit or os.environ.get("AIM_AGENT") or os.environ.get("IVM_AGENT") or "unknown").strip().lower()
    if not picked or len(picked) > 32 or any(not (ch.isalnum() or ch in "-_") for ch in picked):
        raise ValueError(f"invalid agent name: {explicit!r}")
    return picked


def node_name_ok(name: str) -> bool:
    if not name or len(name) > 64:
        return False
    return all(ch.isalnum() or ch in "-_." for ch in name)
