"""Generate the synthetic data for the living reconciliation demo.

Everything here is made up: three demo companies, their master data, sixteen
months of ERP postings, the ERP's own trial balance report, and the source
documents in the document store.  The output is deterministic (fixed seed), so
running it again gives the same files.

Run from the repo root:  python scripts/generate_data.py
"""

import csv
import hashlib
import random
import shutil
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
SEED = 20261101

FIRST_PERIOD = (2025, 6)
LAST_PERIOD = (2026, 9)
REVIEW_DATE = date(2026, 3, 31)
REVIEWER = "robinf"

# Master data ------------------------------------------------------------------

CURRENCIES = [
    # code, name, minor units, source, active
    ("AUD", "Australian dollar", 2, "ISO 4217", True),
    ("EUR", "Euro", 2, "ISO 4217", True),
    ("JPY", "Japanese yen", 0, "ISO 4217", True),
    ("USD", "US dollar", 2, "ISO 4217", True),
    ("KWD", "Kuwaiti dinar", 3, "ISO 4217", False),
]

COMPANIES = [
    # code, name, country, currency, FY start month, time zone, scale for amounts
    ("AU01", "Demo Australia Pty Ltd", "AU", "AUD", 7, "Australia/Sydney", Decimal("1")),
    ("DE01", "Demo Deutschland GmbH", "DE", "EUR", 1, "Europe/Berlin", Decimal("0.6")),
    ("JP01", "Demo Japan KK", "JP", "JPY", 4, "Asia/Tokyo", Decimal("100")),
]

PEOPLE = [
    # login, name, department, level, is AI identity, linked person
    ("robinf", "Robin Fischer", "Group Accounting", "Head of General Ledger", False, None),
    ("jordanl", "Jordan Lee", "Accounting AU", "Senior Accountant", False, None),
    ("alexm", "Alex Morgan", "Accounts Payable AU", "Accountant", False, None),
    ("samt", "Sam Taylor", "Finance AU", "Financial Controller", False, None),
    ("kimw", "Kim Weber", "Accounting DE", "Senior Accountant", False, None),
    ("chrisb", "Chris Bauer", "Accounts Payable DE", "Accountant", False, None),
    ("nicoh", "Nico Hartmann", "Finance DE", "Financial Controller", False, None),
    ("taylorb", "Taylor Brooks", "Sales DE", "Sales Controller", False, None),
    ("yukit", "Yuki Tanaka", "Accounting JP", "Senior Accountant", False, None),
    ("hanas", "Hana Sato", "Accounts Payable JP", "Accountant", False, None),
    ("renk", "Ren Kato", "Finance JP", "Financial Controller", False, None),
    ("alexm_ai", "Alex Morgan (AI)", "Accounts Payable AU", "AI identity", True, "alexm"),
    ("kimw_ai", "Kim Weber (AI)", "Accounting DE", "AI identity", True, "kimw"),
    ("yukit_ai", "Yuki Tanaka (AI)", "Accounting JP", "AI identity", True, "yukit"),
    ("rule_engine", "Sign-off rule engine", "System", "System identity", False, None),
    ("snapshot_script", "Snapshot script", "System", "System identity", False, None),
]

# Who does what in each company: GL accountant, AP accountant, controller,
# and the AI identity that runs the amortisation.
ROLES = {
    "AU01": dict(gl="jordanl", ap="alexm", controller="samt", ai="alexm_ai"),
    "DE01": dict(gl="kimw", ap="chrisb", controller="nicoh", ai="kimw_ai"),
    "JP01": dict(gl="yukit", ap="hanas", controller="renk", ai="yukit_ai"),
}

