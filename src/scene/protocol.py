"""The wire protocol between the client (project venv) and the server (inside Blender).

Length-prefixed JSON: a 4-byte big-endian unsigned length, then that many UTF-8 bytes of a
JSON object. One module, imported by both ends, so the framing has a single definition. Pure
stdlib (``json``, ``struct``, ``socket``) so it loads unchanged inside Blender's bundled Python.

A request is ``{"command": str, "params": dict}``; a response envelope is
``{"ok": bool, "result": <any> | "error": str}`` — ``ok`` is *transport* success (the call was
delivered and dispatched), distinct from any domain-level ``ok`` a handler may put in its result.
"""

from __future__ import annotations

import json
import socket
import struct

_HEADER = struct.Struct(">I")  # 4-byte unsigned length prefix
MAX_MESSAGE_BYTES = 256 * 1024 * 1024  # guardrail: a base64 render is large but bounded


def send_message(sock: socket.socket, obj: object) -> None:
    """Frame *obj* as length-prefixed JSON and send it in full."""
    payload = json.dumps(obj).encode("utf-8")
    if len(payload) > MAX_MESSAGE_BYTES:
        raise ValueError(f"message too large: {len(payload)} bytes > {MAX_MESSAGE_BYTES}")
    sock.sendall(_HEADER.pack(len(payload)) + payload)


def recv_message(sock: socket.socket) -> object | None:
    """Read one framed message, or ``None`` on a clean EOF (peer closed between messages)."""
    header = _recv_exact(sock, _HEADER.size)
    if header is None:
        return None
    (length,) = _HEADER.unpack(header)
    if length > MAX_MESSAGE_BYTES:
        raise ValueError(f"declared message length {length} exceeds cap {MAX_MESSAGE_BYTES}")
    body = _recv_exact(sock, length)
    if body is None:
        raise ConnectionError("peer closed mid-message")
    return json.loads(body.decode("utf-8"))


def _recv_exact(sock: socket.socket, n: int) -> bytes | None:
    """Read exactly *n* bytes. ``None`` if EOF arrives before any byte; raises if EOF is mid-read."""
    chunks: list[bytes] = []
    remaining = n
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            if remaining == n:
                return None  # clean EOF at a message boundary
            raise ConnectionError("peer closed mid-frame")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)
