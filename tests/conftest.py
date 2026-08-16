"""Keep the default test suite hermetic; live checks require an explicit flag."""

from __future__ import annotations

import socket

import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--run-live",
        action="store_true",
        default=False,
        help="run tests marked live and allow real network access",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-live"):
        return
    skip = pytest.mark.skip(reason="live test requires --run-live")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _deny_network_by_default(monkeypatch, request):
    if request.config.getoption("--run-live"):
        yield
        return
    monkeypatch.setenv("XIAONING_OFFLINE_TESTS", "1")
    outbound_attempts = []

    # Fail Firestore client construction before gRPC creates background auth
    # threads. The socket guard below is the final safety net, not the primary
    # way an offline test should discover an unmocked Google dependency.
    try:
        from google.cloud import firestore as google_firestore
    except ImportError:
        google_firestore = None
    if google_firestore is not None:
        def offline_firestore_client(*_args, **_kwargs):
            raise RuntimeError(
                "Firestore is disabled in offline tests; mock it or mark the test live"
            )

        monkeypatch.setattr(google_firestore, "Client", offline_firestore_client)

    original_create_connection = socket.create_connection
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def _is_loopback(address) -> bool:
        if not isinstance(address, tuple) or not address:
            return False
        return str(address[0]).casefold() in {"127.0.0.1", "::1", "localhost"}

    def blocked(*args, **_kwargs):
        outbound_attempts.append(args[0] if args else "unknown")
        raise RuntimeError("network access is disabled in offline tests; use @pytest.mark.live")

    def guarded_create_connection(address, *args, **kwargs):
        if _is_loopback(address):
            return original_create_connection(address, *args, **kwargs)
        return blocked(address)

    def guarded_connect(instance, address):
        if _is_loopback(address):
            return original_connect(instance, address)
        return blocked(address)

    def guarded_connect_ex(instance, address):
        if _is_loopback(address):
            return original_connect_ex(instance, address)
        return blocked(address)

    monkeypatch.setattr(socket, "create_connection", guarded_create_connection)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    yield
    if outbound_attempts:
        pytest.fail(
            f"offline test attempted public network {len(outbound_attempts)} time(s); "
            "mock the boundary or mark the test live"
        )
