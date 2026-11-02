# Prepaid expenses - policy

Synthetic demo document.  Linked from GL account 1300 in the GL master.

## Purpose

Payments made in advance for a service period are held as prepaid expenses and expensed over that period, so the P&L carries each month's share of the cost.

## What belongs in account 1300

- Vendor invoices for a defined future service period: insurance premiums, software subscriptions, rent in advance, maintenance contracts.
- The monthly amortisation run that expenses them.

## What doesn't

- Deposits, capital expenditure, or anything without a service period.
- Manual reclassifications into prepaids without a source document.

## Rules

- Every prepaid invoice has its source document linked.  No document, no prepaid.
- Prepaids are amortised straight line, monthly, from the first month of the service period.
- The balance is split into current (amortising within 12 months of period end) and non-current.  The 12 months is a parameter, not fixed.
- The account is reconciled monthly.  Audit risk is low, so a rec under the company's threshold, with only scheduled movement, all documents and nothing poorly aged, signs itself off.  Anything else needs a reviewer and an approver.
- A late journal, or any change to a signed-off rec's figures or evidence, reopens the rec.

## Ownership

The account owner for each company is set in the account owners table.  The owner explains exceptions; the reviewer and approver sign off.
