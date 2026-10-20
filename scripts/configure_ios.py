"""Write only Mac connection settings from .env into a private iOS build config.

The provider API key stays on the Mac and is never read into this output.
"""
from __future__ import annotations

import ipaddress
import os
from pathlib import Path
import re
import shlex
import tempfile

ROOT = Path(__file__).resolve().parents[1]
CONNECTION_KEYS = frozenset({"CLAIM_HOST", "CLAIM_PHONE_HOST", "CLAIM_PORT", "CLAIM_PAIR_TOKEN"})


def connection_values(source: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in source.read_text().splitlines():
        key, separator, raw = line.strip().removeprefix("export ").partition("=")
        key = key.strip()
        if separator and key in CONNECTION_KEYS:
            parts = shlex.split(raw, comments=True, posix=True)
            if len(parts) > 1:
                raise ValueError(f"Invalid {key} setting")
            values[key] = parts[0] if parts else ""
    return values


def render(values: dict[str, str]) -> str:
    host = values.get("CLAIM_PHONE_HOST", "").strip() or values.get("CLAIM_HOST", "").strip()
    port = values.get("CLAIM_PORT", "4180").strip()
    token = values.get("CLAIM_PAIR_TOKEN", "").strip()
    if not host or host in {"0.0.0.0", "::"}:
        raise ValueError("Set CLAIM_PHONE_HOST to the Mac's local network name or reachable Wi-Fi address, not a wildcard")
    try:
        address = ipaddress.ip_address(host.strip("[]"))
        if address.version == 6:
            host = f"[{address}]"
    except ValueError:
        if not re.fullmatch(r"[A-Za-z0-9.-]+", host):
            raise ValueError("CLAIM_HOST must be a host name or IP address") from None
    if not port.isascii() or not port.isdecimal() or not 1 <= int(port) <= 65535:
        raise ValueError("CLAIM_PORT must be 1 through 65535")
    if not re.fullmatch(r"[A-Za-z0-9._~-]{1,512}", token):
        raise ValueError("CLAIM_PAIR_TOKEN must be a nonempty URL-safe pairing token")
    # Empty build-setting expansion prevents // from becoming an xcconfig comment.
    return ("// Generated connection settings. Private pairing token; do not commit.\n"
            f"CLAIM_SERVER_URL = http:/$()/{host}:{port}\n"
            f"CLAIM_PAIR_TOKEN = {token}\n")


def main() -> None:
    content = render(connection_values(ROOT / ".env"))
    destination = ROOT / "ios" / "LocalConnection.xcconfig"
    descriptor, temporary = tempfile.mkstemp(prefix=".local-connection-", dir=destination.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w") as output:
            output.write(content)
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    print("Created private iOS connection settings (0600). Rebuild the app to apply them.")


if __name__ == "__main__":
    main()
