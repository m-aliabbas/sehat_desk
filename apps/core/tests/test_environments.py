"""Settings for development / staging / production. Each case boots Django in a subprocess,
because settings are read once per process."""

import os
import secrets
import subprocess
import sys

import pytest
from django.conf import settings
from django.urls import reverse

SECRET = secrets.token_urlsafe(50)


def boot(tmp_path, code="pass", **env):
    """Import settings with only the given SEHATDESK_/DJANGO_ variables set."""
    clean = {k: v for k, v in os.environ.items() if not k.startswith(("SEHATDESK_", "DJANGO_"))}
    clean.update(
        DJANGO_SETTINGS_MODULE="config.settings",
        SEHATDESK_ENV_FILE=str(tmp_path / "empty.env"),
        SEHATDESK_DATA_DIR=str(tmp_path / "data"),
    )
    clean.update(env)
    (tmp_path / "empty.env").write_text("")
    script = f"import django; django.setup(); from django.conf import settings as s; {code}"
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=settings.BASE_DIR,
        env=clean,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_deployed_environments_create_and_reuse_a_secret_key_file(tmp_path, environment):
    first = boot(tmp_path, "print(s.SECRET_KEY)", SEHATDESK_ENV=environment)
    assert first.returncode == 0, first.stderr
    key_file = tmp_path / "data" / "secret.key"
    assert key_file.read_text() == first.stdout.strip()
    assert len(first.stdout.strip()) >= 50
    if os.name == "posix":
        assert key_file.stat().st_mode & 0o777 == 0o600
    second = boot(tmp_path, "print(s.SECRET_KEY)", SEHATDESK_ENV=environment)
    assert second.stdout == first.stdout


def test_short_secret_key_file_is_refused(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "secret.key").write_text("short")
    result = boot(tmp_path, SEHATDESK_ENV="production")
    assert result.returncode != 0
    assert "too short" in result.stderr


def test_development_does_not_create_a_key_file(tmp_path):
    result = boot(tmp_path)
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "data" / "secret.key").exists()


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_deployed_environments_refuse_debug(tmp_path, environment):
    result = boot(
        tmp_path, SEHATDESK_ENV=environment, DJANGO_SECRET_KEY=SECRET, DJANGO_DEBUG="true"
    )
    assert result.returncode != 0
    assert "DJANGO_DEBUG must be false" in result.stderr


def test_unknown_environment_is_refused(tmp_path):
    result = boot(tmp_path, SEHATDESK_ENV="prod")
    assert result.returncode != 0
    assert "SEHATDESK_ENV must be one of" in result.stderr


def test_missing_env_file_is_refused(tmp_path):
    result = boot(tmp_path, SEHATDESK_ENV_FILE=str(tmp_path / "nope.env"))
    assert result.returncode != 0
    assert "SEHATDESK_ENV_FILE not found" in result.stderr


def test_env_file_is_read(tmp_path):
    env_file = tmp_path / "staging.env"
    env_file.write_text(f"SEHATDESK_ENV=staging\nDJANGO_SECRET_KEY={SECRET}\n")
    result = boot(tmp_path, "print(s.SEHATDESK_ENV)", SEHATDESK_ENV_FILE=str(env_file))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "staging"


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_deployed_environments_log_to_the_data_folder(tmp_path, environment):
    result = boot(tmp_path, SEHATDESK_ENV=environment, DJANGO_SECRET_KEY=SECRET)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "data" / "logs" / "sehatdesk.log").exists()


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_deploy_check_passes_on_plain_http_lan(tmp_path, environment):
    result = boot(
        tmp_path,
        "from django.core.management import call_command; "
        "call_command('check', deploy=True, fail_level='WARNING')",
        SEHATDESK_ENV=environment,
        DJANGO_SECRET_KEY=SECRET,
    )
    assert result.returncode == 0, result.stderr


def test_https_turns_on_transport_security(tmp_path):
    result = boot(
        tmp_path,
        "print(s.SESSION_COOKIE_SECURE, s.CSRF_COOKIE_SECURE, s.SECURE_SSL_REDIRECT, "
        "s.SECURE_HSTS_SECONDS, s.SECURE_PROXY_SSL_HEADER[0], s.SILENCED_SYSTEM_CHECKS)",
        SEHATDESK_ENV="production",
        DJANGO_SECRET_KEY=SECRET,
        SEHATDESK_HTTPS="true",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == [
        "True",
        "True",
        "True",
        "31536000",
        "HTTP_X_FORWARDED_PROTO",
        "[]",
    ]


def test_staging_keeps_patient_messages_off(tmp_path):
    code = "print(repr(s.SEHATDESK_SMS_ADAPTER), repr(s.SEHATDESK_WHATSAPP_ADAPTER))"
    adapters = {"SEHATDESK_SMS_ADAPTER": "a.Sms", "SEHATDESK_WHATSAPP_ADAPTER": "a.Wa"}
    blocked = boot(tmp_path, code, SEHATDESK_ENV="staging", DJANGO_SECRET_KEY=SECRET, **adapters)
    assert blocked.stdout.split() == ["''", "''"]
    allowed = boot(
        tmp_path,
        code,
        SEHATDESK_ENV="staging",
        DJANGO_SECRET_KEY=SECRET,
        SEHATDESK_STAGING_SEND_MESSAGES="true",
        **adapters,
    )
    assert allowed.stdout.split() == ["'a.Sms'", "'a.Wa'"]


def test_staging_banner_shows_only_in_staging(client, admin_user, settings):
    url = reverse("accounts:login")
    settings.SEHATDESK_ENV = "staging"
    assert b"Do not enter real patients" in client.get(url).content
    settings.SEHATDESK_ENV = "production"
    assert b"Do not enter real patients" not in client.get(url).content
