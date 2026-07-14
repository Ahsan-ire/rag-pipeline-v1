# docs/designs/ — design artifacts

A design that is about to be implemented, or contested in a bake-off, lives
here as a file **before** any code is written. The artifact — not the chat
that produced it — is what implementation gets checked against, and it is the
only thing a fresh-context critic is shown.

Relation to the other records:

- **docs/decisions.md** is the append-only ledger: one short what/why/rejected
  entry per choice. Every design here still gets a ledger entry when decided.
- **docs/designs/** is for designs too big for a ledger entry, and for all
  bake-off briefs. The ledger entry links back to the artifact.
- **IMPLEMENTATION_PLAN.md** phase sections count as design artifacts for
  phase-scoped work; they go through the same plan gate without duplication
  here.

Conventions:

- Filename: `NNN-<slug>.md`, NNN strictly increasing (`001-...`, `002-...`).
  Bake-off briefs: `NNN-bakeoff-<slug>.md`.
- Start from `TEMPLATE.md`. Keep the `Status` line current:
  `draft` → `reviewed` (plan gate READY) → `implemented` → `superseded by NNN`.
- Critique findings and their dispositions are appended under `## Review` by
  the plan gate — they are part of the design record, not chat ephemera.
- Artifacts are never deleted. A rejected or superseded design is
  documentation of a road not taken, which is half the value of keeping it.
