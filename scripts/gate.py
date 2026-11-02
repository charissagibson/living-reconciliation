"""GL integrity: the gate in front of every reconciliation.

- Query certification: run the query, compare its output with the ERP's own
  journal line listing, and record the result and evidence.
- The gate: a query is used only if its file on disk is the version that was
  certified.  Any change, logged or not, makes it uncertified.
- The tie-out: the platform's trial balance against the ERP's trial balance
  report, for every account, every close.
"""

import csv
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
QUERY_DIR = ROOT / "sql" / "queries"
EVIDENCE = ROOT / "build" / "evidence"


class GateClosed(Exception):
    """Raised when an uncertified query would be used."""


def query_path(query_id, version):
    return next(QUERY_DIR.glob(f"{query_id}_*")) / f"{version}.sql"


def code_hash(query_id, version):
    return hashlib.sha256(query_path(query_id, version).read_bytes()).hexdigest()


def run_query(con, query_id, version, as_at):
    """Run a query version as at a point in time, into a temporary table."""
    sql = query_path(query_id, version).read_text(encoding="utf-8")
    con.execute(f"CREATE OR REPLACE TEMP TABLE query_output AS {sql}", {"as_at": as_at})


# Register and change log --------------------------------------------------------

def register_query(con, query_id, name, purpose, version, owner, when, why):
    con.execute("""
        INSERT INTO control.query_register
        VALUES (?, ?, ?, ?, ?, ?, 'uncertified', NULL, NULL, NULL)
    """, [query_id, name, purpose, version, code_hash(query_id, version), owner])
    log_change(con, "query", query_id, None, version, f"Query created: {name}", owner, when, why)


def change_query(con, query_id, new_version, who, when, what, why):
    """A logged change.  The query is uncertified until it's recertified."""
    old = con.execute("SELECT current_version FROM control.query_register WHERE query_id = ?",
                      [query_id]).fetchone()[0]
    con.execute("""
        UPDATE control.query_register
        SET current_version = ?, code_hash = ?, status = 'uncertified'
        WHERE query_id = ?
    """, [new_version, code_hash(query_id, new_version), query_id])
    log_change(con, "query", query_id, old, new_version, what, who, when, why)


def log_change(con, object_type, object_id, version_from, version_to, what, who, when, why):
    n = con.execute("SELECT count(*) + 1 FROM control.change_log").fetchone()[0]
    con.execute("INSERT INTO control.change_log VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [f"CHG-{n:04d}", object_type, object_id, version_from, version_to, what, who, when, why,
                 object_type == "query"])


# Certification -----------------------------------------------------------------

def certify(con, query_id, certified_by, when):
    """Compare the current version's output with the ERP journal line listing,
    for every company and period, as at the certification time.  Certified only
    if every company and period matches on row count and total."""
    version, registered_hash = con.execute(
        "SELECT current_version, code_hash FROM control.query_register WHERE query_id = ?",
        [query_id]).fetchone()
    file_hash = code_hash(query_id, version)
    run_query(con, query_id, version, when)
    rows = con.execute("""
        WITH erp AS (
            SELECT company_code, fiscal_period, count(*) AS n, sum(abs(amount_local)) AS total
            FROM erp.journal_lines WHERE entry_timestamp <= $as_at GROUP BY ALL
        ), q AS (
            SELECT company_code, fiscal_period, count(*) AS n, sum(abs(amount_local)) AS total
            FROM query_output GROUP BY ALL
        )
        SELECT coalesce(erp.company_code, q.company_code), coalesce(erp.fiscal_period, q.fiscal_period),
               coalesce(erp.n, 0), coalesce(q.n, 0), coalesce(erp.total, 0), coalesce(q.total, 0)
        FROM erp FULL JOIN q USING (company_code, fiscal_period)
        ORDER BY 1, 2
    """, {"as_at": when}).fetchall()

    first = con.execute("SELECT count(*) + 1 FROM control.query_certifications").fetchone()[0]
    evidence = EVIDENCE / "certifications" / f"{query_id}_{version}_{when:%Y%m%dT%H%M}.csv"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    all_pass = file_hash == registered_hash
    with evidence.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["company_code", "period", "erp_row_count", "query_row_count", "erp_total",
                    "query_total", "difference", "result"])
        for n, (company, period, erp_n, q_n, erp_total, q_total) in enumerate(rows):
            result = "pass" if (erp_n == q_n and erp_total == q_total and all_pass) else "fail"
            all_pass = all_pass and result == "pass"
            w.writerow([company, period, erp_n, q_n, erp_total, q_total, q_total - erp_total, result])
            con.execute("INSERT INTO control.query_certifications VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        [f"CERT-{first + n:05d}", query_id, version, file_hash, "ERP journal line listing",
                         company, period, erp_n, q_n, erp_total, q_total, q_total - erp_total, result,
                         certified_by, when, evidence.relative_to(ROOT).as_posix()])
    if all_pass:
        con.execute("""
            UPDATE control.query_register
            SET status = 'certified', last_certified_at = ?, last_certified_by = ?,
                annual_review_due = CAST(? AS DATE) + INTERVAL 1 YEAR
            WHERE query_id = ?
        """, [when, certified_by, when, query_id])
    return all_pass


