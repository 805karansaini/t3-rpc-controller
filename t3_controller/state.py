import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse


ENV_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def validate_name(name: str) -> None:
    if not isinstance(name, str) or name in {".", ".."} or not ENV_NAME_RE.fullmatch(name):
        raise ValueError("environment name must match [A-Za-z0-9._-]+ and cannot be . or ..")


def write_private(path: Path, text: str) -> None:
    """Replace a complete file, with owner-only permissions from creation."""
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}-")
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
        temporary_path.replace(path)
    finally:
        temporary_path.unlink(missing_ok=True)


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
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("T3 URL must be an http(s) origin")
    if (
        parsed.username is not None or parsed.password is not None
        or parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment
        or any(character.isspace() for character in value)
    ):
        raise ValueError("T3 URL must contain only an origin, without credentials, path, query, or fragment")
    try:
        port = parsed.port
    except ValueError:
        raise ValueError("T3 URL has an invalid port") from None
    if port == 0:
        raise ValueError("T3 URL has an invalid port")
    return f"{parsed.scheme}://{parsed.netloc}"


class EnvironmentStore:
    def __init__(self, home: Path | None = None):
        self.home = Path(home).expanduser() if home is not None else default_home()
        self.config_path = self.home / "environments.json"
        self.token_dir = self.home / "tokens"

    def save(self, name: str, url: str, token: str) -> None:
        validate_name(name)
        normalized_url = normalize_url(url)
        if not token.strip():
            raise ValueError("bearer token cannot be empty")
        self.home.mkdir(parents=True, exist_ok=True)
        self.token_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.home, 0o700)
        os.chmod(self.token_dir, 0o700)
        config = self.load_config()
        config[name] = {"url": normalized_url}
        token_path = self.token_dir / name
        previous_token = token_path.read_text(encoding="utf-8") if token_path.exists() else None
        write_private(token_path, token.strip())
        try:
            write_private(self.config_path, json.dumps(config, indent=2) + "\n")
        except OSError:
            if previous_token is None:
                token_path.unlink(missing_ok=True)
            else:
                write_private(token_path, previous_token)
            raise

    def load_config(self) -> dict:
        if not self.config_path.exists():
            return {}
        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            raise ValueError("environment configuration must be a JSON object")
        for name, entry in config.items():
            validate_name(name)
            if not isinstance(entry, dict) or not isinstance(entry.get("url"), str):
                raise ValueError("each environment must have a URL string")
            entry["url"] = normalize_url(entry["url"])
        return config

    def load(self, name: str) -> tuple[str, str]:
        validate_name(name)
        config = self.load_config()
        if name not in config:
            raise KeyError(f"unknown environment: {name}")
        token_path = self.token_dir / name
        if not token_path.exists():
            raise FileNotFoundError(f"missing token for environment: {name}")
        token = token_path.read_text(encoding="utf-8").strip()
        if not token:
            raise ValueError(f"empty token for environment: {name}")
        return config[name]["url"], token

    def names(self) -> list[str]:
        return sorted(self.load_config())

    def remove(self, name: str) -> Path:
        validate_name(name)
        config = self.load_config()
        if name not in config:
            raise KeyError(f"unknown environment: {name}")
        removed_root = self.home / "removed"
        removed_root.mkdir(parents=True, exist_ok=True)
        os.chmod(removed_root, 0o700)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = removed_root / f"{name}-{stamp}"
        backup.mkdir(mode=0o700)
        metadata_path = backup / "environment.json"
        write_private(
            metadata_path,
            json.dumps({name: {"url": config[name].get("url")}}, indent=2) + "\n"
        )
        token_path = self.token_dir / name
        if token_path.exists():
            token_path.replace(backup / "token")
        del config[name]
        try:
            write_private(self.config_path, json.dumps(config, indent=2) + "\n")
        except OSError:
            if (backup / "token").exists():
                (backup / "token").replace(token_path)
            raise
        return backup
