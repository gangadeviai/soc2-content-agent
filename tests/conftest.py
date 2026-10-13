import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.brand import FakeFetcher  # noqa: E402
from app.cms import FakePublisher  # noqa: E402
from app.config import Settings  # noqa: E402
from app.demo_data import DEMO_SITE  # noqa: E402, F401  (re-exported for tests)
from app.llm import FakeLLMClient  # noqa: E402
from app.main import create_app  # noqa: E402


@pytest.fixture
def settings(tmp_path) -> Settings:
    os.environ.pop("AGENT_ENV", None)
    return Settings(
        env="test",
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        audit_local_dir=tmp_path / "audit",
        _env_file=None,
    )


@pytest.fixture
def fake_llm() -> FakeLLMClient:
    return FakeLLMClient()


@pytest.fixture
def publisher() -> FakePublisher:
    return FakePublisher()


@pytest.fixture
def client(settings, fake_llm, publisher):
    app = create_app(
        settings,
        llm=fake_llm,
        publisher_factory=lambda tenant_id: publisher,
        fetcher=FakeFetcher(DEMO_SITE),
    )
    with TestClient(app) as c:
        yield c


def token(client, sub, tenant, roles="requester") -> dict:
    t = client.post("/dev/token", params={"sub": sub, "tenant_id": tenant, "roles": roles}).json()["token"]
    return {"Authorization": f"Bearer {t}"}