GL_ACCOUNTS = [
    # id, short name, long name, intended use, class, statement, contra, intercompany,
    # audit risk, rec type, rec frequency, prohibited use, misuse summary
    ("1000", "Bank", "Cash at bank - operating account",
     "Receipts and payments through the operating bank account", "asset", "balance sheet", False, False,
     "high", "bank", "monthly", "Petty cash, card clearing", "Only postings that appear on the bank statement"),
    ("1100", "Trade receivables", "Trade receivables - third parties",
     "Amounts invoiced to customers and not yet received", "asset", "balance sheet", False, False,
     "medium", "open_items", "monthly", "Intercompany receivables", "Customer invoices and receipts only"),
    ("1300", "Prepaid expenses", "Prepaid expenses - current and non-current",
     "Payments in advance for services spread over a future service period", "asset", "balance sheet", False, False,
     "low", "prepaid_schedule", "monthly", "Deposits, capital expenditure, anything with no service period",
     "Vendor invoices with a service period and the amortisation run only; every invoice needs its document"),
    ("1500", "Equipment", "Property, plant and equipment - equipment at cost",
     "Cost of equipment above the capitalisation limit", "asset", "balance sheet", False, False,
     "medium", "fixed_asset_register", "quarterly", "Repairs and maintenance", "Asset acquisitions only"),
    ("1510", "Accumulated depreciation", "Accumulated depreciation - equipment",
     "Depreciation charged on equipment", "asset", "balance sheet", True, False,
     "medium", "fixed_asset_register", "quarterly", "Anything other than depreciation and disposals",
     "Depreciation run only"),
    ("1900", "Intercompany receivable", "Intercompany receivable - group companies",
     "Amounts owed by other group companies", "asset", "balance sheet", False, True,
     "high", "intercompany", "monthly", "Third-party balances", "Group companies only; must net to zero across the group"),
    ("2000", "Trade payables", "Trade payables - third parties",
     "Vendor invoices received and not yet paid", "liability", "balance sheet", False, False,
     "high", "open_items", "monthly", "Accruals, intercompany payables", "Vendor invoices and payments only"),
    ("2100", "Accrued expenses", "Accrued expenses",
     "Estimated expenses for invoices not yet received, reversed on day one", "liability", "balance sheet", False, False,
     "medium", "accrual_listing", "monthly", "Provisions, payables with an invoice", "Month-end accruals and their day-one reversals only"),
    ("3000", "Share capital", "Issued share capital",
     "Paid-up share capital", "equity", "balance sheet", False, False,
     "low", "balance_review", "annual", "Anything other than share issues", "Share issues only"),
    ("3100", "Retained earnings", "Retained earnings",
     "Accumulated profits brought forward", "equity", "balance sheet", False, False,
     "low", "balance_review", "annual", "Direct postings during the year", "Year-end close only"),
    ("4000", "Revenue", "Revenue from services",
     "Revenue from services delivered to customers", "revenue", "P&L", False, False,
     "medium", "none", "none", "Other income, intercompany recharges", "Customer billing only"),
    ("6000", "Rent", "Rent and occupancy",
     "Office rent", "expense", "P&L", False, False,
     "low", "none", "none", "Rental deposits", "Rent invoices and prepaid rent amortisation"),
    ("6100", "Insurance", "Insurance premiums",
     "Business insurance premiums", "expense", "P&L", False, False,
     "low", "none", "none", "Claims received", "Prepaid insurance amortisation"),
    ("6200", "Software", "Software subscriptions",
     "Software as a service and licence subscriptions", "expense", "P&L", False, False,
     "low", "none", "none", "Capitalised software development", "Prepaid subscription amortisation"),
    ("6300", "Salaries", "Salaries and wages",
     "Employee pay", "expense", "P&L", False, False,
     "medium", "none", "none", "Contractor invoices", "Payroll journals only"),
    ("6400", "Depreciation", "Depreciation expense",
     "Depreciation charge for the period", "expense", "P&L", False, False,
     "low", "none", "none", "Impairment", "Depreciation run only"),
    ("6500", "Office costs", "Office and administration costs",
     "Day-to-day office running costs and utilities", "expense", "P&L", False, False,
     "low", "none", "none", "Rent, insurance, software", "Office supplies and utilities invoices, utilities accruals"),
    ("6600", "Bank charges", "Bank fees and charges",
     "Fees charged by the bank", "expense", "P&L", False, False,
     "low", "none", "none", "Interest", "From the bank statement only"),
    ("6700", "Maintenance", "Repairs and maintenance",
     "Maintenance contracts and repairs", "expense", "P&L", False, False,
     "low", "none", "none", "Equipment purchases", "Maintenance invoices and prepaid maintenance amortisation"),
]

COST_CENTRES = [("ADM", "Administration"), ("IT", "Information technology"),
                ("OPS", "Operations"), ("SAL", "Sales")]

VENDOR_KINDS = [
    # suffix, name, payment terms days, payment method
    ("INS", "Insurance Vendor", 30, "bank transfer"),
    ("SWA", "Software Vendor A", 14, "card"),
    ("SWB", "Software Vendor B", 30, "bank transfer"),
    ("SWC", "Software Vendor C", 30, "card"),
    ("LND", "Landlord", 7, "direct debit"),
    ("MNT", "Maintenance Vendor", 30, "bank transfer"),
    ("OFF", "Office Supplies Vendor", 30, "bank transfer"),
    ("UTL", "Utilities Vendor", 14, "direct debit"),
]

