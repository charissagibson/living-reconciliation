-- Q001 extract_journal_lines, version 1
-- Extracts every ERP journal line entered up to the run time ($as_at), unchanged.
SELECT
    company_code, document_number, line_number, posting_date, entry_timestamp,
    fiscal_period, account_id, cost_centre_id, vendor_id, posting_type,
    amount_local, currency, line_text, entered_by, source_document_id, reverses_document
FROM erp.journal_lines
WHERE entry_timestamp <= $as_at
