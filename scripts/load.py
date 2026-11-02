"""Build the demo database: create the schema and load the generated data.

The DuckDB file is rebuilt from scratch every time, so it is never the source of
truth: the CSV files in data/ are.

Run from the repo root:  python scripts/load.py
"""

from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "build" / "demo.duckdb"

LOADS = {
    "master.currencies": "data/master/currencies.csv",
    "master.companies": "data/master/companies.csv",
    "master.people": "data/master/people.csv",
    "master.gl_master": "data/master/gl_master.csv",
    "master.gl_master_company": "data/master/gl_master_company.csv",
    "master.cost_centres": "data/master/cost_centres.csv",
    "master.vendors": "data/master/vendors.csv",
    "master.customers": "data/master/customers.csv",
    "master.account_owners": "data/master/account_owners.csv",
    "master.thresholds": "data/master/thresholds.csv",
    "master.parameters": "data/master/parameters.csv",
    "master.misuse_rules": "data/master/misuse_rules.csv",
    "master.close_periods": "data/master/close_periods.csv",
    "erp.journal_lines": "data/erp/journal_lines.csv",
    "erp.trial_balance_report": "data/erp/trial_balance_report.csv",
    "platform.source_documents": "data/document_store/source_documents.csv",
    "platform.document_links": "data/document_store/document_links.csv",
}


def connect():
    DB_PATH.parent.mkdir(exist_ok=True)
    return duckdb.connect(str(DB_PATH))


def main():
    if DB_PATH.exists():
        DB_PATH.unlink()
    con = connect()
    con.execute((ROOT / "sql" / "schema.sql").read_text(encoding="utf-8"))
    for table, csv_path in LOADS.items():
        # Load by column name, all as text, and let the table's own types cast it,
        # so a wrong value fails the load instead of being guessed.
        con.execute(f"""
            INSERT INTO {table} BY NAME
            SELECT * FROM read_csv('{(ROOT / csv_path).as_posix()}', header = true, all_varchar = true)
        """)
        count = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        print(f"{table:32} {count:>7} rows")
    con.close()


if __name__ == "__main__":
    main()
