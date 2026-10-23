#!/usr/bin/env python3
"""Turn a Secrets Manager SecretString into a mode-600 dotenv file.

Accepts a JSON object of string values, or a raw dotenv body. Empty values
are omitted. Writes atomically and never prints the secret.
"""

from __future__ import annotations

import json
import os
import stat
import sys


def encode_dotenv(pairs: list[tuple[str, object]]) -> str:
    lines: list[str] = []
    for key, value in pairs:
        if not isinstance(key, str) or not key or not all(c.isalnum() or c == "_" for c in key):
            raise SystemExit(f"Refusing secret key {key!r}")
        if value is None:
            continue
        if isinstance(value, (dict, list)):
            raise SystemExit(f"Secret key {key!r} must be a string, number, or bool.")
        text = str(value)
        if text == "":
            continue
        escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        lines.append(f'{key}="{escaped}"')
    if not lines:
        raise SystemExit("Secret has no values.")
    return "\n".join(lines) + "\n"


def to_dotenv(raw: str) -> str:
    text = raw.lstrip("\ufeff").strip()
    if not text or text == "None":
        raise SystemExit(
            "SecretString is empty. Store a JSON object in Secrets Manager."
        )

    parsed: object
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None

    if isinstance(parsed, dict):
        return encode_dotenv(list(parsed.items()))
    if parsed is not None and not isinstance(parsed, (dict, str)):
        raise SystemExit("Secret JSON must be an object of string values.")
    if isinstance(parsed, str):
        try:
            again = json.loads(parsed)
        except json.JSONDecodeError:
            again = None
        if isinstance(again, dict):
            return encode_dotenv(list(again.items()))
        body = parsed if parsed.endswith("\n") else parsed + "\n"
        if "=" not in body:
            raise SystemExit("Secret string has no KEY=value lines.")
        return body

    if "=" not in text:
        raise SystemExit("Secret is neither a JSON object nor dotenv.")
    return text if text.endswith("\n") else text + "\n"


def write_private(dest: str, body: str) -> None:
    parent = os.path.dirname(dest) or "."
    os.makedirs(parent, exist_ok=True)
    tmp = dest + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, body.encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    os.replace(tmp, dest)


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        raise SystemExit(f"Usage: {argv[0]} SECRET_JSON DEST")
    with open(argv[1], "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    secret = payload.get("SecretString")
    if not isinstance(secret, str):
        raise SystemExit("GetSecretValue response has no SecretString.")
    write_private(argv[2], to_dotenv(secret))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
