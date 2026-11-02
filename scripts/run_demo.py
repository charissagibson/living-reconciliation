"""Run the demo: rebuild the database, then play the timeline of events in order.

Each event happens at a point in time (UTC): a query is created, changed or
certified, the nightly extract runs, a company closes its journals, the ERP
reruns its trial balance after a late journal, and each working morning the
rec process takes every open rec one step further.  Before every event the
platform refreshes what the rec reads; after it, any signed rec that changed is
reopened.  A run log is written to build/evidence/run_log.csv.

Run from the repo root:  python scripts/run_demo.py
"""

import csv
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import gate
import load
import rec_process

QUERY = "Q001"
DEMO_PERIODS = ("2026-07", "2026-08", "2026-09")
END = datetime(2026, 10, 9, 23)

log = []


def record(when, event, outcome):
    log.append((when, event, outcome))


def timeline(con):
    """Every event as (time, order, description, action)."""
    events = []

    def at(ts, description, action, order=1):
        events.append((ts, order, description, action))

    # The query is created and certified before the demo months start.
    at(datetime(2026, 6, 15, 9), "Q001 v1 created by kimw",
       lambda t: gate.register_query(con, QUERY, "extract_journal_lines",
                                     "Extract ERP journal lines to the platform", "v1", "kimw", t,
                                     "Platform needs the ERP journal lines"))
    at(datetime(2026, 6, 20, 9), "Q001 certified by robinf", lambda t: certify(t, "robinf"))

    # Planted case 7: a change to the query, which makes it uncertified until recertified.
    at(datetime(2026, 9, 15, 8), "Q001 changed to v2 by kimw",
       lambda t: gate.change_query(con, QUERY, "v2", "kimw", t,
                                   "Round amounts to the currency's decimals",
                                   "Tidy amounts on the way in"))
    at(datetime(2026, 9, 16, 9), "Q001 v2 recertification by robinf", lambda t: certify(t, "robinf"))
    at(datetime(2026, 9, 16, 14), "Q001 changed to v3 by kimw",
       lambda t: gate.change_query(con, QUERY, "v3", "kimw", t,
                                   "Back to the unchanged extract",
                                   "Certification failed: rounding changed the data.  Wrong decimals are "
                                   "fixed in the ERP, not hidden by the query"))
    at(datetime(2026, 9, 17, 9), "Q001 v3 recertification by robinf", lambda t: certify(t, "robinf"))

    # Each company's close: the extract runs and the tie-out checks it, as soon as
    # the journals are closed and the ERP has run its trial balance report.  The
    # fingerprint for the period is built from the months before it.
    for company, period, closed_at in con.execute("""
            SELECT company_code, period, journals_closed_at FROM master.close_periods
            WHERE period IN ? ORDER BY ALL""", [list(DEMO_PERIODS)]).fetchall():
        at(closed_at, f"Close {company} {period}: extract and tie-out",
           lambda t, c=company, p=period: close(t, c, p), order=2)

    # The ERP reruns its report after a late journal.  The tie-out runs against
    # whatever the platform holds at that moment.
    for company, period, run_at in con.execute("""
            SELECT DISTINCT r.company_code, r.fiscal_period, r.report_run_at
            FROM erp.trial_balance_report AS r
            JOIN master.close_periods AS p
              ON p.company_code = r.company_code AND p.period = r.fiscal_period
            WHERE r.fiscal_period IN ? AND r.report_run_at > p.journals_closed_at
            ORDER BY ALL""", [list(DEMO_PERIODS)]).fetchall():
        at(run_at + timedelta(minutes=30), f"ERP rerun {company} {period}: tie-out",
           lambda t, c=company, p=period: tie(t, c, p), order=3)

    # The nightly extract, which also retries any tie-out that failed.
    day = datetime(2026, 6, 20, 20)
    while day <= END:
        at(day, "Nightly extract", nightly, order=4)
        day += timedelta(days=1)

    # Each working morning at 10:00 local, the rec process takes every open rec
    # for the company one step further.
    for company, tz in con.execute("SELECT company_code, cutoff_timezone FROM master.companies").fetchall():
        d = date(2026, 8, 1)
        while d <= END.date():
            if d.weekday() < 5:
                local = datetime.combine(d, time(10), tzinfo=ZoneInfo(tz))
                utc = local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
                at(utc, f"Rec process {company}", lambda t, c=company: rec_morning(t, c), order=5)
            d += timedelta(days=1)

    return sorted(events, key=lambda e: (e[0], e[1]))


