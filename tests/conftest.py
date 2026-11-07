import os
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def isolated_secret(nbytes=18):
    """Unpredictable test material. Nothing here is a deployed credential."""
    return secrets.token_urlsafe(nbytes)


def isolated_username(prefix):
    return f"{prefix}{secrets.token_hex(4)}"


os.environ["JWT_SECRET"] = isolated_secret(32)
os.environ["ADMIN_USERNAME"] = isolated_username("user")
os.environ["ADMIN_PASSWORD"] = isolated_secret()
os.environ.pop("DATABASE_URL", None)
for name in (
    "TEST_USERNAME", "TEST_PASSWORD", "SETUP_TOKEN", "CRON_SECRET",
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_OWNER_IDS",
    "TELEGRAM_WEBHOOK_SECRET", "REMINDERS_DRY_RUN", "REMINDER_MESSAGE_TEMPLATE",
    "REMINDER_MILESTONE_DAYS", "DEFAULT_COUNTRY_CODE",
):
    os.environ.pop(name, None)

import pytest

import server
from netflix_manager.repos import MemoryRepos


@pytest.fixture(autouse=True)
def memory(monkeypatch):
    repos = MemoryRepos()
    monkeypatch.setitem(server.app.config, "REPOS_FACTORY", lambda: repos)
    return repos


@pytest.fixture
def client():
    return server.app.test_client()


def login(client, username=None, password=None):
    if username is None:
        username = os.environ["ADMIN_USERNAME"]
    if password is None:
        password = os.environ["ADMIN_PASSWORD"]
    response = client.post("/api/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.get_json()
    return response.get_json()["token"]


def auth(token):
    return {"Authorization": f"Bearer {token}"}
