#!/usr/bin/env python3
"""
Rebuild the machine-wide lead registry from every lead source in ~/fractal.

The registry is a flat SQLite table of (identity, project, status) rows used
to answer one question fast: "have we already got / touched this lead
anywhere?"  It is a read-model — rebuilt from scratch on every run, never the
system of record. Add new sources by appending a loader to SOURCES below.

Usage:
    python3 ingest.py            # rebuild ~/fractal/lead_registry/registry.db
    python3 ingest.py --quiet    # only print warnings/errors
"""

import csv
import glob
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone

FRACTAL = os.path.expanduser("~/fractal")
HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "registry.db")

# Statuses, strongest first. leadcheck uses this order to pick the headline
# verdict when one entity appears in several places.
STATUS_ORDER = ["replied", "contacted", "suppressed", "queued",
                "audited", "va_result", "lead"]

# ---------------------------------------------------------------- normalizers

_LEGAL_SUFFIXES = re.compile(
    r"\b(llc|inc|ltd|llp|pllc|pc|corp|co)\.?$", re.IGNORECASE)


def norm_domain(value):
    """'https://www.Foo.com/bar' -> 'foo.com'. Returns None if no domain."""
    if not value:
        return None
    v = value.strip().lower()
    v = re.sub(r"^[a-z]+://", "", v)
    v = v.split("/")[0].split("?")[0].split(":")[0]
    if v.startswith("www."):
        v = v[4:]
    v = v.rstrip(".")
    return v if "." in v else None


def norm_email(value):
    if not value:
        return None
    v = value.strip().lower().strip("<>")
    return v if "@" in v else None


def norm_company(value):
    """Lowercase, strip punctuation and trailing legal suffixes, for matching."""
    if not value:
        return None
    v = re.sub(r"[^\w\s]", " ", value.lower())
    v = re.sub(r"\s+", " ", v).strip()
    v = _LEGAL_SUFFIXES.sub("", v).strip()
    return v or None


def split_emails(value):
    if not value:
        return []
    return [e for e in (norm_email(p) for p in re.split(r"[;,\s]+", value)) if e]


# ---------------------------------------------------------------- row helper

class Registry:
    def __init__(self, conn):
        self.conn = conn
        self.counts = {}

    def add(self, project, source_file, status, company=None, domain=None,
            email=None, contact_name=None, linkedin_url=None,
            instagram_username=None, detail=None, last_activity=None):
        if not any([company, domain, email, linkedin_url, instagram_username]):
            return
        self.conn.execute(
            "INSERT INTO records (project, source_file, status, company,"
            " company_norm, domain, email, contact_name, linkedin_url,"
            " instagram_username, detail, last_activity)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (project, source_file, status, company, norm_company(company),
             norm_domain(domain), norm_email(email), contact_name,
             linkedin_url or None, instagram_username, detail, last_activity))
        self.counts[project] = self.counts.get(project, 0) + 1


def read_csv(path, delimiter=","):
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
        yield from csv.DictReader(f, delimiter=delimiter)


# ---------------------------------------------------------------- sources

def load_ai_outreach_pg(reg):
    """Contacts, companies, sends, replies, suppression from the docker
    postgres. This is the only source with real send history — if it can't be
    reached the build still succeeds but the gap is recorded in meta."""
    def psql(query):
        out = subprocess.run(
            ["docker", "exec", "ai-outreach-postgres-1", "psql",
             "-U", "outreach", "-d", "outreach", "--csv", "-c", query],
            capture_output=True, text=True, timeout=30)
        if out.returncode != 0:
            raise RuntimeError(out.stderr.strip()[:200])
        return list(csv.DictReader(out.stdout.splitlines()))

    src = "docker:ai-outreach-postgres-1/outreach"
    rows = psql("""
        SELECT ct.email, ct.full_name, ct.title, ct.linkedin_url,
               co.domain, co.name AS company, co.track::text AS track,
               to_char(max(e.sent_at), 'YYYY-MM-DD') AS last_sent,
               count(r.id) AS replies
        FROM contacts ct
        JOIN companies co ON co.id = ct.company_id
        LEFT JOIN emails e ON e.contact_id = ct.id AND e.status = 'sent'
        LEFT JOIN replies r ON r.email_id = e.id
        GROUP BY ct.id, co.id""")
    for r in rows:
        if int(r["replies"] or 0) > 0:
            status = "replied"
        elif r["last_sent"]:
            status = "contacted"
        else:
            status = "lead"
        reg.add("ai_outreach", src, status, company=r["company"],
                domain=r["domain"], email=r["email"],
                contact_name=r["full_name"], linkedin_url=r["linkedin_url"],
                detail=f"track={r['track']} title={r['title'] or '-'}",
                last_activity=r["last_sent"])

    for r in psql("""
        SELECT co.domain, co.name AS company, co.track::text AS track
        FROM companies co
        WHERE NOT EXISTS (SELECT 1 FROM contacts ct
                          WHERE ct.company_id = co.id)"""):
        reg.add("ai_outreach", src, "lead", company=r["company"],
                domain=r["domain"], detail=f"track={r['track']} (no contact)")

    for r in psql("""
        SELECT email, domain, reason, notes,
               to_char(created_at, 'YYYY-MM-DD') AS created
        FROM suppression"""):
        reg.add("ai_outreach", src, "suppressed", domain=r["domain"],
                email=r["email"],
                detail=f"{r['reason']}: {r['notes'] or ''}".strip(": "),
                last_activity=r["created"])


