"""
Shared helpers for credential and session handling.

- Credentials are read only from the app options file (/data/options.json),
  never from command-line arguments or environment variables, so they do not
  show up in process listings or generated config files.
- Session files are written atomically with 0600 permissions and never
  contain the account password.
"""
import json
import os
import uuid

OPTIONS_FILE = os.environ.get("BIRDFY_OPTIONS", "/data/options.json")
UDID_FILE = os.environ.get("BIRDFY_UDID_FILE", "/data/birdfy_udid")


def load_credentials() -> tuple[str, str]:
    """Return (email, password) from the app options."""
    with open(OPTIONS_FILE) as f:
        opts = json.load(f)
    return opts.get("email", ""), opts.get("password", "")


def write_private_json(path: str, data: dict) -> None:
    """Write JSON atomically, readable only by the owner (0600)."""
    tmp = f"{path}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def device_udid(prefix: str = "android") -> str:
    """A random client identifier, generated once per install and kept in /data."""
    try:
        with open(UDID_FILE) as f:
            value = f.read().strip()
            if value:
                return value
    except FileNotFoundError:
        pass
    value = f"{prefix}-{uuid.uuid4()}"
    fd = os.open(UDID_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(value)
    return value
