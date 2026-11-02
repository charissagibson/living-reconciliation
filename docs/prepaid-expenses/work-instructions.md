# Prepaid expenses - work instructions

Synthetic demo document.  Linked from GL account 1300 in the GL master.

## Account owner: clearing an exception

1. Open `rec.exceptions` for your company and period.
2. For each exception, check the posting against the fingerprint rule it broke:
   - No source document: find the invoice and link it.  The exception clears on the next refresh.
   - New cost centre, unexpected user or posting type: confirm whether the posting belongs in 1300.  If it doesn't, ask for a reclassification.  If it does, explain why.
3. Add a comment to the comments list with the company, account, period, prepaid item and the rule ID, before the day-one deadline.

## Account owner: explaining a difference

If `rec.reconciliation` shows a difference between the GL balance and the open items, add a comment on the rec itself (no prepaid item, no rule ID) saying what causes it and when it clears.

## Reviewer and approver

1. Open `rec.reconciliation` for the period.  Check the tie-out passed, journals are closed, and every exception and difference has a comment.
2. Sign off.  The sign-off records the rec checksum you saw.  If anything changed while you had it open, the sign-off is rejected and the rec reopens.  Open it again and re-check.
