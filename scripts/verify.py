"""Verify the demo run.  GitHub Actions runs this after run_demo.py; any failure
fails the build.

Two kinds of check:
- Integrity checks, which must find nothing wrong.
- Planted cases, which must be caught exactly as planted.  A control that
  catches nothing proves nothing, so each planted case shows a control working.

Run from the repo root:  python scripts/verify.py
"""

import hashlib
import shutil
import sys

import duckdb

import gate
import load
import rec_process
from run_demo import END

INTEGRITY = {
    "Every ERP document balances": """
        SELECT company_code, document_number FROM erp.journal_lines
        GROUP BY ALL HAVING sum(amount_local) <> 0""",
    "Every ERP trial balance run sums to zero": """
        SELECT company_code, fiscal_period, report_run_at FROM erp.trial_balance_report
        GROUP BY ALL HAVING sum(closing_balance) <> 0""",
    "Every document link points to a document": """
        SELECT l.link_id FROM platform.document_links AS l
        LEFT JOIN platform.source_documents AS d USING (source_document_id)
        WHERE d.source_document_id IS NULL""",
    "Every journal-line link points to a journal line": """
        SELECT l.link_id FROM platform.document_links AS l
        LEFT JOIN erp.journal_lines AS j USING (company_code, document_number, line_number)
        WHERE l.linked_to = 'journal line' AND j.document_number IS NULL""",
    "No linked document is deleted": """
        SELECT d.source_document_id FROM platform.source_documents AS d
        JOIN platform.document_links AS l USING (source_document_id)
        WHERE d.is_deleted""",
    "Latest tie-out passes for every company and demo period": """
        SELECT p.company_code, p.period FROM master.close_periods AS p
        LEFT JOIN (SELECT company_code, fiscal_period, bool_and(result = 'pass') AS passed
                   FROM (SELECT * FROM control.tie_out_results
                         QUALIFY run_at = max(run_at) OVER (PARTITION BY company_code, fiscal_period))
                   GROUP BY ALL) AS t
          ON t.company_code = p.company_code AND t.fiscal_period = p.period
        WHERE p.period IN ('2026-07', '2026-08', '2026-09') AND NOT coalesce(t.passed, false)""",
    "The extract on the platform came from a certified version": """
        SELECT DISTINCT j.query_id, j.query_version FROM platform.journal_lines AS j
        WHERE NOT EXISTS (
            SELECT 1 FROM control.query_certifications AS c
            WHERE c.query_id = j.query_id AND c.query_version = j.query_version
            GROUP BY c.certified_at HAVING bool_and(c.result = 'pass'))""",
    "Every signed rec has a snapshot": """
        SELECT s.signoff_id FROM rec.signoff_log AS s
        LEFT JOIN rec.snapshots AS n USING (signoff_id)
        WHERE s.step IN ('approved', 'auto signed off') AND s.outcome = 'accepted' AND n.snapshot_id IS NULL""",
    "No rec was approved by its preparer or the person behind the preparer's AI identity": """
        SELECT a.signoff_id FROM rec.signoff_log AS a
        JOIN rec.signoff_log AS p
          ON p.company_code = a.company_code AND p.account_id = a.account_id AND p.period = a.period
         AND p.step = 'prepared' AND p.outcome = 'accepted' AND p.signoff_id < a.signoff_id
        LEFT JOIN master.people AS ai ON ai.person_id = p.person_id
        WHERE a.step = 'approved' AND a.outcome = 'accepted'
          AND a.person_id IN (p.person_id, ai.linked_person_id)""",
    "Every accepted sign-off matched the rec's checksum at the time": """
        SELECT signoff_id FROM rec.signoff_log
        WHERE outcome = 'accepted' AND step IN ('prepared', 'reviewed', 'approved', 'auto signed off')
          AND rec_checksum_seen IS DISTINCT FROM rec_checksum_at_signing""",
}

