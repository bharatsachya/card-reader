"""
Shared fixtures.

THE ENVIRONMENT IS SET BEFORE app.* IS IMPORTED ANYWHERE. app/config.py builds
its Settings at import time and app/store.py builds the store from it at import
time, so a variable set after the first `from app...` line has already been
missed. Putting it at the top of conftest -- the first thing pytest loads --
is what makes that ordering reliable rather than accidental.
"""

import os
import socket
import tempfile
import threading
import time

import pytest

os.environ.setdefault("STORE_BACKEND", "memory")
os.environ.setdefault("AUTH_MODE", "local")
# Point the default store somewhere disposable so importing the app never
# creates data/leads.db in the working tree just by running the suite.
os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "test.db"))
os.environ.setdefault("IMAGE_DIR", os.path.join(tempfile.mkdtemp(), "images"))


def free_port() -> int:
    """An unused port. Binding to 0 lets the OS pick one that is genuinely free."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="session")
def stub_model():
    """
    The stub model server, running for the whole session.

    Started in a thread rather than a subprocess so it shares the interpreter
    and needs no PYTHONPATH juggling, and so a crash surfaces as a test error
    rather than a silent exit code. Session-scoped because starting uvicorn
    costs ~1s and nothing in a test mutates it.
    """
    import uvicorn

    from tools.stub_model_server import app as stub_app

    port = free_port()
    config = uvicorn.Config(stub_app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("stub model server did not start")
        time.sleep(0.05)

    yield f"http://127.0.0.1:{port}/v1/chat/completions"

    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def sqlite_store():
    """A SqliteLeadStore on a fresh file per test."""
    from app.store import SqliteLeadStore

    return SqliteLeadStore(os.path.join(tempfile.mkdtemp(), "leads.db"))


@pytest.fixture
def memory_store():
    from app.store import InMemoryLeadStore

    return InMemoryLeadStore()
