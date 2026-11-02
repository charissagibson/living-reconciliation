-- The living reconciliation: three views over the platform tables.
-- Every view reads the point in time from the variable as_at (UTC), so the rec
-- can be seen exactly as it stood at any moment: SET VARIABLE as_at = TIMESTAMP '...'.
-- Nothing here is stored, so nothing goes stale.

-- Helpers ---------------------------------------------------------------------

-- The smallest unit of a currency, as an exact decimal.
CREATE OR REPLACE MACRO minor_unit(mu) AS
    CASE mu WHEN 0 THEN CAST(1 AS DECIMAL(18,3))
            WHEN 3 THEN CAST(0.001 AS DECIMAL(18,3))
            ELSE CAST(0.01 AS DECIMAL(18,3)) END;

-- Straight line, monthly: what should be amortised by a month end.  The monthly
-- amount is rounded half up to the currency's decimals in whole minor units, so
-- the arithmetic is exact; the last month takes the rounding difference.
CREATE OR REPLACE MACRO amortised_by(total, months, service_start, as_of, mu) AS
    CAST(CASE
        WHEN datediff('month', service_start, as_of) + 1 >= months THEN total
        WHEN datediff('month', service_start, as_of) + 1 <= 0 THEN 0
        ELSE ((2 * CAST(total * CAST(10 ** mu AS INTEGER) AS BIGINT) + months) // (2 * months))
             * minor_unit(mu) * (datediff('month', service_start, as_of) + 1)
    END AS DECIMAL(18,3));

-- A parameter for a company, falling back to the default for all companies.
CREATE OR REPLACE MACRO param(company, key) AS (
    SELECT parameter_value FROM master.parameters
    WHERE parameter_key = key AND (company_code = company OR company_code IS NULL)
    ORDER BY company_code NULLS LAST LIMIT 1);

-- Who holds a role for an account in a company: the account and company
-- assignment wins over the company assignment.
CREATE OR REPLACE MACRO assigned(company, account, role_name) AS (
    SELECT person_id FROM master.account_owners
    WHERE role = role_name AND company_code = company
      AND (account_id = account OR account_id IS NULL)
    ORDER BY account_id NULLS LAST LIMIT 1);

CREATE OR REPLACE MACRO risk_rank(risk) AS
    CASE risk WHEN 'low' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END;

-- rec.open_items: the lines that make up the balance ---------------------------

CREATE OR REPLACE VIEW rec.open_items AS
WITH scope AS (
    SELECT p.company_code, g.account_id, p.period, p.period_end_date, cur.minor_units,
           CAST(param(p.company_code, 'current_noncurrent_split_months') AS INTEGER) AS split_months
    FROM master.close_periods AS p
    JOIN master.companies AS co USING (company_code)
    JOIN master.currencies AS cur ON cur.currency_code = co.local_currency
    JOIN master.gl_master_company AS gc ON gc.company_code = p.company_code AND gc.active_in_company
    JOIN master.gl_master AS g ON g.account_id = gc.account_id AND g.rec_type = 'prepaid_schedule'
), items AS (
    SELECT
        sc.company_code, sc.account_id, sc.period,
        s.prepaid_item_id, s.vendor_id, s.cost_centre_id,
        d.store_path AS primary_document_link,
        (SELECT count(*) FROM platform.document_links AS k
         WHERE k.prepaid_item_id = s.prepaid_item_id AND k.link_type = 'supporting'
           AND k.linked_at <= getvariable('as_at')) AS supporting_document_count,
        s.service_start, s.service_end, s.total_amount,
        amortised_by(s.total_amount, s.months, s.service_start, sc.period_end_date, sc.minor_units)
            AS amortised_to_date,
        s.total_amount - amortised_to_date AS remaining,
        amortised_by(s.total_amount, s.months, s.service_start,
                     last_day(sc.period_end_date + to_months(sc.split_months)),
                     sc.minor_units) - amortised_to_date AS current_portion,
        remaining - current_portion AS non_current_portion,
        datediff('day', j.posting_date, sc.period_end_date) AS age_days,
        j.entered_by
    FROM scope AS sc
    JOIN platform.prepaid_schedule AS s
      ON s.company_code = sc.company_code AND s.account_id = sc.account_id
    JOIN platform.journal_lines AS j
      ON j.company_code = s.company_code AND j.document_number = s.invoice_document_number
     AND j.account_id = s.account_id
    LEFT JOIN platform.source_documents AS d
      ON d.source_document_id = s.source_document_id AND d.superseded_by_version IS NULL
    WHERE j.fiscal_period <= sc.period
)
SELECT * FROM items WHERE remaining > 0;

-- rec.exceptions: postings that don't fit the account's fingerprint -------------

CREATE OR REPLACE VIEW rec.exceptions AS
WITH lines AS (
    SELECT
        p.company_code, j.account_id, p.period, p.journals_closed_at,
        j.document_number, j.line_number, j.posting_type, j.cost_centre_id, j.entered_by,
        strftime(date_trunc('month', p.period_end_date) - INTERVAL 1 MONTH, '%Y-%m') AS fingerprint_to,
        -- The invoice line is the item's own posting; an amortisation line names the
        -- schedule item it amortises.
        coalesce(s.prepaid_item_id,
                 CASE WHEN j.posting_type = 'amortisation run'
                      THEN nullif(regexp_extract(j.line_text, '^Amortisation (\S+)$', 1), '') END)
            AS prepaid_item_id,
        EXISTS (SELECT 1 FROM platform.document_links AS k
                WHERE k.linked_to = 'journal line' AND k.company_code = j.company_code
                  AND k.document_number = j.document_number AND k.line_number = j.line_number
                  AND k.linked_at <= getvariable('as_at')) AS has_document
    FROM master.close_periods AS p
    JOIN platform.journal_lines AS j
      ON j.company_code = p.company_code AND j.fiscal_period = p.period
    JOIN master.gl_master AS g ON g.account_id = j.account_id AND g.rec_type = 'prepaid_schedule'
    LEFT JOIN platform.prepaid_schedule AS s
      ON s.company_code = j.company_code AND s.invoice_document_number = j.document_number
     AND s.account_id = j.account_id
), checked AS (
    -- Only periods that have a fingerprint to test against.
    SELECT l.* FROM lines AS l
    WHERE EXISTS (SELECT 1 FROM platform.fingerprints AS f
                  WHERE f.company_code = l.company_code AND f.account_id = l.account_id
                    AND f.built_to_period = l.fingerprint_to)
), normal AS (
    SELECT company_code, account_id, built_to_period, dimension, value
    FROM platform.fingerprints
    WHERE share_of_lines >= CAST(param(company_code, 'fingerprint_min_share') AS DECIMAL(9,6))
), found AS (
    SELECT
        l.*, r.rule_id, r.rule_type,
        CASE r.rule_type
            WHEN 'no_source_document' THEN 'No source document linked to this ' || l.posting_type
            WHEN 'unexpected_posting_type' THEN 'Posting type "' || l.posting_type || '" is not in the fingerprint'
            WHEN 'new_cost_centre' THEN 'Cost centre ' || l.cost_centre_id || ' is not in the fingerprint'
            WHEN 'unexpected_user' THEN 'User ' || l.entered_by || ' is not in the fingerprint'
        END AS detail
    FROM checked AS l
    JOIN master.misuse_rules AS r
      ON r.active AND r.account_id = l.account_id
     AND (r.company_code IS NULL OR r.company_code = l.company_code)
    WHERE CASE r.rule_type
        WHEN 'no_source_document' THEN
            NOT l.has_document
            AND list_contains(string_split(regexp_extract(r.rule_setting, 'posting types: ([^;]*)', 1), ', '),
                              l.posting_type)
        WHEN 'unexpected_posting_type' THEN NOT EXISTS (
            SELECT 1 FROM normal AS n
            WHERE n.company_code = l.company_code AND n.account_id = l.account_id
              AND n.built_to_period = l.fingerprint_to AND n.dimension = 'posting type'
              AND n.value = l.posting_type)
        WHEN 'new_cost_centre' THEN l.cost_centre_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM normal AS n
            WHERE n.company_code = l.company_code AND n.account_id = l.account_id
              AND n.built_to_period = l.fingerprint_to AND n.dimension = 'cost centre'
              AND n.value = l.cost_centre_id)
        WHEN 'unexpected_user' THEN NOT EXISTS (
            SELECT 1 FROM normal AS n
            WHERE n.company_code = l.company_code AND n.account_id = l.account_id
              AND n.built_to_period = l.fingerprint_to AND n.dimension = 'user'
              AND n.value = l.entered_by)
    END
)
SELECT
    f.company_code, f.account_id, f.period, f.document_number, f.line_number, f.prepaid_item_id,
    f.rule_id, f.rule_type, f.detail,
    f.period AS first_seen_period,
    -- The account owner sees it first.  If it isn't explained by the day-one
    -- deadline (when the period's journals close), it goes to the reviewer.
    CASE WHEN getvariable('as_at') > f.journals_closed_at AND NOT EXISTS (
            SELECT 1 FROM rec.comments AS c
            WHERE c.company_code = f.company_code AND c.account_id = f.account_id
              AND c.period = f.period AND c.exception_rule_id = f.rule_id
              AND (c.prepaid_item_id IS NULL OR c.prepaid_item_id = f.prepaid_item_id)
              AND c.created_at <= f.journals_closed_at)
         THEN assigned(f.company_code, f.account_id, 'reviewer')
         ELSE assigned(f.company_code, f.account_id, 'owner')
    END AS escalate_to_person_id
FROM found AS f;

-- rec.reconciliation: one row per company, account and period -------------------

CREATE OR REPLACE VIEW rec.reconciliation AS
WITH scope AS (
    SELECT p.company_code, g.account_id, p.period, p.period_end_date, p.journals_closed_at, g.audit_risk
    FROM master.close_periods AS p
    JOIN master.gl_master_company AS gc ON gc.company_code = p.company_code AND gc.active_in_company
    JOIN master.gl_master AS g ON g.account_id = gc.account_id AND g.rec_type = 'prepaid_schedule'
), oi AS (
    SELECT company_code, account_id, period,
           sum(remaining) AS total, count(*) AS n, max(age_days) AS oldest,
           string_agg(concat_ws('~', prepaid_item_id, primary_document_link, supporting_document_count,
                                remaining, current_portion, non_current_portion, age_days), '|'
                      ORDER BY prepaid_item_id) AS item_text
    FROM rec.open_items GROUP BY ALL
), ex AS (
    SELECT company_code, account_id, period, count(*) AS n,
           count(*) FILTER (WHERE rule_type = 'no_source_document') AS docs_missing
    FROM rec.exceptions GROUP BY ALL
), moved AS (
    -- "Hasn't moved" means only scheduled movement: anything other than the
    -- amortisation run counts as movement.
    SELECT company_code, account_id, fiscal_period AS period, count(*) AS n
    FROM platform.journal_lines WHERE posting_type <> 'amortisation run' GROUP BY ALL
), tie AS (
    SELECT company_code, fiscal_period AS period, bool_and(result = 'pass') AS passed
    FROM (SELECT * FROM control.tie_out_results WHERE run_at <= getvariable('as_at')
          QUALIFY run_at = max(run_at) OVER (PARTITION BY company_code, fiscal_period))
    GROUP BY ALL
), thr AS (
    SELECT sc.company_code, sc.account_id, sc.period,
           t.max_audit_risk_for_auto, t.balance_threshold, t.max_open_item_age_days
    FROM scope AS sc
    JOIN master.thresholds AS t
      ON t.company_code = sc.company_code AND (t.account_id = sc.account_id OR t.account_id IS NULL)
     AND t.effective_from <= sc.period_end_date
    QUALIFY row_number() OVER (PARTITION BY sc.company_code, sc.account_id, sc.period
                               ORDER BY t.account_id NULLS LAST, t.effective_from DESC) = 1
), last_step AS (
    SELECT company_code, account_id, period, step, outcome
    FROM rec.signoff_log WHERE signed_at <= getvariable('as_at')
    QUALIFY signoff_id = max(signoff_id) OVER (PARTITION BY company_code, account_id, period)
), rec AS (
    SELECT
        sc.company_code, sc.account_id, sc.period, sc.audit_risk,
        coalesce(tb.closing_balance, 0) AS gl_balance,
        coalesce(oi.total, 0) AS open_items_total,
        gl_balance - open_items_total AS difference,
        coalesce(oi.n, 0) AS open_item_count,
        oi.oldest AS oldest_item_age_days,
        coalesce(ex.docs_missing, 0) AS documents_missing,
        coalesce(ex.n, 0) AS exception_count,
        coalesce(mv.n, 0) > 0 AS moved_since_last_signoff,
        coalesce(sc.journals_closed_at <= getvariable('as_at'), false) AS journals_closed,
        coalesce(tie.passed, false) AS tie_out_passed,
        coalesce(abs(gl_balance) <= thr.balance_threshold
                 AND risk_rank(sc.audit_risk) <= risk_rank(thr.max_audit_risk_for_auto), false) AS under_threshold,
        under_threshold AND NOT moved_since_last_signoff AND documents_missing = 0 AND exception_count = 0
            AND difference = 0 AND coalesce(oldest_item_age_days, 0) <= thr.max_open_item_age_days
            AND journals_closed AND tie_out_passed AS auto_signoff_eligible,
        sha256(concat_ws('|', sc.company_code, sc.account_id, sc.period, gl_balance, open_items_total,
                         difference, exception_count, documents_missing, coalesce(oi.item_text, '')))
            AS rec_checksum,
        CASE
            WHEN ls.step = 'approved' AND ls.outcome = 'accepted' THEN 'certified'
            WHEN ls.step = 'auto signed off' AND ls.outcome = 'accepted' THEN 'auto signed off'
            WHEN ls.step = 'reopened' THEN 'reopened'
            ELSE 'uncertified'
        END AS status
    FROM scope AS sc
    LEFT JOIN (SELECT company_code, account_id, fiscal_period AS period, closing_balance
               FROM platform.trial_balance) AS tb USING (company_code, account_id, period)
    LEFT JOIN oi USING (company_code, account_id, period)
    LEFT JOIN ex USING (company_code, account_id, period)
    LEFT JOIN moved AS mv USING (company_code, account_id, period)
    LEFT JOIN tie USING (company_code, period)
    LEFT JOIN thr USING (company_code, account_id, period)
    LEFT JOIN last_step AS ls USING (company_code, account_id, period)
)
SELECT * FROM rec;
