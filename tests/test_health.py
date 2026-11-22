from dataclasses import dataclass

from fastapi.testclient import TestClient

from receptionist.app import AppDeps, create_app


@dataclass
class FakeChecker:
    result: dict

    async def status(self) -> dict:
        return self.result


def make_client() -> TestClient:
    checker = FakeChecker({"postgres": "up", "redis": "down", "express": "up", "spring": "up"})
    return TestClient(create_app(AppDeps(health=checker)))


def test_root_running():
    res = make_client().get("/")
    assert res.status_code == 200
    assert res.json() == {"message": "Running"}


def test_health_envelope():
    res = make_client().get("/api/health")
    body = res.json()
    assert res.status_code == 200
    assert body["success"] is True
    assert body["data"]["environment"] == "test"
    assert body["data"]["dependencies"]["redis"] == "down"
    assert body["timestamp"]


def test_health_defaults_to_disabled_without_deps():
    body = TestClient(create_app()).get("/api/health").json()
    assert set(body["data"]["dependencies"].values()) == {"disabled"}


def test_unknown_route_404_envelope():
    res = make_client().get("/nope")
    assert res.status_code == 404
    assert res.json()["success"] is False
