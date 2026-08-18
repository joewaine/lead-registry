# Lead registry — machine-wide dedupe check

One SQLite database (`registry.db`) holding every lead, contact, and outreach
touch across all fractal projects, so nothing gets double-worked or contacted
twice by different channels. It is a **read-model**: rebuilt from scratch from
the sources on every refresh, never edited by hand, never the system of
record.

The problem it solves: eight systems on one machine each held some notion of
"person we have already contacted" — a service Postgres, several SQLite stores,
generated CSVs, and scraped output. None of them agreed, and none could see the
others, so the same person could be cold-contacted twice by two channels that
had no idea the other existed.

**`registry.db` is not in this repo.** It holds real contact data for thousands
of people and businesses; it is rebuilt locally from your own sources by
`leadcheck --refresh`.

## Usage

```
leadcheck example.com                # domain or URL
leadcheck jane@example.com           # email (also matches other @domain rows)
leadcheck "Some Hospitality Group"  # company name
leadcheck --batch va-results.csv     # annotate a whole CSV -> *-checked.csv
leadcheck --refresh                  # rebuild from all sources
leadcheck --stats                    # row counts + build freshness
```

Exit codes: `0` match found, `1` clean, `2` error — so it scripts:
`leadcheck foo.com || echo "safe to add"`.

Headline verdicts, strongest wins: `replied` > `contacted` > `suppressed` >
`queued` > `audited` > `va_result` > `lead`.

`--batch` looks for a domain/URL/email column first (`Website URL`, `website`,
`url`, `domain`, `email`), falls back to a company-name column, and appends
`registry_status` + `registry_matches` columns. Use it on every VA deliverable
before acting on it.

## Sources (see `SOURCES` in ingest.py)

Each source is one loader function that yields rows into the registry. The
loaders cover four shapes of upstream store, and adding a new one is a single
function plus an entry in `SOURCES`:

| Shape | Example | Statuses produced |
|---|---|---|
| Service database over docker | a pipeline's Postgres: companies, contacts, sent mail, replies, suppression | lead / contacted / replied / suppressed |
| Local SQLite | per-channel agent state (profiles, sends, DMs) | lead / contacted / replied |
| Generated CSV | scraped or researched lead lists, one file per lane or vertical | lead |
| Send-queue + log CSV | a queue file plus the log its sender writes, plus a suppression list | lead / queued / contacted / suppressed |

Two rules the loaders follow, because both cost real duplicates when broken:

- **Skip merge files.** Where a directory holds both per-lane files and an
  `all_lanes.csv` merge of them, only one gets loaded.
- **Skip our own output.** `leadcheck --batch` writes `*-checked.csv` next to
  its input; those are the same rows annotated, and loading them double-counts.

A loader that fails — the service is down, a path moved — is skipped with a
warning and recorded in `meta.failed_sources`. `leadcheck` then surfaces the
gap on every query until a clean rebuild, so a partial registry can never be
mistaken for a complete one.

## Enforcement (code-level, beyond the agent instructions)

A rule in a CLAUDE.md is advisory — it asks a session to behave. The registry
is also wired into the pipelines that can actually touch a lead, at three
different strengths, chosen by how confident the match is:

- **Block** — a batch sender refuses to queue any recipient whose email or
  domain another project has replied-to, contacted, suppressed, or queued, and
  refuses to run at all if the registry is missing. Email and domain matching
  is exact, so blocking is safe. An explicit `--ignore-registry` overrides.
- **Sync** — `leadcheck --sync` pushes every cross-project touch into the main
  outreach pipeline's own `suppression` table, which its intake endpoints
  already enforce. Rows are tagged `cross_project:` so the operation is
  idempotent and reversible with a single DELETE. Run it after any other
  channel sends.
- **Warn** — agents that match on company name or profile URL warn rather than
  block, because name matching is fuzzy and those sends are human-approved
  anyway. A false block is worse than a false warning when a human is already
  in the loop.

Channels with nothing to match on (usernames only, no email or domain) are
documented as unenforced rather than silently assumed covered.

Shared lookup logic lives in `registry_check.py` — exact-match only; the
fuzzier substring search stays in the interactive CLI.

## Behavioral test

`./probe_claude_md.sh` spawns fresh headless Claude sessions with a planted
suppressed lead and greps the transcript for an unprompted leadcheck call —
re-run after editing ~/.claude/CLAUDE.md or upgrading Claude Code.

## Maintenance

- **New leads generated anywhere** → `leadcheck --refresh` (takes ~2s).
- **New lead source (new folder/file/db)** → add a loader function to
  `SOURCES` in `ingest.py`, then refresh. A loader that fails (e.g. docker
  down) is skipped with a warning and recorded in `meta.failed_sources`;
  leadcheck surfaces the gap on every query until a clean rebuild.
- The registry warns when older than 7 days.
- Identity matching: domains are normalized (scheme/www stripped), emails
  lowercased, company names matched with legal suffixes stripped. Domain and
  email are exact; company-name matching is substring — treat name-only
  matches as "go look", not proof.
