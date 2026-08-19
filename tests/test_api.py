from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from app.db import SessionLocal, init_db
from app.main import app
from app.models import Job
from app.security import hash_token


def test_health_and_job_authorization(tmp_path):
    init_db()
    client = TestClient(app)
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    job_id = "authorization-test"
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-test")
    with SessionLocal() as db:
        db.merge(
            Job(
                id=job_id,
                access_token_hash=hash_token("secret"),
                source_filename="x.pdf",
                source_path=str(source),
                target_language="en",
                expires_at=datetime.now(UTC) + timedelta(days=7),
            )
        )
        db.commit()
    assert client.get(f"/api/v1/jobs/{job_id}").status_code == 404
    assert client.get(f"/api/v1/jobs/{job_id}?token=secret").status_code == 200
