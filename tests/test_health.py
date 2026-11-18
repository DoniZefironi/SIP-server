from fastapi.testclient import TestClient

from app.main import app


def test_health_reports_database_down() -> None:
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 503
    assert response.json() == {"status": "degraded", "db": "down", "ami": "disabled"}
