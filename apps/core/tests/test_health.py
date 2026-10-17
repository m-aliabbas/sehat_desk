import pytest
from django.test import Client


@pytest.fixture
def client():
    return Client()


def test_live_returns_ok_without_database(client):
    response = client.get("/health/live/")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.django_db
def test_ready_checks_database(client):
    response = client.get("/health/ready/")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}


def test_health_rejects_post(client):
    response = client.post("/health/live/")
    assert response.status_code == 405
