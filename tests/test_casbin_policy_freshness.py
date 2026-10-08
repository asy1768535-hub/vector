"""Independent workers sharing a real, disposable SQL adapter database."""
from __future__ import annotations

import importlib.util
import multiprocessing
from pathlib import Path
from types import SimpleNamespace

import pytest
from casbin_sqlalchemy_adapter import Adapter
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parent.parent


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def workers(tmp_path):
    dsn = f"sqlite:///{(tmp_path / 'policies.db').as_posix()}"
    engine = create_engine(dsn)
    Adapter(engine)
    pairs = []
    for i in range(2):
        worker = _load_module(f"permission_worker_{i}", "app/casbin/enforcer.py")
        worker.settings = SimpleNamespace(db_dsn_sync=dsn)
        service = _load_module(f"permission_service_{i}", "app/casbin/service.py")
        service.get_enforcer = worker.get_enforcer
        pairs.append((worker, service))
    yield pairs, engine
    for worker, _ in pairs:
        adapter = getattr(worker, "_adapter", None)
        if adapter is None and getattr(worker, "_enforcer", None) is not None:
            adapter = worker._enforcer.get_adapter()
        if adapter is not None:
            adapter._engine.dispose()
    engine.dispose()


def test_grant_visible_to_previously_initialized_worker(workers):
    ((first, writer), (second, _)), _engine = workers
    assert not first.has_permission("user-a", "library-a", "read")
    assert not second.has_permission("user-a", "library-a", "read")
    writer.grant("user-a", "library-a", ["read"])
    assert second.has_permission("user-a", "library-a", "read")
    assert not second.has_permission("user-b", "library-a", "read")
    assert not second.has_permission("user-a", "library-b", "read")


def test_revoke_visible_to_previously_initialized_worker(workers):
    ((first, writer), (second, _)), _engine = workers
    writer.grant("user-a", "library-a", ["read", "insert"])
    assert first.has_permission("user-a", "library-a", "read")
    assert second.has_permission("user-a", "library-a", "read")
    assert writer.revoke("user-a", "library-a", ["read"]) == 1
    assert not second.has_permission("user-a", "library-a", "read")
    assert second.has_permission("user-a", "library-a", "insert")


def test_regrant_does_not_use_other_workers_revoked_cache(workers):
    ((first, writer), (second, other)), _engine = workers
    writer.grant("user-a", "library-a", ["read", "insert", "delete"])
    assert second.has_permission("user-a", "library-a", "read")
    assert writer.revoke("user-a", "library-a") == 3
    assert len(other.grant("user-a", "library-a", ["read", "insert", "delete"])) == 3
    assert first.has_permission("user-a", "library-a", "read")


def test_sequential_grants_across_workers_are_idempotent(workers):
    ((first, writer), (second, other)), engine = workers
    first.get_enforcer()
    second.get_enforcer()
    assert len(writer.grant("user-a", "library-a", ["read"])) == 1
    assert other.grant("user-a", "library-a", ["read"]) == []
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM casbin_rule")).scalar_one() == 1


def test_permission_projections_follow_latest_saved_state(workers):
    ((_, writer), (_, reader)), _engine = workers
    assert reader.list_user_permissions("user-a") == {}
    writer.grant("user-a", "library-a", ["read", "delete"])
    assert reader.list_user_permissions("user-a") == {"library-a": ["read", "delete"]}
    assert reader.list_library_grantees("library-a") == {"user-a": ["read", "delete"]}
    writer.revoke("user-a", "library-a")
    assert reader.list_library_grantees("library-a") == {}


def test_refresh_does_not_mutate_an_inflight_snapshot(workers):
    ((first, writer), (_, other)), _engine = workers
    writer.grant("user-a", "library-a", ["read"])
    snapshot = first.get_enforcer()
    other.revoke("user-a", "library-a")
    current = first.get_enforcer()
    assert current is not snapshot
    assert not current.enforce("user-a", "library:library-a", "read")
    assert snapshot.enforce("user-a", "library:library-a", "read")


def test_database_read_failure_never_falls_back_to_cached_allow(workers, monkeypatch):
    ((first, writer), _), _engine = workers
    writer.grant("user-a", "library-a", ["read"])
    assert first.has_permission("user-a", "library-a", "read")

    def fail_load(self, model):
        raise RuntimeError("test database unavailable")

    monkeypatch.setattr(Adapter, "load_policy", fail_load)
    with pytest.raises(RuntimeError, match="test database unavailable"):
        first.has_permission("user-a", "library-a", "read")


def _permission_process(connection, dsn):
    from app.casbin import enforcer, service

    enforcer.settings = SimpleNamespace(db_dsn_sync=dsn)
    try:
        while True:
            operation = connection.recv()
            if operation == "stop":
                return
            if operation == "check":
                result = enforcer.has_permission("user-a", "library-a", "read")
            elif operation == "grant":
                result = len(service.grant("user-a", "library-a", ["read"]))
            elif operation == "revoke":
                result = service.revoke("user-a", "library-a", ["read"])
            else:
                raise AssertionError(operation)
            connection.send(result)
    finally:
        connection.close()


def test_two_real_processes_observe_grant_revoke_and_regrant(tmp_path):
    dsn = f"sqlite:///{(tmp_path / 'process-policies.db').as_posix()}"
    engine = create_engine(dsn)
    Adapter(engine)
    engine.dispose()
    context = multiprocessing.get_context("spawn")
    processes, connections = [], []
    try:
        for _ in range(2):
            parent, child = context.Pipe()
            process = context.Process(target=_permission_process, args=(child, dsn))
            process.start()
            child.close()
            processes.append(process)
            connections.append(parent)

        def call(index, operation):
            connections[index].send(operation)
            assert connections[index].poll(15), "permission worker timed out"
            return connections[index].recv()

        assert call(0, "check") is False
        assert call(1, "check") is False
        assert call(0, "grant") == 1
        assert call(1, "check") is True
        assert call(1, "grant") == 0
        assert call(0, "revoke") == 1
        assert call(1, "check") is False
        assert call(1, "grant") == 1
        assert call(0, "check") is True
        assert call(1, "revoke") == 1
        assert call(0, "check") is False
    finally:
        for connection in connections:
            connection.send("stop")
            connection.close()
        for process in processes:
            process.join(5)
            if process.is_alive():
                process.terminate()
                process.join(5)
