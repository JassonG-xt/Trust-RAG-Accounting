# Wiki Schema and Conventions

Conventions the ingest agent must follow when compiling this wiki. Seeded from
a committed template; the agent may extend the prose but not the invariants.

## Page types

`client` | `policy` | `invoice_rule` | `concept` | `source_summary` | `answer`

Each page lives under the directory for its type (`clients/`, `policies/`,
`invoice_rules/`, `concepts/`, `sources/`, `answers/`) and is named
`<page_id>.md` so `[[page_id]]` wikilinks resolve in Obsidian.

## Front matter

Every page carries `page_id`, `page_type`, `title`, `client` (null = global),
`status` (`active` | `superseded`), `valid_from`, `valid_to`, `superseded_by`,
`sources`, `revision`, `updated`.

## Invariants (enforced by tier-1 lint)

1. **Client isolation.** A `client: X` page cites only sources owned by X or
   global.
2. **Citation bridge.** Every page has a non-empty `sources` list and every
   `[[wikilink]]` resolves to an existing page.
3. **Temporal pairing.** At most one `active` page per supersession lineage;
   `superseded` pages carry a resolving `superseded_by`.
4. **Index consistency.** `index.md` lists every page exactly once; `log.md`
   entries follow `## [YYYY-MM-DD] <op> | <title>`.
