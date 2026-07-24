from sqlalchemy import create_engine, inspect

from backend.app.persistence.schema import metadata


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
