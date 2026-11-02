"""The living reconciliation's process: build what the rec reads, then prepare,
review, approve or auto sign off, reopen when the rec changes, and snapshot.

The rec itself is the three views in sql/views/rec.sql.  This module only writes
the platform tables they read (the prepaid schedule, the fingerprints, the synced
comments) and the rec tables that record what people and rules did (the sign-off
log and the snapshots).
"""

import hashlib
from datetime import timedelta

from openpyxl import Workbook

from gate import ROOT

SNAPSHOTS = ROOT / "build" / "snapshots"
COMMENTS_FILE = ROOT / "data" / "comments" / "comments.csv"
GENESIS = "0" * 64
SIGNED = ("approved", "auto signed off")


def set_as_at(con, when):
    con.execute(f"SET VARIABLE as_at = TIMESTAMP '{when:%Y-%m-%d %H:%M:%S}'")


def shift_period(period, months):
    y, m = map(int, period.split("-"))
    n = y * 12 + m - 1 + months
    return f"{n // 12:04d}-{n % 12 + 1:02d}"


# What the rec reads ---------------------------------------------------------------

def refresh(con, when):
    """Rebuild the prepaid schedule from the evidence on the platform, and sync the
    comments list.  Both only see what existed at `when`."""
    set_as_at(con, when)
    con.execute("DELETE FROM platform.prepaid_schedule")
    con.execute("""
        INSERT INTO platform.prepaid_schedule
        SELECT
            k.prepaid_item_id, d.company_code, k.account_id,
            (SELECT any_value(a.account_id) FROM platform.journal_lines AS a
             WHERE a.posting_type = 'amortisation run' AND a.company_code = d.company_code
               AND a.account_id <> k.account_id
               AND a.line_text = 'Amortisation ' || k.prepaid_item_id) AS expense_account_id,
            j.cost_centre_id, d.vendor_id, d.source_document_id, j.document_number, d.amount,
            d.service_start, d.service_end,
            datediff('month', d.service_start, d.service_end) + 1 AS months,
            'straight line, monthly' AS method
        FROM platform.document_links AS k
        JOIN platform.source_documents AS d USING (source_document_id)
        JOIN platform.document_links AS jl
          ON jl.source_document_id = k.source_document_id AND jl.linked_to = 'journal line'
         AND jl.account_id = k.account_id AND jl.linked_at <= getvariable('as_at')
        JOIN platform.journal_lines AS j
          ON j.company_code = jl.company_code AND j.document_number = jl.document_number
         AND j.line_number = jl.line_number
        WHERE k.linked_to = 'prepaid item' AND k.link_type = 'primary'
          AND k.linked_at <= getvariable('as_at') AND d.uploaded_at <= getvariable('as_at')
          AND NOT d.is_deleted AND d.superseded_by_version IS NULL
    """)
    con.execute(f"""
        INSERT INTO rec.comments
        SELECT c.*, getvariable('as_at') AS synced_at
        FROM read_csv('{COMMENTS_FILE.as_posix()}', header = true, all_varchar = true) AS c
        WHERE CAST(c.created_at AS TIMESTAMP) <= getvariable('as_at')
          AND c.comment_id NOT IN (SELECT comment_id FROM rec.comments)
    """)


def build_fingerprints(con, company, period, when):
    """The profile of what normally goes into each account with misuse rules,
    from the months before `period` plus a sample month from the year before."""
    lookback = int(param(con, company, "fingerprint_lookback_months"))
    offset = int(param(con, company, "fingerprint_sample_month_offset"))
    built_from, built_to = shift_period(period, -lookback), shift_period(period, -1)
    sample = shift_period(period, -offset)
    set_as_at(con, when)
    con.execute("""
        INSERT INTO platform.fingerprints
        WITH l AS (
            SELECT j.*,
                   EXISTS (SELECT 1 FROM platform.document_links AS k
                           WHERE k.linked_to = 'journal line' AND k.company_code = j.company_code
                             AND k.document_number = j.document_number AND k.line_number = j.line_number
                             AND k.linked_at <= getvariable('as_at')) AS has_document,
                   count(*) OVER (PARTITION BY j.account_id) AS total
            FROM platform.journal_lines AS j
            WHERE j.company_code = $company
              AND j.account_id IN (SELECT account_id FROM master.misuse_rules WHERE active)
              AND (j.fiscal_period BETWEEN $built_from AND $built_to OR j.fiscal_period = $sample)
        ), dims AS (
            SELECT account_id, 'posting type' AS dimension, posting_type AS value, count(*) AS n,
                   count(*) / any_value(total) AS share FROM l GROUP BY ALL
            UNION ALL
            SELECT account_id, 'user', entered_by, count(*), count(*) / any_value(total) FROM l GROUP BY ALL
            UNION ALL
            SELECT account_id, 'cost centre', cost_centre_id, count(*), count(*) / any_value(total)
            FROM l WHERE cost_centre_id IS NOT NULL GROUP BY ALL
            UNION ALL
            SELECT account_id, 'vendor', vendor_id, count(*), count(*) / any_value(total)
            FROM l WHERE vendor_id IS NOT NULL GROUP BY ALL
            UNION ALL
            SELECT account_id, 'document-link rate', posting_type, count(*), avg(has_document::INTEGER)
            FROM l GROUP BY ALL
        )
        SELECT $company, account_id, $built_from, $built_to, $sample, dimension, value, n, share,
               getvariable('as_at')
        FROM dims
    """, {"company": company, "built_from": built_from, "built_to": built_to, "sample": sample})


