-- platform.trial_balance: the platform's trial balance, built only from the
-- certified extract in platform.journal_lines.
CREATE OR REPLACE VIEW platform.trial_balance AS
SELECT
    p.company_code,
    p.period AS fiscal_period,
    j.account_id,
    coalesce(sum(j.amount_local) FILTER (WHERE j.fiscal_period < p.period), 0) AS opening_balance,
    coalesce(sum(j.amount_local) FILTER (WHERE j.fiscal_period = p.period), 0) AS period_movement,
    sum(j.amount_local) AS closing_balance
FROM master.close_periods AS p
JOIN platform.journal_lines AS j
    ON j.company_code = p.company_code
   AND j.fiscal_period <= p.period
GROUP BY p.company_code, p.period, j.account_id;
