"""The wire protocol framing — pure, no Blender, no bridge."""

from __future__ import annotations

import socket
import threading

import pytest

from scene.protocol import MAX_MESSAGE_BYTES, recv_message, send_message


def test_roundtrip_single_message() -> None:
    a, b = socket.socketpair()
    try:
        send_message(a, {"command": "ping", "params": {}})
        assert recv_message(b) == {"command": "ping", "params": {}}
    finally:
        a.close()
        b.close()


def test_multiple_messages_preserve_order_and_boundaries() -> None:
    a, b = socket.socketpair()
    try:
        for i in range(5):
            send_message(a, {"n": i})
        assert [recv_message(b) for _ in range(5)] == [{"n": i} for i in range(5)]
    finally:
        a.close()
        b.close()


def test_clean_eof_returns_none() -> None:
    a, b = socket.socketpair()
    a.close()  # peer closes at a message boundary
    try:
        assert recv_message(b) is None
    finally:
        b.close()


def test_large_payload_survives_fragmentation() -> None:
    a, b = socket.socketpair()
    big = {"blob": "x" * 500_000}  # exceeds the kernel socket buffer → must send from a thread
    writer = threading.Thread(target=send_message, args=(a, big))
    writer.start()
    try:
        assert recv_message(b) == big  # read loop reassembles the fragmented payload
    finally:
        writer.join(timeout=5)
        a.close()
        b.close()


def test_oversize_message_is_rejected() -> None:
    a, b = socket.socketpair()
    try:
        with pytest.raises(ValueError, match="too large"):
            send_message(a, {"blob": "x" * (MAX_MESSAGE_BYTES + 1)})
    finally:
        a.close()
        b.close()
