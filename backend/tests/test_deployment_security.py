import base64

import httpx
import pytest
from fastapi import FastAPI

from app.config import Settings
from app.security import POCSecurityMiddleware, validate_deployment


@pytest.mark.asyncio
async def test_production_access_and_request_bounds(tmp_path):
    settings = Settings(app_env="production", database_path=tmp_path / "db", poc_username="reviewer", poc_password="test-password-long-enough")
    app = FastAPI()
    app.add_middleware(POCSecurityMiddleware, settings=settings)

    @app.get("/api/health")
    async def health():
        return {"status": "ok"}

    @app.get("/api/incidents")
    async def incidents():
        return []

    @app.post("/api/investigations")
    async def start():
        return {"started": True}

    credentials = base64.b64encode(b"reviewer:test-password-long-enough").decode()
    auth = {"Authorization": f"Basic {credentials}"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://demo.example") as client:
        assert (await client.get("/api/health")).status_code == 200
        assert (await client.get("/api/incidents")).status_code == 401
        assert (await client.get("/api/incidents", headers=auth)).status_code == 200
        assert (await client.post("/api/investigations", headers={**auth, "Origin": "https://other.example"}, json={})).status_code == 403
        assert (await client.post("/api/investigations", headers={**auth, "Content-Type": "text/plain"}, content="{}")).status_code == 415
        assert (await client.post("/api/investigations", headers={**auth, "Content-Type": "application/json"}, content="x" * 16385)).status_code == 413
        assert (await client.post("/api/investigations", headers=auth, json={})).status_code == 200
        assert (await client.post("/api/investigations", headers=auth, json={})).status_code == 200
        assert (await client.post("/api/investigations", headers=auth, json={})).status_code == 200
        assert (await client.post("/api/investigations", headers=auth, json={})).status_code == 429


def test_production_rejects_ephemeral_storage_and_weak_credentials(tmp_path):
    with pytest.raises(RuntimeError, match="POC_USERNAME"):
        validate_deployment(Settings(app_env="production", database_path=tmp_path / "db"))
    with pytest.raises(RuntimeError, match="mounted persistent volume"):
        validate_deployment(Settings(app_env="production", database_path=tmp_path / "db", poc_username="reviewer", poc_password="a-long-enough-password", require_persistent_volume=True))
