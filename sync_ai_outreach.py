#!/usr/bin/env python3
"""
Push cross-project outreach history into ai_outreach's suppression table.

ai_outreach already refuses suppressed leads at intake (`/api/leads/import`
and `/api/leads/:id/contact`), but its suppression table only knows about its
own sends. This script feeds it every email/domain another fractal project has
replied-to, contacted, suppressed, or queued — so the pipeline's own tested
enforcement point covers the whole machine, with no code running in docker.

Rows are tagged `notes = 'cross_project: ...'` (the reason enum has no
cross_project value; already_contacted is the honest fit). Idempotent via the
table's unique indexes + ON CONFLICT DO NOTHING. Undo everything this script
ever did with:

    DELETE FROM suppression WHERE notes LIKE 'cross_project:%';

Usage:
    python3 sync_ai_outreach.py            # sync
    python3 sync_ai_outreach.py --dry-run  # show what would be inserted
    python3 sync_ai_outreach.py --no-queued  # only replied/contacted/suppressed

Run after `leadcheck --refresh` whenever another channel has sent anything.
"""

import os
import sqlite3
import subprocess
import sys

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "registry.db")
PG = ["docker", "exec", "-i", "ai-outreach-postgres-1",
      "psql", "-U", "outreach", "-d", "outreach", "-v", "ON_ERROR_STOP=1"]


def sql_str(value):
    return "'" + value.replace("'", "''") + "'"


def main():
    dry = "--dry-run" in sys.argv
    statuses = ["replied", "contacted", "suppressed"]
    if "--no-queued" not in sys.argv:
        statuses.append("queued")

    if not os.path.exists(DB_PATH):
        sys.exit(f"{DB_PATH} missing — run: leadcheck --refresh")
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    placeholders = ",".join("?" * len(statuses))
    rows = conn.execute(
        f"""SELECT DISTINCT email, domain, status, project
            FROM records
            WHERE status IN ({placeholders})
              AND project NOT LIKE 'ai_outreach%'
              AND (email IS NOT NULL OR domain IS NOT NULL)""",
        statuses).fetchall()
    conn.close()

    if not rows:
        print("nothing to sync")
        return

    values = []
    for email, domain, status, project in rows:
        note = f"cross_project: {status} in {project} (lead_registry sync)"
        values.append(
            f"({sql_str(email) if email else 'NULL'},"
            f" {sql_str(domain) if email is None and domain else 'NULL'},"
            f" 'already_contacted', {sql_str(note)})")

    if dry:
        print(f"would sync {len(values)} row(s):")
        for email, domain, status, project in rows[:20]:
            print(f"  {email or domain}  [{status} in {project}]")
        if len(rows) > 20:
            print(f"  ... +{len(rows) - 20} more")
        return

    # Email rows conflict on the email index, domain-only rows on the domain
    # index — two statements so each can name its conflict target.
    email_vals = [v for v in values if not v.startswith("(NULL")]
    domain_vals = [v for v in values if v.startswith("(NULL")]
    script = ""
    if email_vals:
        script += (
            "INSERT INTO suppression (email, domain, reason, notes) VALUES\n"
            + ",\n".join(email_vals)
            + "\nON CONFLICT (email) WHERE email IS NOT NULL DO NOTHING;\n")
    if domain_vals:
        script += (
            "INSERT INTO suppression (email, domain, reason, notes) VALUES\n"
            + ",\n".join(domain_vals)
            + "\nON CONFLICT (domain) WHERE domain IS NOT NULL DO NOTHING;\n")

    out = subprocess.run(PG, input=script, capture_output=True, text=True,
                         timeout=60)
    if out.returncode != 0:
        sys.exit(f"psql failed: {out.stderr.strip()[:500]}")
    inserted = sum(int(line.split()[-1])
                   for line in out.stdout.splitlines()
                   if line.startswith("INSERT"))
    print(f"synced: {inserted} new suppression row(s) "
          f"({len(values) - inserted} already present)")


if __name__ == "__main__":
    main()