def load_claude_leadgen(reg):
    # all_lanes.csv is a merge of the lane files — skip it to avoid doubles.
    for path in sorted(glob.glob(
            f"{FRACTAL}/claude_lead_generation/leads/lane*.csv")):
        if path.endswith("-checked.csv"):
            continue
        src = os.path.relpath(path, FRACTAL)
        for r in read_csv(path):
            reg.add("claude_lead_generation", src, "lead",
                    company=r.get("company"), domain=r.get("website"),
                    email=r.get("email"), contact_name=r.get("contact_name"),
                    linkedin_url=r.get("linkedin_url"),
                    detail=f"lane={r.get('lane')} {r.get('city') or ''}".strip())


def _load_leadgen_dir(reg, project):
    # -checked.csv files are leadcheck output over the same rows — skip them
    # or every lead ingests twice.
    for path in sorted(glob.glob(f"{FRACTAL}/{project}/leads/*.csv")):
        if path.endswith("-checked.csv"):
            continue
        src = os.path.relpath(path, FRACTAL)
        for r in read_csv(path):
            low = {k.strip().lower(): v for k, v in r.items() if k}
            reg.add(project, src, "lead",
                    company=low.get("company") or low.get("agency"),
                    domain=low.get("website") or low.get("domain") or low.get("url"),
                    email=low.get("email"),
                    contact_name=low.get("contact_name") or low.get("name"),
                    linkedin_url=low.get("linkedin_url") or low.get("linkedin"))


def load_shaun_leadgen(reg):
    _load_leadgen_dir(reg, "shaun_lead_generation")


def load_nick_leadgen(reg):
    _load_leadgen_dir(reg, "nick_lead_generation")


def load_restaurant_groups(reg):
    path = f"{FRACTAL}/restaurant_groups/nyc_restaurant_groups_contacts.csv"
    src = os.path.relpath(path, FRACTAL)
    for r in read_csv(path):
        emails = (split_emails(r.get("person_emails"))
                  + split_emails(r.get("role_emails"))
                  + split_emails(r.get("external_emails")))
        for email in emails or [None]:
            reg.add("restaurant_groups", src, "lead", company=r.get("group"),
                    domain=r.get("website"), email=email,
                    detail=f"type={r.get('type')}")


def load_agencylist(reg):
    base = f"{FRACTAL}/agencylist"
    path = f"{base}/agency-contacts.csv"
    for r in read_csv(path):
        reg.add("agencylist", os.path.relpath(path, FRACTAL), "lead",
                company=r.get("agency"), domain=r.get("domain"),
                email=r.get("primary_email"))
        alt = norm_email(r.get("alt_email"))
        if alt:
            reg.add("agencylist", os.path.relpath(path, FRACTAL), "lead",
                    company=r.get("agency"), domain=r.get("domain"), email=alt)

    path = f"{base}/outreach-merge.csv"
    for r in read_csv(path):
        reg.add("agencylist", os.path.relpath(path, FRACTAL), "queued",
                company=r.get("agency"), email=r.get("email"),
                detail=f"priority={r.get('priority')}")

    # sent.csv is written by send_batch.py once sending starts.
    path = f"{base}/sent.csv"
    if os.path.exists(path):
        for r in read_csv(path):
            low = {k.strip().lower(): v for k, v in r.items() if k}
            reg.add("agencylist", os.path.relpath(path, FRACTAL), "contacted",
                    company=low.get("agency"), email=low.get("email"),
                    last_activity=low.get("sent_at") or low.get("date"))

    path = f"{base}/suppress.txt"
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            reg.add("agencylist", os.path.relpath(path, FRACTAL), "suppressed",
                    email=line if "@" in line else None,
                    domain=None if "@" in line else line,
                    detail="suppress.txt")


def load_tech_list(reg):
    path = f"{FRACTAL}/linkedinsalesnavnames/nyc-tech-contacts.csv"
    src = os.path.relpath(path, FRACTAL)
    for r in read_csv(path):
        emails = ([norm_email(r.get("hiring_email"))]
                  if norm_email(r.get("hiring_email")) else [])
        emails += split_emails(r.get("other_emails"))
        for email in emails or [None]:
            reg.add("linkedinsalesnavnames", src, "lead",
                    company=r.get("company"), domain=r.get("website"),
                    email=email, detail="tech list (retired vertical)")


