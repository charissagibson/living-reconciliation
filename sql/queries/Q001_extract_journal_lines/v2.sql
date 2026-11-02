-- Q001 extract_journal_lines, version 2
-- Change: round every amount to its currency's decimals on the way in.
-- (Planted case 7: a well-meant change that alters the data.  Certification catches it.)
SELECT
    j.company_code, j.document_number, j.line_number, j.posting_date, j.entry_timestamp,
    j.fiscal_period, j.account_id, j.cost_centre_id, j.vendor_id, j.posting_type,
    CAST(round(j.amount_local * power(10, c.minor_units)) / power(10, c.minor_units) AS DECIMAL(18,3)) AS amount_local,
    j.currency, j.line_text, j.entered_by, j.source_document_id, j.reverses_document
FROM erp.journal_lines AS j
JOIN master.currencies AS c ON c.currency_code = j.currency
WHERE j.entry_timestamp <= $as_at
