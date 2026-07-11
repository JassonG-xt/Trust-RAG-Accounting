# Production Persistence

TrustRAG keeps filesystem implementations as the zero-dependency development
default and provides Postgres and S3-compatible implementations behind the
same application seams.

## Initial rollout

1. Install `pip install -e '.[production]'`.
2. Set `DATABASE_URL` and run `alembic upgrade head`.
3. Import legacy JSON/JSONL with `trustrag-import-legacy`.
4. Compare document, version, chunk, checkpoint, and action counts.
5. Set `TRUSTRAG_STORAGE_BACKEND=postgres` and a stable
   `TRUSTRAG_TENANT_ID`.
6. Keep the legacy files read-only for one release before archiving them.

The import command is idempotent. Review actions are inserted in historical
order and their `previous_status` must match the stored state projection.

## Consistency and rollback

- Postgres review writes lock the checkpoint row and reject stale status
  transitions instead of silently accepting concurrent reviewer decisions.
- Every production row is scoped by `tenant_id`; the business `client` field
  remains document metadata and is not a security boundary.
- Switching `TRUSTRAG_STORAGE_BACKEND` back to `local` restores the local demo
  implementation. It does not copy newer Postgres records back into JSONL.
- Original files stored through the S3 adapter use checksum-addressed keys, so
  identical uploads resolve to the same immutable URI.

Do not run `metadata.create_all()` in production. Alembic is the authoritative
schema migration path.
