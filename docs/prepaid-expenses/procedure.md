# Prepaid expenses - procedure

Synthetic demo document.  Linked from GL account 1300 in the GL master.

## During the month

1. Accounts payable posts the vendor invoice to 1300, with the service period in the line text and the invoice attached.
2. The document is linked to the posting and the prepaid item in the document store.  The platform builds the prepaid schedule from that evidence.

## At close

1. The amortisation run posts each item's share for the month, under the owner's AI identity.
2. Journals close on day one.  The certified extract runs and the trial balance tie-out must pass.
3. The rec builds itself: GL balance, open items, current and non-current split, and exceptions against the account's fingerprint.
4. If the rec qualifies for auto sign-off, the rule engine signs it.  Otherwise:
   - The owner's AI identity prepares it.
   - The owner explains every exception, and any difference, in the comments list.
   - The reviewer reviews it.
   - The approver approves it.  The checksum the approver saw must match the rec at signing.
5. The snapshot script files the signed rec and its open items for the auditors, with an Excel copy.

## After close

- A late journal, or any change to the rec's figures or evidence, reopens it.  It goes back through prepare, review and approve.
- Exceptions not explained by the day-one deadline go to the reviewer.