def load_site_audit(reg):
    db = f"{FRACTAL}/site_audit/db/audit.sqlite"
    if os.path.exists(db):
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            for domain, date, handed in conn.execute(
                    "SELECT domain, max(date), max(handed_off_at)"
                    " FROM runs GROUP BY domain"):
                reg.add("site_audit", "site_audit/db/audit.sqlite", "audited",
                        domain=domain,
                        detail="handed off" if handed else "audit run",
                        last_activity=(handed or date or "")[:10] or None)
        finally:
            conn.close()

    # All VA deliverables/QC files land in picks/ as va-*.csv (trial rounds,
    # scored real batches). The Google Sheet the VA fills is not reachable
    # from here, so the scored CSV written at QC time is the registry-visible
    # copy — QC every sheet batch into one or the registry can't see it.
    for path in sorted(glob.glob(f"{FRACTAL}/site_audit/picks/va-*.csv")):
        if path.endswith("-checked.csv"):
            continue
        src = os.path.relpath(path, FRACTAL)
        for r in read_csv(path):
            low = {k.strip().lower(): v for k, v in r.items() if k}
            note = (low.get("comments") or low.get("derick_note") or "")[:200]
            verdict = low.get("match_per_task")
            detail = (f"match={verdict} {note}".strip() if verdict
                      else note or None)
            reg.add("site_audit", src, "va_result",
                    company=low.get("company name") or low.get("company"),
                    domain=low.get("website url") or low.get("domain"),
                    detail=detail)

    path = f"{FRACTAL}/site_audit/picks/derick-draftable.tsv"
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                d = norm_domain(line.split("\t")[0])
                if d:
                    reg.add("site_audit", os.path.relpath(path, FRACTAL),
                            "va_result", domain=d, detail="derick draftable")

    # Inbound self-serve submissions from joewaine.com/audit — the worker
    # (tools/webaudit_worker.mjs) appends one JSON line per completed/failed
    # job. These are warm inbound leads: the submitter gave their email.
    path = f"{FRACTAL}/site_audit/db/web_requests.jsonl"
    if os.path.exists(path):
        src = os.path.relpath(path, FRACTAL)
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                reg.add("site_audit", src, "inbound_request",
                        domain=r.get("domain"), email=norm_email(r.get("email")),
                        detail=f"self-serve audit {r.get('status', '')}".strip(),
                        last_activity=(r.get("ts") or "")[:10] or None)


def load_instagram(reg):
    db = f"{FRACTAL}/instagram/leads.db"
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        for user, status, sent_at, replied_at in conn.execute(
                "SELECT username, status, sent_at, replied_at FROM leads"):
            if replied_at:
                st = "replied"
            elif sent_at:
                st = "contacted"
            else:
                st = "lead"
            reg.add("instagram", "instagram/leads.db", st,
                    instagram_username=user, detail=f"ig status={status}",
                    last_activity=(replied_at or sent_at or "")[:10] or None)
    finally:
        conn.close()


def load_linkedinagent(reg):
    db = f"{FRACTAL}/linkedinagent/data/leadgen.db"
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        for name, url, company, status, acted in conn.execute("""
                SELECT l.name, l.url, l.company, l.status,
                       max(CASE WHEN a.type IN ('connect','dm') THEN a.at END)
                FROM leads l LEFT JOIN actions a ON a.lead_id = l.id
                GROUP BY l.id"""):
            st = ("replied" if status == "replied"
                  else "contacted" if acted or status == "sent"
                  else "lead")
            reg.add("linkedinagent", "linkedinagent/data/leadgen.db", st,
                    contact_name=name, company=company, linkedin_url=url,
                    detail=f"li status={status}",
                    last_activity=(acted or "")[:10] or None)
    finally:
        conn.close()


def load_ai_outreach_network(reg):
    base = f"{FRACTAL}/ai_outreach/network"
    for fname in ("studio_contacts.csv", "test_leads.csv",
                  "ai_services_seed.csv"):
        path = f"{base}/{fname}"
        if not os.path.exists(path):
            continue
        src = os.path.relpath(path, FRACTAL)
        for r in read_csv(path):
            low = {k.strip().lower(): v for k, v in r.items() if k}
            emails = split_emails(low.get("all_emails")) or []
            best = norm_email(low.get("best_email") or low.get("email"))
            if best and best not in emails:
                emails.insert(0, best)
            for email in emails or [None]:
                reg.add("ai_outreach/network", src, "lead",
                        company=low.get("company"),
                        domain=low.get("domain") or low.get("url"),
                        email=email, contact_name=low.get("name"),
                        detail=low.get("track") or low.get("note"))


