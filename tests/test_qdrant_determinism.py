from __future__ import annotations

import pytest

from app.services import qdrant


class _Response:
    status_code = 200

    def __init__(self, results):
        self._results = results

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return {"result": self._results}


class _Client:
    request_json = None

    def __init__(self, *, timeout):
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, _url, *, headers, json):
        del headers
        type(self).request_json = json
        return _Response(
            [
                {"id": "b", "score": 0.5, "payload": {"chunk_id": "b"}},
                {"id": "z", "score": 0.8, "payload": {"chunk_id": "z"}},
                {"id": "a", "score": 0.5, "payload": {"chunk_id": "a"}},
            ]
        )


async def test_exact_search_sorts_score_then_chunk_id(monkeypatch):
    monkeypatch.setattr(qdrant.httpx, "AsyncClient", _Client)

    results = await qdrant.search("collection", [1.0], limit=50, exact=True)

    assert _Client.request_json["params"] == {"exact": True}
    assert _Client.request_json["limit"] == 51
    assert [item["id"] for item in results] == ["z", "a", "b"]


class _TieClient:
    request_limits = []

    def __init__(self, *, timeout):
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, _url, *, headers, json):
        del headers
        type(self).request_limits.append(json["limit"])
        points = [
            {"id": f"{index:03d}", "score": 0.5, "payload": {"chunk_id": f"{index:03d}"}}
            for index in reversed(range(64))
        ]
        return _Response(points[: json["limit"]])


async def test_exact_search_expands_only_when_tie_crosses_cutoff(monkeypatch):
    _TieClient.request_limits = []
    monkeypatch.setattr(qdrant.httpx, "AsyncClient", _TieClient)

    results = await qdrant.search("collection", [1.0], limit=50, exact=True)

    assert _TieClient.request_limits == [51, 102]
    assert len(results) == 50
    assert [item["id"] for item in results] == [f"{index:03d}" for index in range(50)]


class _CompletedTieClient(_TieClient):
    async def post(self, _url, *, headers, json):
        del headers
        type(self).request_limits.append(json["limit"])
        points = [
            {
                "id": f"{index:03d}",
                "score": 0.5 if index < 64 else 0.4,
                "payload": {"chunk_id": f"{index:03d}"},
            }
            for index in range(250)
        ]
        return _Response(points[: json["limit"]])


async def test_exact_search_stops_when_expanded_probe_completes_tie(monkeypatch):
    _CompletedTieClient.request_limits = []
    monkeypatch.setattr(qdrant.httpx, "AsyncClient", _CompletedTieClient)

    results = await qdrant.search("collection", [1.0], limit=50, exact=True)

    assert _CompletedTieClient.request_limits == [51, 102]
    assert [item["id"] for item in results] == [f"{index:03d}" for index in range(50)]


class _OverflowTieClient(_TieClient):
    async def post(self, _url, *, headers, json):
        del headers
        points = [
            {"id": f"{index:03d}", "score": 0.5, "payload": {"chunk_id": f"{index:03d}"}}
            for index in reversed(range(250))
        ]
        return _Response(points[: json["limit"]])


async def test_exact_search_fails_when_tie_exceeds_probe_bound(monkeypatch):
    monkeypatch.setattr(qdrant.httpx, "AsyncClient", _OverflowTieClient)

    with pytest.raises(qdrant.QdrantDeterminismError, match="tie exceeds"):
        await qdrant.search("collection", [1.0], limit=50, exact=True)


async def test_exact_search_rejects_limit_without_probe_slot():
    with pytest.raises(ValueError, match="below deterministic probe bound"):
        await qdrant.search("collection", [1.0], limit=201, exact=True)
