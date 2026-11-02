# Living reconciliation

[![Certify](https://github.com/charissagibson/living-reconciliation/actions/workflows/certify.yml/badge.svg)](https://github.com/charissagibson/living-reconciliation/actions/workflows/certify.yml)

A balance sheet reconciliation that pulls its own balance, open items and evidence, signs itself off when it's low risk and unchanged, reopens itself when someone posts a late journal, and files a snapshot for the auditors.  No roll-forward, no retyping, no formulas looping back to the ERP.

This is a working demo on synthetic data.  One prepaid expenses reconciliation for three companies, driven by a GL master, with query certification and a trial balance tie-out as the gate in front of it.

## Why I built it

Month end still runs on copying last month's file, opening the ERP, witnessing a balance and typing it in.  Every rec, every month.  I don't want accountants rolling files forward anymore.

My view is the proof belongs upstream.  Certify the query that brings the ERP data onto the platform once, tie out the trial balance every close, and every rec inherits a proven balance.  Then the accountant's time goes on understanding the high-risk accounts, not proving a number the system already proved.

Everything starts from the GL master: what each account is for, who owns it, how risky it is and what misuse looks like.  The rec reads all of that as data.  Thresholds, ownership and rules live in tables, never in code, so a company can change them without a rebuild.

## What happens in the demo

The demo plays three months of close (July to September 2026) for three companies: one in AUD, one in EUR and one in JPY, which has no decimals.  Each case below is planted on purpose.  A control that catches nothing proves nothing.

| # | What's planted | What the system does |
|---|---|---|
| 1 | A prepaid invoice posted with no source document | Flagged as an exception and the rec shows a difference.  Once the invoice is linked, both clear and the rec is approved |
| 2 | A prepaid posted to a cost centre the account has never seen | Flagged against the account's fingerprint.  The owner explains it, and the rec goes through |
| 3 | A sales controller's manual journal into prepaids | Breaks four misuse rules.  Nobody explains it by the day-one deadline, so it goes to the reviewer and the rec stays uncertified |
| 4 | A low-risk rec under its company's threshold, with only scheduled amortisation | Signs itself off.  No person needed |
| 5 | An invoice for August entered on 10 September, after August closed | The signed rec reopens itself, goes back through review and is signed off again |
| 6 | A document linked to the rec while the approver has it open | The approver signed a checksum that's no longer current.  The sign-off is rejected and the rec reopens |
| 7 | A well-meant change to the extract query (rounding amounts) | The query is uncertified the moment it changes, so the platform won't use it.  Recertification fails because the change altered the data |
| 8 | The ERP reruns its trial balance before the platform has the late journal | The tie-out fails and sign-off is blocked until the next extract brings it in |
| 9 | A yen amount with decimals | Caught by the currency check, using each currency's decimals from the currency master |

The full story is in the run log the demo writes (`build/evidence/run_log.csv`).  `scripts/verify.py` then checks every planted case happened exactly as planted, and that nothing else did.  GitHub Actions runs all of it on every push.

## How it works

1. The GL master and its master data family (companies, currencies, owners, thresholds, parameters, misuse rules) sit in standing tables.
2. A certified query extracts the ERP journal lines onto the platform.  It's only used if its file is exactly the version that was certified.  Any change, logged or not, closes the gate until it's recertified.
3. The tie-out compares the platform's trial balance with the ERP's own report, account by account, every close.  A break blocks sign-off for that company and period.
4. The rec is three views over the platform tables: open items, exceptions and the reconciliation itself.  Nothing is stored, so nothing goes stale.  The prepaid schedule is built only from linked evidence.
5. Exceptions are found against each account's fingerprint: the posting types, users, cost centres and vendors that normally go into it, from the last 12 months plus a sample month from the year before.
6. Sign-off is prepare, review, approve.  AI prepares under its own identity (the owner's login with an `_ai` suffix), never under a person's name.  The approver can't be the preparer or the person behind the preparer's AI identity.
7. Every sign-off records the rec checksum the signer saw.  If the rec has changed since, the sign-off is rejected.
8. The sign-off log is append-only, and each row carries the hash of the row before it, so changing or removing any earlier row breaks the chain.
9. After sign-off, a script copies the rec and its open items into snapshot tables as values, and writes the Excel file from those rows.  The Excel is the auditors' view, values only.  The platform is the record.

## What's in here

| Path | What it is |
|---|---|
| `sql/schema.sql` | The 32 tables, in five schemas: `master`, `erp`, `platform`, `rec`, `control` |
| `sql/queries/` | The extract query, every version kept |
| `sql/views/` | The platform trial balance and the three rec views |
| `scripts/generate_data.py` | Makes the synthetic data.  Fixed seed, so it's the same every time |
| `scripts/gate.py` | Query certification, the gate and the tie-out |
| `scripts/rec_process.py` | The prepaid schedule, fingerprints, sign-off, reopen and snapshots |
| `scripts/run_demo.py` | Plays the timeline of events in order |
| `scripts/verify.py` | The integrity checks and the planted cases |
| `data/` | The synthetic master data, ERP postings, document store and comments file |
| `docs/prepaid-expenses/` | The policy, procedure and work instructions the GL master links to |

## Run it

You need Python 3.12.

```bash
python -m venv .venv
source .venv/bin/activate     # on Windows: .venv\Scripts\activate
pip install -r requirements.txt
python scripts/generate_data.py
python scripts/run_demo.py
python scripts/verify.py
```

The database is DuckDB, rebuilt from the CSV files every run.  The snapshots land in `build/snapshots/`.  The SQL is written to move to Databricks later.

## How this was made

I'm a CPA (Australia) and a self-taught finance data engineer.  I'm not a coder.  I designed this, set the standards it's checked against, and directed AI (Claude Code) to write the code.  Then I checked it the way I'd check a rec: against planted cases that should fail, and evidence that they did.

The design is my own method, rebuilt from scratch on made-up data.  No employer's code, data or systems are in here.

## Not in this slice yet

- Flux analysis on a weighted composite score
- Intercompany, posted at source so the matrix nets to zero
- The close master, in-month catches and accruals
- Governed change requests for master data, and access driven by the HR record
- Databricks as a second target

## The data

Everything here is synthetic.  The companies, people, vendors, customers, documents and amounts are made up.
