import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse


ENV_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def default_home() -> Path:
    configured = os.environ.get("T3_CONTROLLER_HOME")
    if configured:
        return Path(configured).expanduser()
    checkout_home = Path(__file__).resolve().parent.parent / ".state"
    if checkout_home.exists() or (checkout_home.parent / ".git").exists():
        return checkout_home
    return Path.home() / ".local" / "state" / "t3-rpc-controller"


DEFAULT_HOME = default_home()


def normalize_url(url: str) -> str:
    value = url.strip()
    if value.startswith("@url:") or "`" in value:
        raise ValueError(
            "use a plain URL, for example http://100.65.116.80:3773 "
            "(without @url: or backticks)"
        )
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("T3 URL must be an http(s) origin")
    return f"{parsed.scheme}://{parsed.netloc}"


class EnvironmentStore:
    def __init__(self, home: Path = DEFAULT_HOME):
        self.home = Path(home)
        self.config_path = self.home / "environments.json"
        self.token_dir = self.home / "tokens"

    def save(self, name: str, url: str, token: str) -> None:
        if not ENV_NAME_RE.fullmatch(name):
            raise ValueError("environment name must match [A-Za-z0-9._-]+")
        normalized_url = normalize_url(url)
        self.home.mkdir(parents=True, exist_ok=True)
        self.token_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.home, 0o700)
        os.chmod(self.token_dir, 0o700)
        config = self.load_config()
        config[name] = {"url": normalized_url}
        temp_path = self.config_path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(config, indent=2) + "\n")
        os.chmod(temp_path, 0o600)
        temp_path.replace(self.config_path)
        token_path = self.token_dir / name
        token_path.write_text(token.strip())
        os.chmod(token_path, 0o600)

    def load_config(self) -> dict:
        if not self.config_path.exists():
            return {}
        return json.loads(self.config_path.read_text())

    def load(self, name: str) -> tuple[str, str]:
        if not ENV_NAME_RE.fullmatch(name):
            raise ValueError("invalid environment name")
        config = self.load_config()
        if name not in config:
            raise KeyError(f"unknown environment: {name}")
        token_path = self.token_dir / name
        if not token_path.exists():
            raise FileNotFoundError(f"missing token for environment: {name}")
        return config[name]["url"], token_path.read_text().strip()

    def names(self) -> list[str]:
        return sorted(self.load_config())

    def remove(self, name: str) -> Path:
        if not ENV_NAME_RE.fullmatch(name):
            raise ValueError("invalid environment name")
        config = self.load_config()
        if name not in config:
            raise KeyError(f"unknown environment: {name}")
        removed_root = self.home / "removed"
        removed_root.mkdir(parents=True, exist_ok=True)
        os.chmod(removed_root, 0o700)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = removed_root / f"{name}-{stamp}"
        backup.mkdir(mode=0o700)
        metadata_path = backup / "environment.json"
        metadata_path.write_text(
            json.dumps({name: {"url": config[name].get("url")}}, indent=2) + "\n"
        )
        os.chmod(metadata_path, 0o600)
        token_path = self.token_dir / name
        if token_path.exists():
            token_path.replace(backup / "token")
        del config[name]
        temp_path = self.config_path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(config, indent=2) + "\n")
        os.chmod(temp_path, 0o600)
        temp_path.replace(self.config_path)
        return backup