def load_warm_followups(reg):
    """Gmail-history warm follow-up lists (fractal/warm_followups/*.csv).

    Built by scanning joe.waine@gmail.com for past clients/prospects. The
    "-checked" variants are leadcheck output of the same rows — skip them.
    """
    for path in sorted(glob.glob(f"{FRACTAL}/warm_followups/*.csv")):
        if path.endswith("-checked.csv"):
            continue
        if os.path.basename(path).startswith("sent-"):
            # Send log: warm emails actually sent from Joe's Gmail.
            for r in read_csv(path):
                reg.add("warm_followups", os.path.relpath(path, FRACTAL),
                        (r.get("status") or "contacted").strip(),
                        email=r.get("email"), contact_name=r.get("name"),
                        detail=r.get("note"), last_activity=r.get("sent_at"))
            continue
        for r in read_csv(path):
            tier = (r.get("tier") or "").strip()
            # X-skip rows are known do-not-touch (active clients, family,
            # already in the 2026 pipeline) — record them as suppressed so
            # cold channels keep away from them too.
            status = "suppressed" if tier.startswith("X-skip") else "lead"
            detail = f"warm-followup {tier}" if tier else "warm-followup"
            emails = [e.strip() for e in
                      ([r.get("email") or ""] +
                       (r.get("alt_emails") or "").split(";")) if e.strip()]
            for email in emails or [None]:
                reg.add("warm_followups", os.path.relpath(path, FRACTAL),
                        status, company=r.get("company"), email=email,
                        contact_name=r.get("name"), detail=detail,
                        last_activity=r.get("last_seen"))


def load_cash_rich(reg):
    """Google Places sourcing of the 100 cash-rich business categories.
    Only all_leads.csv (the merge) is loaded — the per-category CSVs in the
    same dir are the same rows split out, and would double-count."""
    path = f"{FRACTAL}/cash_rich/leads/all_leads.csv"
    if not os.path.exists(path):
        return
    src = os.path.relpath(path, FRACTAL)
    for r in read_csv(path):
        reg.add("cash_rich", src, "lead",
                company=r.get("business_name"), domain=r.get("website"),
                detail=f"category={r.get('category')} tier={r.get('tier')}")


SOURCES = [
    ("ai_outreach postgres", load_ai_outreach_pg),
    ("claude_lead_generation", load_claude_leadgen),
    ("shaun_lead_generation", load_shaun_leadgen),
    ("nick_lead_generation", load_nick_leadgen),
    ("restaurant_groups", load_restaurant_groups),
    ("agencylist", load_agencylist),
    ("linkedinsalesnavnames", load_tech_list),
    ("site_audit", load_site_audit),
    ("instagram", load_instagram),
    ("linkedinagent", load_linkedinagent),
    ("ai_outreach network CSVs", load_ai_outreach_network),
    ("warm_followups", load_warm_followups),
    ("cash_rich", load_cash_rich),
]


def main():
    quiet = "--quiet" in sys.argv
    tmp = DB_PATH + ".build"
    if os.path.exists(tmp):
        os.remove(tmp)
    conn = sqlite3.connect(tmp)
    conn.executescript("""
        CREATE TABLE records (
            id INTEGER PRIMARY KEY,
            project TEXT NOT NULL,
            source_file TEXT NOT NULL,
            status TEXT NOT NULL,
            company TEXT, company_norm TEXT,
            domain TEXT, email TEXT,
            contact_name TEXT, linkedin_url TEXT, instagram_username TEXT,
            detail TEXT, last_activity TEXT
        );
        CREATE INDEX idx_records_domain ON records(domain);
        CREATE INDEX idx_records_email ON records(email);
        CREATE INDEX idx_records_company ON records(company_norm);
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
    """)
    reg = Registry(conn)
    failed = []
    for label, loader in SOURCES:
        try:
            loader(reg)
        except Exception as exc:  # a dead source must not sink the build
            failed.append(f"{label}: {exc}")
            print(f"WARN  {label} failed: {exc}", file=sys.stderr)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute("INSERT INTO meta VALUES ('built_at', ?)", (now,))
    conn.execute("INSERT INTO meta VALUES ('failed_sources', ?)",
                 ("; ".join(failed),))
    conn.commit()
    conn.close()
    os.replace(tmp, DB_PATH)

    if not quiet:
        total = sum(reg.counts.values())
        print(f"registry rebuilt: {total} rows -> {DB_PATH}")
        for project in sorted(reg.counts):
            print(f"  {reg.counts[project]:>6}  {project}")
    if failed:
        print(f"NOTE: {len(failed)} source(s) failed — registry is missing "
              "their data until the next successful --refresh.",
              file=sys.stderr)
        sys.exit(3)


if __name__ == "__main__":
    main()