def param(con, company, key):
    return con.execute("SELECT param(?, ?)", [company, key]).fetchone()[0]


# Reading the rec -------------------------------------------------------------------

def rec_row(con, key, when):
    set_as_at(con, when)
    cur = con.execute("""SELECT * FROM rec.reconciliation
                         WHERE company_code = ? AND account_id = ? AND period = ?""", list(key))
    names = [d[0] for d in cur.description]
    return dict(zip(names, cur.fetchone()))


def accepted_since_reopen(con, key):
    rows = con.execute("""SELECT step, outcome, person_id, rec_checksum_at_signing FROM rec.signoff_log
                          WHERE company_code = ? AND account_id = ? AND period = ?
                          ORDER BY signoff_id""", list(key)).fetchall()
    steps = []
    for step, outcome, person, checksum in rows:
        if step == "reopened":
            steps = []
        elif outcome == "accepted":
            steps.append((step, person, checksum))
    return steps


def explained(con, key, when):
    """Every exception has an owner's comment, and any difference has a comment
    on the rec itself."""
    set_as_at(con, when)
    unexplained = con.execute("""
        SELECT count(*) FROM rec.exceptions AS e
        WHERE e.company_code = ? AND e.account_id = ? AND e.period = ?
          AND NOT EXISTS (SELECT 1 FROM rec.comments AS c
                          WHERE c.company_code = e.company_code AND c.account_id = e.account_id
                            AND c.period = e.period AND c.exception_rule_id = e.rule_id
                            AND (c.prepaid_item_id IS NULL OR c.prepaid_item_id = e.prepaid_item_id))
    """, list(key)).fetchone()[0]
    rec_comment = con.execute("""
        SELECT count(*) FROM rec.comments
        WHERE company_code = ? AND account_id = ? AND period = ?
          AND prepaid_item_id IS NULL AND exception_rule_id IS NULL
    """, list(key)).fetchone()[0]
    r = rec_row(con, key, when)
    return unexplained == 0 and r["documents_missing"] == 0 and (r["difference"] == 0 or rec_comment > 0)


# The sign-off log: append-only, hash-chained ----------------------------------------

def row_payload(signoff_id, company, account, period, step, person, signed_at, seen, at_signing,
                outcome, note, previous_row_hash):
    values = [signoff_id, company, account, period, step, person, f"{signed_at:%Y-%m-%d %H:%M:%S}",
              seen, at_signing, outcome, note, previous_row_hash]
    return "|".join("" if v is None else str(v) for v in values)