PLANTED = {
    # name: (query, expected rows)
    "Case 7: the rounding change (v2) failed certification, on JP01 2026-09 only": (
        """SELECT company_code, period FROM control.query_certifications
           WHERE query_version = 'v2' AND result = 'fail'""",
        [("JP01", "2026-09")]),
    "Case 7: every query change is logged and needed recertification": (
        """SELECT version_from, version_to FROM control.change_log
           WHERE object_type = 'query' AND recertification_required ORDER BY changed_at""",
        [(None, "v1"), ("v1", "v2"), ("v2", "v3")]),
    "Case 8: AU01 2026-08 tie-out broke after the late journal, then passed after the next extract": (
        """SELECT bool_and(result = 'pass') FROM control.tie_out_results
           WHERE company_code = 'AU01' AND fiscal_period = '2026-08'
           GROUP BY run_at ORDER BY run_at""",
        [(True,), (False,), (True,)]),
    "Case 9: the only amounts with too many decimals are the planted JPY invoice": (
        """SELECT j.company_code, j.amount_local FROM erp.journal_lines AS j
           JOIN master.currencies AS c ON c.currency_code = j.currency
           WHERE j.amount_local * CAST(10 ** c.minor_units AS INTEGER)
              <> trunc(j.amount_local * CAST(10 ** c.minor_units AS INTEGER))
           ORDER BY 2""",
        [("JP01", -12345.5), ("JP01", 12345.5)]),
    "Case 1: DE01 2026-08 prepaid had no document on 2 September": (
        """SET VARIABLE as_at = TIMESTAMP '2026-09-02 12:00:00';
           SELECT company_code, period, rule_type FROM rec.exceptions
           WHERE company_code = 'DE01' AND period = '2026-08'""",
        [("DE01", "2026-08", "no_source_document")]),
    "Case 1: once the invoice was linked on 3 September, the exception cleared and the rec was approved": (
        """SELECT count(*) FROM rec.exceptions WHERE company_code = 'DE01' AND period = '2026-08'
           UNION ALL
           SELECT count(*) FROM rec.signoff_log WHERE company_code = 'DE01' AND period = '2026-08'
             AND step = 'approved' AND outcome = 'accepted' AND signed_at >= TIMESTAMP '2026-09-03 08:00:00'""",
        [(0,), (1,)]),
    "Case 2: the new INNO cost centre is flagged on AU01 2026-09, and explained by the owner": (
        """SELECT e.posting_type, e.detail, e.escalate_to_person_id FROM (
               SELECT x.*, j.posting_type FROM rec.exceptions AS x
               JOIN platform.journal_lines AS j USING (company_code, document_number, line_number)) AS e
           WHERE e.company_code = 'AU01' AND e.period = '2026-09' ORDER BY 1""",
        [("amortisation run", "Cost centre INNO is not in the fingerprint", "alexm"),
         ("vendor invoice", "Cost centre INNO is not in the fingerprint", "alexm")]),
    "Case 3: the sales controller's journal into DE01 prepaids breaks four rules and goes to the reviewer": (
        """SELECT rule_type, escalate_to_person_id FROM rec.exceptions
           WHERE company_code = 'DE01' AND period = '2026-09' ORDER BY 1""",
        [("new_cost_centre", "nicoh"), ("no_source_document", "nicoh"),
         ("unexpected_posting_type", "nicoh"), ("unexpected_user", "nicoh")]),
    "No other exceptions: across every rec and month, only the planted cases are left flagged": (
        """SELECT company_code, period, count(*) FROM rec.exceptions
           WHERE period IN ('2026-07', '2026-08', '2026-09') GROUP BY ALL ORDER BY ALL""",
        [("AU01", "2026-09", 2), ("DE01", "2026-09", 4)]),
    "Case 4: only JP01 2026-08 auto signed off (low risk, under threshold, no unscheduled movement)": (
        """SELECT company_code, period FROM rec.signoff_log WHERE step = 'auto signed off'""",
        [("JP01", "2026-08")]),
    "Case 5: AU01 2026-08 was approved, reopened by the late journal, and signed off again": (
        """SELECT step, person_id FROM rec.signoff_log
           WHERE company_code = 'AU01' AND period = '2026-08' ORDER BY signoff_id""",
        [("prepared", "alexm_ai"), ("reviewed", "samt"), ("approved", "robinf"), ("reopened", "rule_engine"),
         ("prepared", "alexm_ai"), ("reviewed", "samt"), ("approved", "robinf")]),
    "Case 6: AU01 2026-09 approval on a stale checksum was rejected and the rec reopened": (
        """SELECT step, outcome FROM rec.signoff_log
           WHERE company_code = 'AU01' AND period = '2026-09' ORDER BY signoff_id""",
        [("prepared", "accepted"), ("reviewed", "accepted"), ("rejected", "checksum mismatch"),
         ("reopened", "accepted"), ("prepared", "accepted"), ("reviewed", "accepted"),
         ("approved", "accepted")]),
    "Final state: every rec signed off except DE01 2026-09, held by its exceptions": (
        """SELECT company_code, period, status, difference FROM rec.reconciliation
           WHERE period IN ('2026-07', '2026-08', '2026-09') AND status NOT IN ('certified', 'auto signed off')""",
        [("DE01", "2026-09", "uncertified", 2400.0)]),
}


