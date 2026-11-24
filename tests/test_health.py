from dataclasses import dataclass

from fastapi.testclient import TestClient

from receptionist.app import AppDeps, create_app


@dataclass
class FakeChecker:
    result: dict

    async def status(self) -> dict:
        return self.result


def make_client() -> TestClient:
    checker = FakeChecker({"postgres": "up", "redis": "down"})
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


def test_method_not_allowed_envelope():
    res = make_client().post("/")
    body = res.json()
    assert res.status_code == 405
    assert body["success"] is False
    assert body["message"] == "Method not allowed"


def test_unhandled_exception_returns_generic_500_envelope():
    app = create_app()

    @app.get("/boom")
    async def boom():
        raise RuntimeError("secret internal detail")

    res = TestClient(app, raise_server_exceptions=False).get("/boom")
    assert res.status_code == 500
    body = res.json()
    assert body["success"] is False
    assert body["message"] == "Internal server error"
    assert "secret internal detail" not in res.text


def test_health_503_while_draining():
    client = make_client()
    client.app.state.draining = True
    res = client.get("/api/health")
    assert res.status_code == 503
    body = res.json()
    assert body["success"] is False
    assert body["data"]["draining"] is True


def test_lifespan_shutdown_sets_draining():
    app = create_app(AppDeps(health=FakeChecker({})))
    with TestClient(app):
        assert app.state.draining is False
    assert app.state.draining is True


def test_ready_ok_without_calling_dependencies():
    class Exploding:
        async def status(self) -> dict:
            raise AssertionError("/ready must not probe dependencies")

    res = TestClient(create_app(AppDeps(health=Exploding()))).get("/ready")
    assert res.status_code == 200
    body = res.json()
    assert body["success"] is True
    assert body["message"] == "Ready"


def test_ready_503_while_draining():
    client = make_client()
    client.app.state.draining = True
    res = client.get("/ready")
    assert res.status_code == 503
    assert res.json()["data"]["draining"] is True
    # The root (liveness) stays 200 while draining, so live calls aren't killed.
    assert client.get("/").status_code == 200
