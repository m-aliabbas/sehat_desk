"""Unsaved forms: kept per person, private, and removed on request."""

import json

import pytest
from django.test import Client

from apps.core.models import FormDraft

pytestmark = pytest.mark.django_db


def _save(client, path="/pharmacy/purchases/new/", **extra):
    body = {
        "path": path,
        "form": "0",
        "title": "New purchase",
        "data": json.dumps({"values": {"a": ["1"]}}),
    }
    return client.post("/drafts/save/", {**body, **extra})


def test_saved_draft_comes_back_on_its_page_and_saving_again_replaces_it(admin_client):
    assert _save(admin_client).status_code == 200
    assert _save(admin_client).status_code == 200
    assert FormDraft.objects.count() == 1
    found = admin_client.get("/drafts/for-page/", {"path": "/pharmacy/purchases/new/"}).json()
    assert found["drafts"][0]["data"]["values"]["a"] == ["1"]
    assert b"New purchase" in admin_client.get("/drafts/").content


def test_someone_else_cannot_see_or_discard_my_draft(admin_client, make_user):
    _save(admin_client)
    other = Client()
    other.force_login(make_user("nurse"))
    assert (
        other.get("/drafts/for-page/", {"path": "/pharmacy/purchases/new/"}).json()["drafts"] == []
    )
    other.post("/drafts/discard/", {"path": "/pharmacy/purchases/new/"})
    assert FormDraft.objects.count() == 1


def test_discard_removes_and_outside_paths_and_junk_are_refused(admin_client):
    _save(admin_client)
    admin_client.post("/drafts/discard/", {"path": "/pharmacy/purchases/new/", "form": "0"})
    assert FormDraft.objects.count() == 0
    assert _save(admin_client, path="https://evil.example/").status_code == 400
    assert _save(admin_client, data="not json").status_code == 400