# Prepaid items for every company: kind, vendor suffix, expense account, cost
# centre, service start, months, base amount (scaled per company).
PREPAID_PLAN = [
    ("Insurance premium", "INS", "6100", "ADM", date(2025, 7, 1), 12, 24000),
    ("Insurance premium", "INS", "6100", "ADM", date(2026, 7, 1), 12, 26400),
    ("Software subscription", "SWA", "6200", "IT", date(2025, 9, 1), 12, 18000),
    ("Software subscription", "SWA", "6200", "IT", date(2026, 9, 1), 12, 19800),
    ("Software subscription", "SWB", "6200", "IT", date(2026, 1, 1), 12, 9600),
    ("Software subscription", "SWC", "6200", "IT", date(2025, 6, 1), 12, 6000),
    ("Software subscription", "SWC", "6200", "IT", date(2026, 6, 1), 12, 6600),
    ("Rent in advance", "LND", "6000", "ADM", date(2025, 7, 1), 3, 15000),
    ("Rent in advance", "LND", "6000", "ADM", date(2025, 10, 1), 3, 15000),
    ("Rent in advance", "LND", "6000", "ADM", date(2026, 1, 1), 3, 15000),
    ("Rent in advance", "LND", "6000", "ADM", date(2026, 4, 1), 3, 15000),
    ("Rent in advance", "LND", "6000", "ADM", date(2026, 7, 1), 3, 15000),
    ("Maintenance contract", "MNT", "6700", "OPS", date(2025, 10, 1), 24, 36000),
]


# Helpers ----------------------------------------------------------------------

def periods():
    y, m = FIRST_PERIOD
    while (y, m) <= LAST_PERIOD:
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def period_key(y, m):
    return f"{y:04d}-{m:02d}"


