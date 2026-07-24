from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import Engine, insert, select

from .schema import tenants


@dataclass(frozen=True)
class TenantRecord:
    tenant_id: str
    name: str
    status: str
    created_at: str
    updated_at: str
    config: dict = field(default_factory=dict)


class TenantRegistryRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def create(
        self,
        tenant_id: str,
        name: str,
        *,
        now: str,
        status: str = "active",
        config: dict | None = None,
    ) -> TenantRecord:
        record = TenantRecord(tenant_id, name, status, now, now, config or {})
        with self._engine.begin() as connection:
            connection.execute(
                insert(tenants).values(
                    tenant_id=tenant_id,
                    name=name,
                    status=status,
                    created_at=now,
                    updated_at=now,
                    config_json=record.config,
                )
            )
        return record

    def get(self, tenant_id: str) -> TenantRecord | None:
        with self._engine.connect() as connection:
            row = connection.execute(
                select(tenants).where(tenants.c.tenant_id == tenant_id)
            ).mappings().one_or_none()
        if row is None:
            return None
        return TenantRecord(
            tenant_id=row["tenant_id"],
            name=row["name"],
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            config=dict(row["config_json"] or {}),
        )

    def list_active(self) -> list[TenantRecord]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                select(tenants).where(tenants.c.status == "active").order_by(tenants.c.tenant_id)
            ).mappings().all()
        return [
            TenantRecord(
                r["tenant_id"], r["name"], r["status"],
                r["created_at"], r["updated_at"], dict(r["config_json"] or {}),
            )
            for r in rows
        ]

    def is_active(self, tenant_id: str) -> bool:
        record = self.get(tenant_id)
        return record is not None and record.status == "active"
