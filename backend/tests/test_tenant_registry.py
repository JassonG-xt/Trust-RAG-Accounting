from sqlalchemy import create_engine, inspect

from backend.app.persistence.schema import metadata
from backend.app.persistence.tenants import TenantRegistryRepository


def test_tenants_table_is_defined_in_metadata():
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    columns = {c["name"] for c in inspect(engine).get_columns("tenants")}
    assert columns == {
        "tenant_id",
        "name",
        "status",
        "created_at",
        "updated_at",
        "config_json",
    }


def _engine():
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    return engine


def test_registry_create_get_list_and_is_active():
    repo = TenantRegistryRepository(_engine())
    created = repo.create("alpha-firm", "Alpha Firm", now="2026-07-24T00:00:00+00:00")
    assert created.tenant_id == "alpha-firm"
    assert repo.get("alpha-firm").name == "Alpha Firm"
    assert repo.is_active("alpha-firm") is True
    assert repo.get("missing") is None
    assert [t.tenant_id for t in repo.list_active()] == ["alpha-firm"]


def test_registry_suspended_tenant_is_not_active():
    repo = TenantRegistryRepository(_engine())
    repo.create("beta-firm", "Beta Firm", now="2026-07-24T00:00:00+00:00", status="suspended")
    assert repo.is_active("beta-firm") is False
    assert repo.list_active() == []