def certify(when, who):
    passed = gate.certify(CON, QUERY, who, when)
    version = CON.execute("SELECT current_version FROM control.query_register WHERE query_id = ?",
                          [QUERY]).fetchone()[0]
    record(when, f"Certify {QUERY} {version}", "certified" if passed else "certification failed")


def try_extract(when, event):
    try:
        gate.extract(CON, QUERY, when)
        return True
    except gate.GateClosed as refusal:
        record(when, event, f"extract refused: {refusal}; platform keeps its last certified extract")
        return False


def tie(when, company, period):
    passed = gate.tie_out(CON, company, period, when)
    record(when, f"Tie-out {company} {period}", "pass" if passed else "fail: sign-off blocked")


def close(when, company, period):
    if try_extract(when, f"Close {company} {period}"):
        tie(when, company, period)
    rec_process.build_fingerprints(CON, company, period, when)


def nightly(when):
    if not try_extract(when, "Nightly extract"):
        return
    failing = CON.execute("""
        SELECT DISTINCT company_code, fiscal_period FROM (
            SELECT * FROM control.tie_out_results
            QUALIFY run_at = max(run_at) OVER (PARTITION BY company_code, fiscal_period)
        ) WHERE result = 'fail'
    """).fetchall()
    for company, period in failing:
        tie(when, company, period)


def rec_morning(when, company):
    for key in [k for k in rec_keys() if k[0] == company]:
        rec_process.advance(CON, key, when)


def rec_keys():
    return CON.execute("""SELECT company_code, account_id, period FROM rec.reconciliation
                          WHERE period IN ? ORDER BY ALL""", [list(DEMO_PERIODS)]).fetchall()


def record_signoffs(since_id):
    for when, company, account, period, step, person, outcome, note in CON.execute("""
            SELECT signed_at, company_code, account_id, period, step, person_id, outcome, note
            FROM rec.signoff_log WHERE signoff_id > ? ORDER BY signoff_id""", [since_id]).fetchall():
        record(when, f"Rec {company} {account} {period}: {step} by {person}",
               outcome if not note else f"{outcome}: {note}")


def main():
    global CON
    load.main()
    CON = load.connect()
    for view in ("platform_trial_balance.sql", "rec.sql"):
        CON.execute((gate.ROOT / "sql" / "views" / view).read_text(encoding="utf-8"))
    rec_process.set_as_at(CON, datetime(2026, 6, 1))
    rec_process.create_snapshot_table(CON)

    for when, _order, _description, action in timeline(CON):
        last_id = CON.execute("SELECT coalesce(max(signoff_id), 0) FROM rec.signoff_log").fetchone()[0]
        rec_process.refresh(CON, when)
        action(when)
        rec_process.refresh(CON, when)
        rec_process.reopen_check(CON, rec_keys(), when)
        record_signoffs(last_id)
    CON.close()

    out = gate.EVIDENCE / "run_log.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["at_utc", "event", "outcome"])
        w.writerows((f"{t:%Y-%m-%d %H:%M}", e, o) for t, e, o in log)
    print()
    for t, e, o in log:
        if o != "pass":
            print(f"{t:%Y-%m-%d %H:%M}  {e:48} {o}")
    print(f"\n{len(log)} events logged.  Run log: {out.relative_to(gate.ROOT).as_posix()}")


if __name__ == "__main__":
    main()