def add_months(d, n):
    m = d.month - 1 + n
    return date(d.year + m // 12, m % 12 + 1, 1)


def month_end(y, m):
    return add_months(date(y, m, 1), 1) - timedelta(days=1)


def day_one(y, m):
    """The first working day after month end."""
    d = month_end(y, m) + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def to_utc(d, t, tz):
    local = datetime.combine(d, t, tzinfo=ZoneInfo(tz))
    return local.astimezone(timezone.utc).replace(tzinfo=None)


def fmt_ts(ts):
    return ts.strftime("%Y-%m-%d %H:%M:%S")


MINOR_UNITS = {c[0]: c[2] for c in CURRENCIES}


def money(value, currency):
    step = Decimal(1).scaleb(-MINOR_UNITS[currency])
    return Decimal(value).quantize(step, rounding=ROUND_HALF_UP)


def write_csv(path, header, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(header)
        for row in rows:
            w.writerow(["" if v is None else v for v in row])


# The ledger ---------------------------------------------------------------------

class Ledger:
    def __init__(self):
        self.lines = []
        self.seq = {}

    def post(self, company, currency, posting_date, entry_ts, posting_type, entered_by,
             lines, source_document_id=None, reverses=None, period=None):
        """lines: (account, amount, cost centre, vendor, text).  Must sum to zero."""
        assert sum(a for _, a, _, _, _ in lines) == 0, (company, posting_type, lines)
        self.seq[company] = self.seq.get(company, 0) + 1
        doc = f"{company}-{self.seq[company]:06d}"
        fiscal_period = period or posting_date.strftime("%Y-%m")
        for n, (account, amount, cc, vendor, text) in enumerate(lines, start=1):
            self.lines.append(dict(
                company_code=company, document_number=doc, line_number=n,
                posting_date=posting_date, entry_timestamp=entry_ts, fiscal_period=fiscal_period,
                account_id=account, cost_centre_id=cc, vendor_id=vendor, posting_type=posting_type,
                amount_local=amount, currency=currency, line_text=text, entered_by=entered_by,
                source_document_id=source_document_id, reverses_document=reverses))
        return doc


class DocumentStore:
    def __init__(self):
        self.docs = []
        self.links = []

    def add(self, company, doc_type, vendor, doc_date, amount, currency, uploaded_at, uploaded_by,
            body, service_start=None, service_end=None):
        doc_id = f"DOC-{company}-{len(self.docs) + 1:05d}"
        rel = Path("data") / "documents" / company / f"{doc_id}.txt"
        text = (f"SYNTHETIC {doc_type.upper()} - demo data only\n"
                f"Document: {doc_id}\nCompany: {company}\nVendor: {vendor}\n"
                f"Date: {doc_date}\n{body}\n")
        if amount is not None:
            text += f"Amount: {amount} {currency}\n"
        if service_start:
            text += f"Service period: {service_start} to {service_end}\n"
        path = ROOT / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        self.docs.append(dict(
            source_document_id=doc_id, version=1, document_type=doc_type, company_code=company,
            vendor_id=vendor, document_date=doc_date, service_start=service_start,
            service_end=service_end, amount=amount, currency=currency,
            store_path=rel.as_posix(), content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            uploaded_at=uploaded_at, uploaded_by=uploaded_by, superseded_by_version=None,
            is_deleted=False))
        return doc_id

    def link(self, doc_id, linked_to, company, linked_at, linked_by, link_type="primary",
             account=None, period=None, document_number=None, line_number=None, prepaid_item=None):
        self.links.append(dict(
            link_id=f"LNK-{len(self.links) + 1:06d}", source_document_id=doc_id, linked_to=linked_to,
            company_code=company, account_id=account, period=period, document_number=document_number,
            line_number=line_number, prepaid_item_id=prepaid_item, link_type=link_type,
            linked_at=linked_at, linked_by=linked_by))


# Generation -----------------------------------------------------------------------

def schedule_amount_to_date(item, period_end):
    """Straight line, monthly: what should be amortised by the end of a period.
    The last month takes the rounding difference, so the item fully amortises."""
    if period_end < item["service_start"]:
        return Decimal(0)
    months_done = min(item["months"], (period_end.year - item["service_start"].year) * 12
                      + period_end.month - item["service_start"].month + 1)
    if months_done == item["months"]:
        return item["total"]
    monthly = money(item["total"] / item["months"], item["currency"])
    return monthly * months_done


def generate():
    rng = random.Random(SEED)
    ledger = Ledger()
    store = DocumentStore()
    prepaid_items = []

    for code, _name, country, ccy, _fy, tz, scale in COMPANIES:
        roles = ROLES[code]

        def amt(base, jitter=0.1):
            return money(Decimal(base) * scale * Decimal(str(1 + rng.uniform(-jitter, jitter))), ccy)

        def at(d, hour, minute=0):
            return to_utc(d, time(hour, minute), tz)

        def vendor_id(suffix):
            return f"V-{country}-{suffix}"

        def vendor_invoice(item_or_none, vendor, posting_date, entry_ts, account, cc, amount, body,
                           service=None, with_document=True, period=None, document_at=None):
            """A vendor invoice: debit the cost or prepaid account, credit trade payables.
            document_at: the invoice was posted without its document, which is
            uploaded and linked on the platform later, at this time (UTC)."""
            doc_id = None
            if with_document:
                doc_id = store.add(code, "invoice", vendor, posting_date, amount, ccy,
                                   entry_ts - timedelta(hours=1), roles["ap"], body,
                                   *(service or (None, None)))
            text = body if not service else f"{body} - service {service[0]} to {service[1]}"
            doc = ledger.post(code, ccy, posting_date, entry_ts, "vendor invoice", roles["ap"], [
                (account, amount, cc, vendor, text),
                ("2000", -amount, None, vendor, text),
            ], source_document_id=doc_id, period=period)
            evidence, linked_at = doc_id, entry_ts
            if document_at:
                evidence = store.add(code, "invoice", vendor, posting_date, amount, ccy,
                                     document_at - timedelta(minutes=10), roles["ap"], body,
                                     *(service or (None, None)))
                linked_at = document_at
            if evidence:
                for line in (1, 2):
                    store.link(evidence, "journal line", code, linked_at, roles["ap"],
                               account=account if line == 1 else "2000",
                               document_number=doc, line_number=line)
                if item_or_none:
                    store.link(evidence, "prepaid item", code, linked_at, roles["ap"],
                               account="1300", prepaid_item=item_or_none["id"])
            # Pay it on its terms, if that falls inside the data.
            terms = next(k[2] for k in VENDOR_KINDS if vendor.endswith(k[0]))
            pay_date = posting_date + timedelta(days=terms)
            if pay_date <= month_end(*LAST_PERIOD):
                ledger.post(code, ccy, pay_date, at(pay_date, 11), "payment", roles["ap"], [
                    ("2000", amount, None, vendor, f"Payment {doc}"),
                    ("1000", -amount, None, vendor, f"Payment {doc}"),
                ])
            return doc

        def new_prepaid(kind, suffix, expense, cc, start, months, amount, posting_date=None,
                        entry_ts=None, with_document=True, period=None, document_at=None):
            item = dict(id=f"PP-{code}-{len([p for p in prepaid_items if p['company'] == code]) + 1:03d}",
                        company=code, currency=ccy, vendor=vendor_id(suffix), expense=expense, cc=cc,
                        service_start=start, months=months, total=amount, tz=tz,
                        service_end=add_months(start, months) - timedelta(days=1))
            posting_date = posting_date or start + timedelta(days=rng.randint(0, 4))
            entry_ts = entry_ts or at(posting_date, rng.randint(9, 16), rng.choice([0, 15, 30, 45]))
            item["invoice_doc"] = vendor_invoice(
                item, item["vendor"], posting_date, entry_ts, "1300", cc, amount,
                f"{kind} {item['id']}", service=(start, item["service_end"]),
                with_document=with_document, period=period, document_at=document_at)
            prepaid_items.append(item)
            return item

        # Opening balances.
        first = date(*FIRST_PERIOD, 1)
        ledger.post(code, ccy, first, at(first, 9), "opening balance", roles["gl"], [
            ("1000", money(600000 * scale, ccy), None, None, "Opening balance"),
            ("1500", money(240000 * scale, ccy), None, None, "Opening balance"),
            ("1510", money(-40000 * scale, ccy), None, None, "Opening balance"),
            ("3000", money(-500000 * scale, ccy), None, None, "Opening balance"),
            ("3100", money(-300000 * scale, ccy), None, None, "Opening balance"),
        ])

        # The planned prepaid items.
        for kind, suffix, expense, cc, start, months, base in PREPAID_PLAN:
            new_prepaid(kind, suffix, expense, cc, start, months, amt(base, 0.05))

        # Planted cases.  Each one is there to prove a control works.
        if code == "AU01":
            # Planted case 2: a prepaid posted to a cost centre the account has never seen.
            inno = new_prepaid("Software subscription", "SWB", "6200", "INNO", date(2026, 9, 1), 12,
                               amt(7200, 0))
            # Planted case 6: a supporting document is linked to the September rec's open
            # item while the approver has it open, so the checksum the approver saw is stale.
            order_at = datetime(2026, 10, 5, 22, 30)
            order = store.add(code, "contract", inno["vendor"], date(2026, 8, 20), inno["total"], ccy,
                              order_at - timedelta(minutes=10), roles["ap"],
                              "Subscription order form for the innovation lab",
                              inno["service_start"], inno["service_end"])
            store.link(order, "prepaid item", code, order_at, roles["ap"], link_type="supporting",
                       account="1300", prepaid_item=inno["id"])
            # Planted case 5: a late journal.  The August invoice is entered on 10 September,
            # after August's journals closed on day one.
            new_prepaid("Software subscription", "SWC", "6200", "IT", date(2026, 8, 1), 12, amt(4800, 0),
                        posting_date=date(2026, 8, 31), entry_ts=at(date(2026, 9, 10), 10),
                        period="2026-08")
        if code == "DE01":
            # Planted case 1: a prepaid posted with no source document.  The invoice is
            # uploaded and linked on the platform on 3 September, which clears the exception.
            new_prepaid("Insurance premium", "INS", "6100", "ADM", date(2026, 8, 1), 12, amt(10000, 0),
                        with_document=False, document_at=datetime(2026, 9, 3, 8))

        # Monthly activity.
        prior_revenue = None
        for y, m in periods():
            p = period_key(y, m)
            end = month_end(y, m)
            d1 = day_one(y, m)

            revenue = amt(220000)
            ledger.post(code, ccy, end, at(end, 15), "GL journal", roles["gl"], [
                ("1100", revenue, "SAL", None, f"Revenue {p}"),
                ("4000", -revenue, "SAL", None, f"Revenue {p}"),
            ])
            if prior_revenue is not None:
                receipt_date = date(y, m, 15)
                ledger.post(code, ccy, receipt_date, at(receipt_date, 10), "payment", roles["ap"], [
                    ("1000", prior_revenue, None, None, "Customer receipts"),
                    ("1100", -prior_revenue, None, None, "Customer receipts"),
                ])
            prior_revenue = revenue

            pay = amt(90000, 0.02)
            payday = date(y, m, 25)
            ledger.post(code, ccy, payday, at(payday, 9), "GL journal", roles["gl"], [
                ("6300", pay, "ADM", None, f"Payroll {p}"),
                ("1000", -pay, None, None, f"Payroll {p}"),
            ])

            for _ in range(3):
                inv_date = date(y, m, rng.randint(3, 26))
                amount = amt(rng.choice([800, 1500, 2600]), 0.3)
                vendor_invoice(None, vendor_id("OFF"), inv_date, at(inv_date, rng.randint(9, 16)),
                               "6500", rng.choice(["ADM", "OPS", "SAL"]), amount, "Office supplies")

            if code == "JP01" and p == "2026-09":
                # Planted case 9: decimals in a currency that has none.
                inv_date = date(2026, 9, 8)
                vendor_invoice(None, vendor_id("OFF"), inv_date, at(inv_date, 14), "6500", "ADM",
                               Decimal("12345.5"), "Office supplies")

            if code == "DE01" and p == "2026-09":
                # Planted case 3: a manual journal into prepaids by a user outside the fingerprint.
                fair = money(2400, ccy)
                jd = date(2026, 9, 22)
                ledger.post(code, ccy, jd, at(jd, 16, 40), "GL journal", "taylorb", [
                    ("1300", fair, "SAL", None, "Trade fair stand 2027 - move to prepaid"),
                    ("6500", -fair, "SAL", None, "Trade fair stand 2027 - move to prepaid"),
                ])

            # Utilities accrual at month end, reversed on day one.
            accrual = amt(1500, 0.2)
            acc_doc = ledger.post(code, ccy, end, at(end, 16), "GL journal", roles["gl"], [
                ("6500", accrual, "ADM", vendor_id("UTL"), f"Utilities accrual {p}"),
                ("2100", -accrual, None, vendor_id("UTL"), f"Utilities accrual {p}"),
            ])
            if (y, m) < LAST_PERIOD:
                ledger.post(code, ccy, end + timedelta(days=1), at(d1, 8), "reversal", roles["gl"], [
                    ("2100", accrual, None, vendor_id("UTL"), f"Reverse utilities accrual {p}"),
                    ("6500", -accrual, "ADM", vendor_id("UTL"), f"Reverse utilities accrual {p}"),
                ], reverses=acc_doc)

            dep = money(4000 * scale, ccy)
            ledger.post(code, ccy, end, at(d1, 7, 30), "depreciation run", roles["gl"], [
                ("6400", dep, "OPS", None, f"Depreciation {p}"),
                ("1510", -dep, None, None, f"Depreciation {p}"),
            ], period=p)

            fees = amt(45, 0.3)
            ledger.post(code, ccy, end, at(end, 12), "GL journal", roles["gl"], [
                ("6600", fees, "ADM", None, f"Bank fees {p}"),
                ("1000", -fees, None, None, f"Bank fees {p}"),
            ])

            # Amortisation run on day one, by the AI identity: brings every item up to
            # what the schedule says by period end, so a missed month catches up.
            run_ts = at(d1, 7)
            run_lines = []
            for item in [i for i in prepaid_items if i["company"] == code]:
                posted_invoice = next(l for l in ledger.lines if l["document_number"] == item["invoice_doc"])
                if posted_invoice["entry_timestamp"] > run_ts or posted_invoice["fiscal_period"] > p:
                    continue
                done = -sum(l["amount_local"] for l in ledger.lines
                            if l["posting_type"] == "amortisation run" and l["account_id"] == "1300"
                            and l["line_text"].endswith(item["id"]) and l["company_code"] == code)
                due = schedule_amount_to_date(item, end) - done
                if due:
                    text = f"Amortisation {item['id']}"
                    run_lines.append((item["expense"], due, item["cc"], item["vendor"], text))
                    run_lines.append(("1300", -due, item["cc"], item["vendor"], text))
            if run_lines:
                ledger.post(code, ccy, end, run_ts, "amortisation run", roles["ai"], run_lines, period=p)

        # Supporting evidence beyond the one attachment per line.
        if code == "AU01":
            maint = next(i for i in prepaid_items if i["company"] == code and i["expense"] == "6700")
            cdoc = store.add(code, "contract", maint["vendor"], date(2025, 9, 15), maint["total"], ccy,
                             at(date(2025, 9, 15), 10), roles["ap"],
                             "Maintenance agreement, 24 months, billed in advance",
                             maint["service_start"], maint["service_end"])
            store.link(cdoc, "prepaid item", code, at(date(2025, 9, 15), 10), roles["ap"],
                       link_type="supporting", account="1300", prepaid_item=maint["id"])
            sdoc = store.add(code, "statement", maint["vendor"], date(2026, 9, 30), None, ccy,
                             at(date(2026, 10, 1), 9), roles["ap"],
                             "Vendor statement at 30 September 2026: contract paid in full in advance")
            store.link(sdoc, "reconciliation", code, at(date(2026, 10, 1), 9), roles["ap"],
                       link_type="supporting", account="1300", period="2026-09")

    renumber_by_entry_time(ledger, store)
    return ledger, store, prepaid_items


def renumber_by_entry_time(ledger, store):
    """Number documents in the order they were entered, the way an ERP does,
    so a late journal carries a late document number."""
    first_entry = {}
    for l in ledger.lines:
        key = (l["company_code"], l["document_number"])
        first_entry[key] = min(first_entry.get(key, l["entry_timestamp"]), l["entry_timestamp"])
    new_number = {}
    for company in sorted({c for c, _ in first_entry}):
        docs = sorted((ts, doc) for (c, doc), ts in first_entry.items() if c == company)
        for n, (_, doc) in enumerate(docs, start=1):
            new_number[doc] = f"{company}-{n:06d}"
    for l in ledger.lines:
        l["document_number"] = new_number[l["document_number"]]
        if l["reverses_document"]:
            l["reverses_document"] = new_number[l["reverses_document"]]
        if l["line_text"].startswith("Payment "):
            l["line_text"] = "Payment " + new_number[l["line_text"][len("Payment "):]]
    for k in store.links:
        if k["document_number"]:
            k["document_number"] = new_number[k["document_number"]]


def trial_balance_report(ledger):
    """The ERP's own trial balance, as each run saw it.  One run per company and
    period when its journals close, plus a rerun after any late journal."""
    rows = []
    for code, *_rest, tz, _scale in COMPANIES:
        runs = []
        for y, m in periods():
            p = period_key(y, m)
            runs.append((p, to_utc(day_one(y, m), time(17), tz)))
        late = [l for l in ledger.lines if l["company_code"] == code
                and l["entry_timestamp"] > to_utc(day_one(*map(int, l["fiscal_period"].split("-"))), time(17), tz)]
        for l in late:
            runs.append((l["fiscal_period"], l["entry_timestamp"] + timedelta(hours=1)))
        for p, run_at in sorted(set(runs)):
            seen = [l for l in ledger.lines if l["company_code"] == code
                    and l["entry_timestamp"] <= run_at and l["fiscal_period"] <= p]
            for account in sorted({l["account_id"] for l in seen}):
                opening = sum((l["amount_local"] for l in seen
                               if l["account_id"] == account and l["fiscal_period"] < p), Decimal(0))
                movement = sum((l["amount_local"] for l in seen
                                if l["account_id"] == account and l["fiscal_period"] == p), Decimal(0))
                rows.append((code, p, account, opening, movement, opening + movement, fmt_ts(run_at)))
    return rows


def main():
    # Start the document store folder empty, so no file from an earlier run survives.
    shutil.rmtree(DATA / "documents", ignore_errors=True)
    ledger, store, prepaid_items = generate()
    m = DATA / "master"
    rv = (REVIEW_DATE, REVIEWER)

    write_csv(m / "currencies.csv",
              ["currency_code", "currency_name", "minor_units", "minor_units_source", "active",
               "last_reviewed_at", "last_reviewed_by"],
              [(*c, *rv) for c in CURRENCIES])
    write_csv(m / "companies.csv",
              ["company_code", "company_name", "country", "local_currency", "fy_start_month",
               "cutoff_timezone", "sub_ledgers", "active", "last_reviewed_at", "last_reviewed_by"],
              [(c[0], c[1], c[2], c[3], c[4], c[5], "AP;AR;FA", True, *rv) for c in COMPANIES])
    write_csv(m / "people.csv",
              ["person_id", "display_name", "department", "level", "is_ai_identity",
               "linked_person_id", "active"],
              [(*p, True) for p in PEOPLE])
    write_csv(m / "gl_master.csv",
              ["account_id", "short_name", "long_name", "intended_use", "definition", "account_class",
               "statement", "is_contra", "is_intercompany", "audit_risk", "rec_type", "rec_frequency",
               "prohibited_use", "local_regulatory_notes", "misuse_summary", "active", "policy_link",
               "procedure_link", "work_instruction_link", "last_reviewed_at", "last_reviewed_by"],
              [(a[0], a[1], a[2], a[3], a[2] + ": " + a[3].lower() + ".", a[4], a[5], a[6], a[7],
                a[8], a[9], a[10], a[11], None, a[12], True,
                *((f"docs/prepaid-expenses/{d}.md" for d in ("policy", "procedure", "work-instructions"))
                  if a[0] == "1300" else (None, None, None)),
                *rv) for a in GL_ACCOUNTS])
    write_csv(m / "gl_master_company.csv",
              ["company_code", "account_id", "active_in_company", "restricted_use_note",
               "last_reviewed_at", "last_reviewed_by"],
              [(c[0], a[0], True,
                "Post from the bank statement feed only" if (c[0], a[0]) == ("JP01", "6600") else None, *rv)
               for c in COMPANIES for a in GL_ACCOUNTS])
    cc_rows = [(c[0], cc, name, ROLES[c[0]]["controller"], True) for c in COMPANIES for cc, name in COST_CENTRES]
    cc_rows.append(("AU01", "INNO", "Innovation lab", "samt", True))
    write_csv(m / "cost_centres.csv",
              ["company_code", "cost_centre_id", "name", "owner_person_id", "active"], cc_rows)
    write_csv(m / "vendors.csv",
              ["vendor_id", "vendor_name", "country", "default_currency", "payment_terms_days",
               "payment_method", "is_intercompany", "active", "last_reviewed_at", "last_reviewed_by"],
              [(f"V-{c[2]}-{k[0]}", f"{k[1]} {c[2]}", c[2], c[3], k[2], k[3], False, True, *rv)
               for c in COMPANIES for k in VENDOR_KINDS])
    write_csv(m / "customers.csv",
              ["customer_id", "customer_name", "country", "default_currency", "payment_terms_days",
               "payment_method", "credit_limit", "is_intercompany", "active", "last_reviewed_at",
               "last_reviewed_by"],
              [(f"C-{c[2]}-{n}", f"Customer {c[2]} {n}", c[2], c[3], terms, method,
                money(limit * c[6], c[3]), False, True, *rv)
               for c in COMPANIES
               for n, terms, method, limit in ((1, 30, "bank transfer", 100000),
                                               (2, 14, "direct debit we collect", 50000),
                                               (3, 0, "card", 10000))])
    owners = []
    for c in COMPANIES:
        r = ROLES[c[0]]
        owners += [("owner", r["gl"], c[0], None), ("reviewer", r["controller"], c[0], None),
                   ("approver", "robinf", c[0], None)]
    owners.append(("owner", "alexm", "AU01", "1300"))  # account and company owner wins for AU01 prepaids
    write_csv(m / "account_owners.csv",
              ["assignment_id", "role", "person_id", "company_code", "account_id", "effective_from",
               "effective_to", "last_reviewed_at", "last_reviewed_by"],
              [(f"OWN-{n:03d}", *o, date(2025, 6, 1), None, *rv) for n, o in enumerate(owners, start=1)])
    write_csv(m / "thresholds.csv",
              ["threshold_id", "company_code", "account_id", "max_audit_risk_for_auto", "balance_threshold",
               "max_open_item_age_days", "effective_from", "last_reviewed_at", "last_reviewed_by"],
              [("THR-001", "AU01", "1300", "low", money(50000, "AUD"), 400, date(2025, 6, 1), *rv),
               ("THR-002", "DE01", None, "low", money(30000, "EUR"), 400, date(2025, 6, 1), *rv),
               ("THR-003", "JP01", None, "low", money(8000000, "JPY"), 400, date(2025, 6, 1), *rv)])
    write_csv(m / "parameters.csv",
              ["company_code", "parameter_key", "parameter_value", "unit", "description",
               "last_reviewed_at", "last_reviewed_by"],
              [(None, "fingerprint_lookback_months", "12", "months",
                "Months of posting history the fingerprint is built from", *rv),
               (None, "fingerprint_sample_month_offset", "13", "months",
                "The sample month from the year before, counted back from the first month checked", *rv),
               (None, "fingerprint_min_share", "0.02", "share of lines",
                "Below this share of lines, a value doesn't count as normal for the account", *rv),
               (None, "current_noncurrent_split_months", "12", "months",
                "Amounts amortising within this many months of period end are current", *rv)])
    write_csv(m / "misuse_rules.csv",
              ["rule_id", "account_id", "company_code", "rule_type", "rule_setting", "active",
               "last_reviewed_at", "last_reviewed_by"],
              [("MR-1300-01", "1300", None, "no_source_document",
                "posting types: vendor invoice, GL journal; minimum document-link rate 1.00", True, *rv),
               ("MR-1300-02", "1300", None, "unexpected_posting_type", "posting type not in the fingerprint", True, *rv),
               ("MR-1300-03", "1300", None, "new_cost_centre", "cost centre not in the fingerprint", True, *rv),
               ("MR-1300-04", "1300", None, "unexpected_user", "user not in the fingerprint", True, *rv)])
    write_csv(m / "close_periods.csv",
              ["company_code", "period", "period_end_date", "cutoff_at_utc", "journals_closed_at", "status"],
              [(c[0], period_key(y, m_), month_end(y, m_),
                fmt_ts(to_utc(month_end(y, m_), time(23, 59, 59), c[5])),
                fmt_ts(to_utc(day_one(y, m_), time(17), c[5])), "journals closed")
               for c in COMPANIES for y, m_ in periods()])

    line_cols = ["company_code", "document_number", "line_number", "posting_date", "entry_timestamp",
                 "fiscal_period", "account_id", "cost_centre_id", "vendor_id", "posting_type", "amount_local",
                 "currency", "line_text", "entered_by", "source_document_id", "reverses_document"]
    lines = sorted(ledger.lines, key=lambda l: (l["company_code"], l["document_number"], l["line_number"]))
    write_csv(DATA / "erp" / "journal_lines.csv", line_cols,
              [[fmt_ts(l[c]) if c == "entry_timestamp" else l[c] for c in line_cols] for l in lines])
    write_csv(DATA / "erp" / "trial_balance_report.csv",
              ["company_code", "fiscal_period", "account_id", "opening_balance", "period_movement",
               "closing_balance", "report_run_at"], trial_balance_report(ledger))

    doc_cols = list(store.docs[0].keys())
    write_csv(DATA / "document_store" / "source_documents.csv", doc_cols,
              [[fmt_ts(d[c]) if c == "uploaded_at" else d[c] for c in doc_cols] for d in store.docs])
    link_cols = list(store.links[0].keys())
    write_csv(DATA / "document_store" / "document_links.csv", link_cols,
              [[fmt_ts(k[c]) if c == "linked_at" else k[c] for c in link_cols] for k in store.links])

    # The comments file stands in for the list where owners explain exceptions and
    # differences.  The platform syncs it on a short refresh.
    write_csv(DATA / "comments" / "comments.csv",
              ["comment_id", "company_code", "account_id", "period", "prepaid_item_id", "exception_rule_id",
               "author_person_id", "created_at", "comment_text"],
              [("CMT-001", "AU01", "1300", "2026-09", "PP-AU01-014", "MR-1300-03", "alexm", "2026-09-30 22:00:00",
                "New cost centre INNO is the innovation lab, set up in September.  The subscription is "
                "approved for it."),
               ("CMT-002", "AU01", "1300", "2026-08", None, None, "alexm", "2026-09-10 23:00:00",
                "Invoice PP-AU01-015 was entered on 10 September, after August closed.  Its August "
                "amortisation is caught up in the September run, so the difference is timing only.")])

    print(f"{len(ledger.lines)} journal lines, {len(store.docs)} documents, "
          f"{len(store.links)} document links, {len(prepaid_items)} prepaid items")


if __name__ == "__main__":
    main()
