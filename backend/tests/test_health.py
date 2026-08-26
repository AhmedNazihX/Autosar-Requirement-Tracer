from fastapi.testclient import TestClient

from api.main import __version__, app

client = TestClient(app)


def test_health_returns_200() -> None:
    response = client.get("/health")
    assert response.status_code == 200


def test_health_returns_documented_shape() -> None:
    response = client.get("/health")
    assert response.json() == {"status": "ok", "version": __version__}
