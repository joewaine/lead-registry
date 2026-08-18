"""
Shared registry lookup for fractal project pipelines.

Import this from any send/import/add path to ask: "has another project already
touched this lead?"  Matching is exact (normalized domain, email, email-domain,
normalized company name) — the fuzzier substring search stays in the
interactive `leadcheck` CLI where a human judges the result.

    sys.path.insert(0, os.path.expanduser("~/fractal/lead_registry"))
    from registry_check import check, strongest, RegistryUnavailable

    matches = check(email="jane@acme.com", exclude_project="agencylist")
    if strongest(matches) in BLOCKING_STATUSES:
        ...skip the lead...

Callers decide policy: senders should treat RegistryUnavailable as fatal
(refusing to send blind is the point), list-builders may downgrade to a
warning.
"""

import os
import re
import sqlite3

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "registry.db")

# Strongest first — same order the registry/leadcheck use.
STATUS_ORDER = ["replied", "contacted", "suppressed", "queued",
                "audited", "va_result", "lead"]

# A lead in one of these states has been (or is about to be) touched by an
# outreach channel; contacting it from another channel is double-outreach.
BLOCKING_STATUSES = frozenset({"replied", "contacted", "suppressed", "queued"})


class RegistryUnavailable(RuntimeError):
    """registry.db is missing — run `leadcheck --refresh` to build it."""


def _norm_domain(value):
    if not value:
        return None
    v = value.strip().lower()
    v = re.sub(r"^[a-z]+://", "", v)
    v = v.split("/")[0].split("?")[0].split(":")[0]
    if v.startswith("www."):
        v = v[4:]
    v = v.rstrip(".")
    return v if "." in v else None


def _norm_company(value):
    if not value:
        return None
    v = re.sub(r"[^\w\s]", " ", value.lower())
    v = re.sub(r"\s+", " ", v).strip()
    v = re.sub(r"\b(llc|inc|ltd|llp|pllc|pc|corp|co)\.?$", "", v).strip()
    return v or None


def check(domain=None, email=None, company=None, linkedin_url=None,
          exclude_project=None):
    """Return matching registry rows as dicts, strongest status first.

    exclude_project filters out the calling project's own rows so a pipeline
    doesn't block on data it already tracks itself (prefix match, so
    "ai_outreach" also excludes "ai_outreach/network").
    """
    if not os.path.exists(DB_PATH):
        raise RegistryUnavailable(
            f"{DB_PATH} missing — run: leadcheck --refresh")

    clauses, params = [], []
    dom = _norm_domain(domain)
    em = email.strip().lower() if email and "@" in email else None
    if em:
        clauses.append("email = ?")
        params.append(em)
        dom = dom or _norm_domain(em.split("@", 1)[1])
    if dom:
        clauses.append("domain = ?")
        params.append(dom)
        clauses.append("email LIKE ?")
        params.append(f"%@{dom}")
    comp = _norm_company(company)
    if comp:
        clauses.append("company_norm = ?")
        params.append(comp)
    if linkedin_url:
        clauses.append("linkedin_url = ?")
        params.append(linkedin_url.strip())
    if not clauses:
        return []

    sql = f"SELECT * FROM records WHERE ({' OR '.join(clauses)})"
    if exclude_project:
        sql += " AND project NOT LIKE ?"
        params.append(f"{exclude_project}%")

    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()
    rows.sort(key=lambda r: STATUS_ORDER.index(r["status"]))
    return rows


def strongest(matches):
    """The most serious status among the matches, or None if clean."""
    return matches[0]["status"] if matches else None


def summarize(matches, limit=3):
    """One-line human summary for warnings: 'contacted (ai_outreach), ...'."""
    seen, parts = set(), []
    for m in matches:
        key = (m["project"], m["status"])
        if key not in seen:
            seen.add(key)
            parts.append(f"{m['status']} ({m['project']})")
    extra = len(parts) - limit
    return "; ".join(parts[:limit]) + (f" +{extra} more" if extra > 0 else "")