def append(con, key, step, person, when, seen, at_signing, outcome, note=None):
    last = con.execute("SELECT signoff_id, row_hash FROM rec.signoff_log ORDER BY signoff_id DESC LIMIT 1").fetchone()
    signoff_id, previous = (last[0] + 1, last[1]) if last else (1, GENESIS)
    row_hash = hashlib.sha256(row_payload(signoff_id, *key, step, person, when, seen, at_signing,
                                          outcome, note, previous).encode("utf-8")).hexdigest()
    con.execute("INSERT INTO rec.signoff_log VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [signoff_id, *key, step, person, when, seen, at_signing, outcome, note, previous, row_hash])
    return signoff_id


def sign(con, key, step, person, when, seen):
    """Record a sign-off step, if the rules allow it.  The checksum the signer saw
    must be the rec's checksum now; a mismatch rejects the sign-off and reopens the rec."""
    r = rec_row(con, key, when)
    preparers = {p for s, p, _ in accepted_since_reopen(con, key) if s == "prepared"}
    preparers |= {row[0] for row in con.execute(
        "SELECT linked_person_id FROM master.people WHERE person_id IN (SELECT unnest(?))",
        [list(preparers)]).fetchall() if row[0]}
    reason = None
    if not r["journals_closed"]:
        reason = "journals not closed"
    elif not r["tie_out_passed"]:
        reason = "tie-out not passed"
    elif seen != r["rec_checksum"]:
        reason = "checksum mismatch"
    elif step == "approved" and person in preparers:
        reason = "same person as preparer"
    if reason:
        append(con, key, "rejected", person, when, seen, r["rec_checksum"], reason, f"{step} rejected")
        if reason == "checksum mismatch":
            append(con, key, "reopened", "rule_engine", when, None, r["rec_checksum"], "accepted",
                   "The rec changed after the approver opened it")
        return None
    signoff_id = append(con, key, step, person, when, seen, r["rec_checksum"], "accepted")
    if step in SIGNED:
        snapshot(con, key, signoff_id, when)
    return signoff_id


def reopen_check(con, keys, when):
    """A signed rec whose checksum has changed goes back to uncertified."""
    for key in keys:
        r = rec_row(con, key, when)
        if r["status"] not in ("certified", "auto signed off"):
            continue
        signed = [c for s, _, c in accepted_since_reopen(con, key) if s in SIGNED][-1]
        if signed != r["rec_checksum"]:
            append(con, key, "reopened", "rule_engine", when, None, r["rec_checksum"], "accepted",
                   "The rec changed after sign-off")


def advance(con, key, when):
    """Take the rec one step further, if it's ready: auto sign-off, or the next of
    prepare, review and approve."""
    r = rec_row(con, key, when)
    if r["status"] in ("certified", "auto signed off") or not (r["journals_closed"] and r["tie_out_passed"]):
        return
    steps = [s for s, _, _ in accepted_since_reopen(con, key)]
    if not steps and r["auto_signoff_eligible"]:
        sign(con, key, "auto signed off", "rule_engine", when, r["rec_checksum"])
        return
    company, account, _ = key
    if not steps:
        owner = con.execute("SELECT assigned(?, ?, 'owner')", [company, account]).fetchone()[0]
        ai = con.execute("SELECT person_id FROM master.people WHERE linked_person_id = ? AND is_ai_identity",
                         [owner]).fetchone()
        sign(con, key, "prepared", ai[0] if ai else owner, when, r["rec_checksum"])
    elif steps[-1] == "prepared" and explained(con, key, when):
        reviewer = con.execute("SELECT assigned(?, ?, 'reviewer')", [company, account]).fetchone()[0]
        sign(con, key, "reviewed", reviewer, when, r["rec_checksum"])
    elif steps[-1] == "reviewed" and explained(con, key, when):
        approver = con.execute("SELECT assigned(?, ?, 'approver')", [company, account]).fetchone()[0]
        # The approver opens the rec an hour before signing; what they sign is what they saw.
        seen = rec_row(con, key, when - timedelta(hours=1))["rec_checksum"]
        sign(con, key, "approved", approver, when, seen)


# Snapshots ---------------------------------------------------------------------------

def create_snapshot_table(con):
    con.execute("""
        CREATE TABLE IF NOT EXISTS rec.snapshot_lines AS
        SELECT CAST(NULL AS VARCHAR) AS snapshot_id, CAST(NULL AS INTEGER) AS line_number,
               r.*, o.* EXCLUDE (company_code, account_id, period)
        FROM rec.reconciliation AS r
        LEFT JOIN rec.open_items AS o USING (company_code, account_id, period)
        LIMIT 0
    """)


def snapshot_checksum(con, snapshot_id):
    cur = con.execute("SELECT * FROM rec.snapshot_lines WHERE snapshot_id = ? ORDER BY line_number",
                      [snapshot_id])
    text = "\n".join("|".join("" if v is None else str(v) for v in row) for row in cur.fetchall())
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def snapshot(con, key, signoff_id, when):
    """Copy the signed rec and its open items into the snapshot tables as values,
    then write the Excel file from those rows.  The snapshot never pulls anything."""
    n = con.execute("SELECT count(*) + 1 FROM rec.snapshots").fetchone()[0]
    snapshot_id = f"SNP-{n:04d}"
    set_as_at(con, when)
    con.execute("""
        INSERT INTO rec.snapshot_lines
        SELECT ?, row_number() OVER (ORDER BY o.prepaid_item_id), r.*, o.* EXCLUDE (company_code, account_id, period)
        FROM rec.reconciliation AS r
        LEFT JOIN rec.open_items AS o USING (company_code, account_id, period)
        WHERE r.company_code = ? AND r.account_id = ? AND r.period = ?
    """, [snapshot_id, *key])
    rows = con.execute("SELECT count(*) FROM rec.snapshot_lines WHERE snapshot_id = ?", [snapshot_id]).fetchone()[0]
    excel = write_excel(con, key, snapshot_id, signoff_id)
    con.execute("INSERT INTO rec.snapshots VALUES (?, ?, ?, ?, ?, ?, 'snapshot_script', ?, ?, ?)",
                [snapshot_id, *key, signoff_id, when, rows, snapshot_checksum(con, snapshot_id),
                 excel.relative_to(ROOT).as_posix()])


REC_FIELDS = ["company_code", "account_id", "period", "audit_risk", "gl_balance", "open_items_total",
              "difference", "open_item_count", "oldest_item_age_days", "documents_missing", "exception_count",
              "moved_since_last_signoff", "journals_closed", "tie_out_passed", "under_threshold",
              "auto_signoff_eligible", "rec_checksum", "status"]
ITEM_FIELDS = ["prepaid_item_id", "vendor_id", "cost_centre_id", "primary_document_link",
               "supporting_document_count", "service_start", "service_end", "total_amount",
               "amortised_to_date", "remaining", "current_portion", "non_current_portion", "age_days",
               "entered_by"]
AMOUNT_FIELDS = {"gl_balance", "open_items_total", "difference", "total_amount", "amortised_to_date",
                 "remaining", "current_portion", "non_current_portion"}


def amount_format(minor_units):
    """An Excel number format with the currency's decimals.  Excel shows the
    separators in the reader's own settings, so 1,234.50 in Australia reads
    1.234,50 in Germany, from the same file."""
    return "#,##0" if minor_units == 0 else "#,##0." + "0" * minor_units


def write_excel(con, key, snapshot_id, signoff_id):
    """Values only: no formulas, nothing linked back to the ERP or the platform.
    Amounts are stored as numbers, formatted to the company currency's decimals."""
    currency, minor_units = con.execute("""
        SELECT c.currency_code, c.minor_units FROM master.companies AS co
        JOIN master.currencies AS c ON c.currency_code = co.local_currency
        WHERE co.company_code = ?""", [key[0]]).fetchone()
    number_format = amount_format(minor_units)
    cur = con.execute(f"""SELECT {', '.join(REC_FIELDS)} FROM rec.snapshot_lines
                          WHERE snapshot_id = ? LIMIT 1""", [snapshot_id])
    header = dict(zip(REC_FIELDS, cur.fetchone()))
    items = con.execute(f"""SELECT {', '.join(ITEM_FIELDS)} FROM rec.snapshot_lines
                            WHERE snapshot_id = ? AND prepaid_item_id IS NOT NULL ORDER BY line_number""",
                        [snapshot_id]).fetchall()
    log = con.execute("""SELECT signoff_id, step, person_id, signed_at, rec_checksum_at_signing, outcome, note
                         FROM rec.signoff_log
                         WHERE company_code = ? AND account_id = ? AND period = ? AND signoff_id <= ?
                         ORDER BY signoff_id""", [*key, signoff_id]).fetchall()

    wb = Workbook()
    ws = wb.active
    ws.title = "Reconciliation"
    ws.append(["Snapshot", snapshot_id])
    ws.append(["currency", currency])
    for field in REC_FIELDS:
        ws.append([field, float(header[field]) if hasattr(header[field], "as_tuple") else header[field]])
        if field in AMOUNT_FIELDS:
            ws.cell(row=ws.max_row, column=2).number_format = number_format
    sheet = wb.create_sheet("Open items")
    sheet.append(ITEM_FIELDS)
    for item in items:
        sheet.append([float(v) if hasattr(v, "as_tuple") else v for v in item])
        for col, field in enumerate(ITEM_FIELDS, start=1):
            if field in AMOUNT_FIELDS:
                sheet.cell(row=sheet.max_row, column=col).number_format = number_format
    sheet = wb.create_sheet("Sign-off")
    sheet.append(["signoff_id", "step", "person_id", "signed_at_utc", "rec_checksum_at_signing", "outcome", "note"])
    for row in log:
        sheet.append(list(row))

    SNAPSHOTS.mkdir(parents=True, exist_ok=True)
    path = SNAPSHOTS / f"{key[0]}_{key[1]}_{key[2]}_{snapshot_id}.xlsx"
    wb.save(path)
    return path