def document_files_match(con):
    """Every stored document file still matches the hash recorded when it was uploaded."""
    bad = []
    for doc_id, path, expected in con.execute(
            "SELECT source_document_id, store_path, content_hash FROM platform.source_documents").fetchall():
        f = gate.ROOT / path
        if not f.exists() or hashlib.sha256(f.read_bytes()).hexdigest() != expected:
            bad.append(doc_id)
    recorded = {(gate.ROOT / path).resolve() for (path,) in con.execute(
        "SELECT store_path FROM platform.source_documents").fetchall()}
    bad += [f.name for f in (gate.ROOT / "data" / "documents").rglob("*") if f.is_file() and f.resolve() not in recorded]
    return bad


def signoff_chain_intact(con):
    """Recompute every row's hash from its contents and the row before it."""
    previous = rec_process.GENESIS
    for row in con.execute("SELECT * FROM rec.signoff_log ORDER BY signoff_id").fetchall():
        *values, previous_row_hash, row_hash = row
        expected = hashlib.sha256(rec_process.row_payload(*values, previous).encode("utf-8")).hexdigest()
        if previous_row_hash != previous or row_hash != expected:
            return False
        previous = row_hash
    return True


def snapshots_intact(con):
    """Every snapshot's checksum recomputes from its lines, and its Excel file exists."""
    bad = []
    for snapshot_id, checksum, excel in con.execute(
            "SELECT snapshot_id, snapshot_checksum, excel_path FROM rec.snapshots").fetchall():
        if rec_process.snapshot_checksum(con, snapshot_id) != checksum or not (gate.ROOT / excel).exists():
            bad.append(snapshot_id)
    return bad


def gate_catches_unlogged_change():
    """Copy the database, pretend someone edited the query without logging it,
    and confirm the gate refuses to run it."""
    copy = load.DB_PATH.with_name("gate_test.duckdb")
    shutil.copy(load.DB_PATH, copy)
    con = duckdb.connect(str(copy))
    con.execute("UPDATE control.query_register SET code_hash = 'hash of an edit nobody logged'")
    try:
        gate.check_gate(con, "Q001")
        return False
    except gate.GateClosed:
        return True
    finally:
        con.close()
        copy.unlink()


def main():
    con = duckdb.connect(str(load.DB_PATH), read_only=True)
    final = f"SET VARIABLE as_at = TIMESTAMP '{END:%Y-%m-%d %H:%M:%S}'"
    con.execute(final)
    failures = 0

    def report(ok, name, detail=""):
        nonlocal failures
        failures += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {name}{'  ' + detail if detail else ''}")

    print("Integrity checks")
    for name, sql in INTEGRITY.items():
        rows = con.execute(sql).fetchall()
        report(not rows, name, f"{len(rows)} found: {rows[:3]}" if rows else "")
    bad = document_files_match(con)
    report(not bad, "Every stored document matches its recorded hash, and no file is unrecorded", f"{bad[:3]}" if bad else "")
    report(signoff_chain_intact(con), "The sign-off log's hash chain is intact (nothing changed or removed)")
    bad = snapshots_intact(con)
    report(not bad, "Every snapshot matches its checksum and has its Excel file", f"{bad[:3]}" if bad else "")

    print("\nPlanted cases")
    for name, (sql, expected) in PLANTED.items():
        con.execute(final)
        rows = [tuple(float(v) if hasattr(v, "as_tuple") else v for v in r) for r in con.execute(sql).fetchall()]
        report(rows == expected, name, "" if rows == expected else f"expected {expected}, got {rows}")
    con.close()
    report(gate_catches_unlogged_change(), "An unlogged edit to a certified query closes the gate")

    print(f"\n{'All checks passed' if not failures else f'{failures} check(s) failed'}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