# The gate and the extract ----------------------------------------------------------

def check_gate(con, query_id):
    """The query may be used only if it's certified and its file is exactly the
    version that was certified.  Returns the version, or raises GateClosed."""
    version, status, registered_hash = con.execute(
        "SELECT current_version, status, code_hash FROM control.query_register WHERE query_id = ?",
        [query_id]).fetchone()
    if status != "certified":
        raise GateClosed(f"{query_id} {version} is uncertified")
    if code_hash(query_id, version) != registered_hash:
        raise GateClosed(f"{query_id} {version} has changed since it was certified")
    return version


def extract(con, query_id, when):
    """Refresh platform.journal_lines with the certified query.  If the gate is
    closed, the platform keeps its last certified extract."""
    version = check_gate(con, query_id)
    run_query(con, query_id, version, when)
    con.execute("DELETE FROM platform.journal_lines")
    con.execute("""
        INSERT INTO platform.journal_lines
        SELECT *, ? AS extracted_at, ? AS query_id, ? AS query_version FROM query_output
    """, [when, query_id, version])


# The tie-out ----------------------------------------------------------------------

def tie_out(con, company, period, when):
    """Platform trial balance against the ERP's latest trial balance report run
    for the company and period, account by account.  Returns True if it passed."""
    n = con.execute("SELECT count(DISTINCT run_id) + 1 FROM control.tie_out_results").fetchone()[0]
    run_id = f"TIE-{n:05d}"
    con.execute("""
        INSERT INTO control.tie_out_results
        WITH erp AS (
            SELECT account_id, closing_balance
            FROM erp.trial_balance_report
            WHERE company_code = $company AND fiscal_period = $period AND report_run_at <= $when
            QUALIFY report_run_at = max(report_run_at) OVER ()
        ), plat AS (
            SELECT account_id, closing_balance
            FROM platform.trial_balance
            WHERE company_code = $company AND fiscal_period = $period
        )
        SELECT $run_id, $when, $company, $period, account_id,
               coalesce(erp.closing_balance, 0), coalesce(plat.closing_balance, 0),
               coalesce(plat.closing_balance, 0) - coalesce(erp.closing_balance, 0),
               CASE WHEN coalesce(plat.closing_balance, 0) = coalesce(erp.closing_balance, 0)
                    THEN 'pass' ELSE 'fail' END
        FROM erp FULL JOIN plat USING (account_id)
    """, {"run_id": run_id, "when": when, "company": company, "period": period})
    failed = con.execute("SELECT count(*) FROM control.tie_out_results WHERE run_id = ? AND result = 'fail'",
                         [run_id]).fetchone()[0]
    return failed == 0
