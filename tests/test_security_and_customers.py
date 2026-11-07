import io
import json
import os
from datetime import date, timedelta
from pathlib import Path

import server
from netflix_manager.dates import add_one_month
from tests.conftest import auth, isolated_secret, isolated_username, login

ROOT = Path(__file__).resolve().parent.parent


def test_missing_auth_fails_closed(client, monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    response = client.post("/api/login", json={
        "username": os.environ["ADMIN_USERNAME"],
        "password": isolated_secret(),
    })
    assert response.status_code == 503
    body = response.get_data(as_text=True)
    assert os.environ["JWT_SECRET"] not in body
    assert response.get_json()["error"] == "Authentication is not configured"


def test_short_jwt_secret_is_not_used(client, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "y" * 8)
    response = client.post("/api/login", json={
        "username": os.environ["ADMIN_USERNAME"],
        "password": os.environ["ADMIN_PASSWORD"],
    })
    assert response.status_code == 503


def test_wrong_password_is_generic(client):
    wrong = isolated_secret()
    assert wrong != os.environ["ADMIN_PASSWORD"]
    response = client.post("/api/login", json={
        "username": os.environ["ADMIN_USERNAME"],
        "password": wrong,
    })
    assert response.status_code == 401
    assert response.get_json()["error"] == "Invalid username or password"


def test_published_defaults_are_gone_from_source():
    text = "\n".join(path.read_text(encoding="utf-8") for path in [
        ROOT / "server.py",
        *(ROOT / "netflix_manager").glob("*.py"),
    ])
    for key in ("ADMIN_PASSWORD", "JWT_SECRET", "ADMIN_USERNAME", "TEST_PASSWORD"):
        assert f'{key}", "' not in text
        assert f"{key}', '" not in text
    assert "str(e)" not in (ROOT / "server.py").read_text(encoding="utf-8")


def test_exception_does_not_leak_database_url(client):
    user = isolated_username("db")
    password = isolated_secret()
    host = "db.example"
    leaked = RuntimeError(f"postgresql://{user}:{password}@{host}:5432/postgres")
    with server.app.app_context():
        response, status = server.handle_exception(leaked)
    assert status == 500
    body = response.get_data(as_text=True)
    assert password not in body
    assert user not in body
    assert host not in body
    assert response.get_json()["error"] == "Internal server error"


def test_demo_never_reads_real_rows(client, memory, monkeypatch):
    memory.customers.insert({
        "name": "Secret Person",
        "phone": "919999999999",
        "account": "Secret Account",
        "profile_name": "Secret Profile",
        "monthly_amount": 250,
        "start_date": "2026-01-15",
        "payment_status": "Paid",
    })
    before = memory.customers.calls
    demo_user = isolated_username("demo")
    demo_password = isolated_secret()
    monkeypatch.setenv("TEST_USERNAME", demo_user)
    monkeypatch.setenv("TEST_PASSWORD", demo_password)
    token = login(client, demo_user, demo_password)
    response = client.get("/api/customers", headers=auth(token))
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Secret Person" not in body
    assert "919999999999" not in body
    assert "Demo Customer" in body
    assert memory.customers.calls == before
    denied = client.post("/api/customers", headers=auth(token), json={})
    assert denied.status_code == 403


def test_demo_is_off_without_configuration(client):
    response = client.post("/api/login", json={
        "username": isolated_username("test"),
        "password": isolated_secret(),
    })
    assert response.status_code == 401


def test_setup_is_disabled_and_does_not_reuse_jwt_secret(client, monkeypatch):
    assert client.post("/api/setup").status_code == 404
    setup = isolated_secret()
    monkeypatch.setenv("SETUP_TOKEN", setup)
    wrong = client.post("/api/setup", headers={"X-Setup-Token": os.environ["JWT_SECRET"]})
    assert wrong.status_code == 401
    called = []
    monkeypatch.setattr(server, "init_db", lambda: called.append(True))
    ok = client.post("/api/setup", headers={"X-Setup-Token": setup})
    assert ok.status_code == 200
    assert called == [True]


def test_customer_crud_and_old_cycle_renewal(client, memory):
    token = login(client)
    created = client.post("/api/customers", headers=auth(token), json={
        "name": "Ada",
        "phone": "9876543210",
        "account": "Account 1",
        "profile_name": "Ada",
        "monthly_amount": 199,
        "start_date": "2026-01-15",
        "payment_status": "Payment pending",
    })
    assert created.status_code == 201
    body = created.get_json()
    assert body["expiry_date"] == "2026-02-15"
    renewed = client.post(f"/api/customers/{body['id']}/paid", headers=auth(token))
    assert renewed.status_code == 200
    again = renewed.get_json()
    assert again["start_date"] == "2026-02-15"
    assert again["expiry_date"] == "2026-03-15"
    assert again["payment_status"] == "Paid"
    assert again["start_date"] != date.today().isoformat()
    edited = client.put(f"/api/customers/{body['id']}", headers=auth(token), json={
        "name": "Ada <script>",
        "phone": "9876543210",
        "account": "Account 1",
        "profile_name": "Ada",
        "monthly_amount": 199,
        "start_date": "2026-02-15",
        "payment_status": "Payment pending",
    })
    assert edited.status_code == 200
    assert edited.get_json()["payment_status"] == "Payment pending"
    assert edited.get_json()["start_date"] == "2026-02-15"
    removed = client.delete(f"/api/customers/{body['id']}", headers=auth(token))
    assert removed.status_code == 200
    missing = client.delete("/api/customers/999", headers=auth(token))
    assert missing.status_code == 200


def test_month_end_and_leap_renewal(client):
    token = login(client)
    created = client.post("/api/customers", headers=auth(token), json={
        "name": "Month End",
        "phone": "+44 7911 123456",
        "account": "Account",
        "profile_name": "Profile",
        "monthly_amount": 10,
        "start_date": "2026-01-31",
    })
    cid = created.get_json()["id"]
    assert created.get_json()["expiry_date"] == "2026-02-28"
    renewed = client.post(f"/api/customers/{cid}/paid", headers=auth(token)).get_json()
    assert renewed["start_date"] == "2026-02-28"
    assert renewed["expiry_date"] == "2026-03-28"
    leap = client.post("/api/customers", headers=auth(token), json={
        "name": "Leap",
        "phone": "+1 415 555 2671",
        "account": "Account",
        "profile_name": "Profile",
        "monthly_amount": 10,
        "start_date": "2024-01-31",
    }).get_json()
    assert leap["expiry_date"] == "2024-02-29"
    assert add_one_month(date(2024, 1, 31)).isoformat() == "2024-02-29"


def test_late_payment_does_not_restart_from_today(client, monkeypatch):
    token = login(client)
    created = client.post("/api/customers", headers=auth(token), json={
        "name": "Late",
        "phone": "919876543210",
        "account": "Account",
        "profile_name": "Profile",
        "monthly_amount": 10,
        "start_date": "2026-01-15",
    }).get_json()
    monkeypatch.setattr(server, "date", date)
    renewed = client.post(f"/api/customers/{created['id']}/paid", headers=auth(token)).get_json()
    assert renewed["start_date"] == "2026-02-15"
    assert renewed["start_date"] != date.today().isoformat()


def test_whatsapp_link_encoding_and_invalid_phone(client):
    token = login(client)
    created = client.post("/api/customers", headers=auth(token), json={
        "name": "Ada & Co\n<script>",
        "phone": "9876543210",
        "account": "Account 1",
        "profile_name": "Ada",
        "monthly_amount": 199,
        "start_date": (date.today() - timedelta(days=3)).isoformat(),
    }).get_json()
    link = client.get(f"/api/customers/{created['id']}/whatsapp", headers=auth(token))
    assert link.status_code == 200
    url = link.get_json()["url"]
    assert url.startswith("https://wa.me/919876543210?text=")
    assert " " not in url.split("?text=")[1]
    assert "%0A" in url
    assert "<script>" not in link.get_json()["message"]
    assert "Ada & Co" in link.get_json()["message"]
    bad = client.post("/api/customers", headers=auth(token), json={
        "name": "Bad",
        "phone": "12345",
        "account": "Account",
        "profile_name": "Profile",
        "monthly_amount": 10,
        "start_date": "2026-04-01",
    }).get_json()
    refused = client.get(f"/api/customers/{bad['id']}/whatsapp", headers=auth(token))
    assert refused.status_code == 422
    assert "wa.me" not in refused.get_data(as_text=True)


def test_csv_formula_duplicate_and_size(client):
    token = login(client)
    client.post("/api/customers", headers=auth(token), json={
        "name": "Ada",
        "phone": "919876543210",
        "account": "Account",
        "profile_name": "Profile",
        "monthly_amount": 10,
        "start_date": "2026-04-01",
        "payment_status": "Paid",
    })
    exported = client.get("/api/customers/export", headers=auth(token))
    assert exported.status_code == 200
    assert exported.data.startswith(b"Name,Phone,Account")
    payload = (
        "Name,Phone,Account,Profile Name,Monthly Amount,Start Date,Payment Status\n"
        "=cmd,919000000000,Account,Profile,10,2026-04-02,Paid\n"
        "Ada,919876543210,Account,Profile,10,2026-04-01,Paid\n"
    ).encode()
    response = client.post(
        "/api/customers/import",
        headers=auth(token),
        data={"file": (io.BytesIO(payload), "customers.csv")},
    )
    body = response.get_json()
    assert body["added"] == 1
    assert any("duplicate" in item for item in body["errors"])
    assert "919000000000" not in " ".join(body["errors"])
    listed = client.get("/api/customers", headers=auth(token)).get_json()
    formula = [row for row in listed if str(row["name"]).startswith("=cmd")]
    assert formula
    again = client.get("/api/customers/export", headers=auth(token)).get_data(as_text=True)
    assert "'=cmd" in again
    huge = client.post(
        "/api/customers/import",
        headers=auth(token),
        data={"file": (io.BytesIO(b"x" * 200_001), "customers.csv")},
    )
    assert huge.status_code == 400


def test_frontend_preview_and_delete_markup():
    html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
    assert "setMonth" not in html
    assert "NetflixDates.addOneMonthISO" in html
    assert 'onclick="openDel(${c.id})"' in html
    assert "wa.me" not in html
    vercel = json.loads((ROOT / "vercel.json").read_text(encoding="utf-8"))
    assert "crons" not in vercel
    snippet = json.loads((ROOT / "deploy" / "cron-snippet.json").read_text(encoding="utf-8"))
    assert snippet["crons"][0]["schedule"] == "30 3 * * *"
    assert snippet["crons"][0]["path"] == "/api/cron/reminders"
    dates = (ROOT / "public" / "dates.js").read_text(encoding="utf-8")
    assert "addOneMonthISO" in dates
