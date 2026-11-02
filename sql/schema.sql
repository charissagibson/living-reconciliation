-- Schema for the living reconciliation demo.
-- Tables only; the four views (platform.trial_balance, rec.open_items, rec.exceptions,
-- rec.reconciliation) are built in their own files on top of these.
-- Amounts are DECIMAL(18,3) so the column can hold any currency; each amount is
-- rounded to its own currency's minor units from master.currencies.
-- Timestamps are UTC.

CREATE SCHEMA IF NOT EXISTS master;
CREATE SCHEMA IF NOT EXISTS erp;
CREATE SCHEMA IF NOT EXISTS platform;
CREATE SCHEMA IF NOT EXISTS rec;
CREATE SCHEMA IF NOT EXISTS control;

-- master: the master data family ------------------------------------------

CREATE TABLE master.currencies (
    currency_code       VARCHAR PRIMARY KEY,   -- ISO 4217
    currency_name       VARCHAR NOT NULL,
    minor_units         INTEGER NOT NULL CHECK (minor_units IN (0, 2, 3)),
    minor_units_source  VARCHAR NOT NULL,      -- ISO 4217, or ERP where it differs
    active              BOOLEAN NOT NULL,
    last_reviewed_at    DATE,
    last_reviewed_by    VARCHAR
);

CREATE TABLE master.companies (
    company_code        VARCHAR PRIMARY KEY,
    company_name        VARCHAR NOT NULL,
    country             VARCHAR NOT NULL,
    local_currency      VARCHAR NOT NULL REFERENCES master.currencies (currency_code),
    fy_start_month      INTEGER NOT NULL CHECK (fy_start_month BETWEEN 1 AND 12),
    cutoff_timezone     VARCHAR NOT NULL,      -- the company's cut-off is its local end of day
    sub_ledgers         VARCHAR NOT NULL,      -- semicolon-separated list
    active              BOOLEAN NOT NULL,
    last_reviewed_at    DATE,
    last_reviewed_by    VARCHAR
);

CREATE TABLE master.people (
    person_id           VARCHAR PRIMARY KEY,   -- the login; an AI identity is the login with an _ai suffix
    display_name        VARCHAR NOT NULL,
    department          VARCHAR NOT NULL,
    level               VARCHAR NOT NULL,
    is_ai_identity      BOOLEAN NOT NULL,
    linked_person_id    VARCHAR,               -- for an AI identity, the person it acts for
    active              BOOLEAN NOT NULL
);

CREATE TABLE master.gl_master (
    account_id              VARCHAR PRIMARY KEY,
    short_name              VARCHAR NOT NULL,
    long_name               VARCHAR NOT NULL,
    intended_use            VARCHAR NOT NULL,
    definition              VARCHAR NOT NULL,
    account_class           VARCHAR NOT NULL CHECK (account_class IN ('asset', 'liability', 'equity', 'revenue', 'expense')),
    statement               VARCHAR NOT NULL CHECK (statement IN ('balance sheet', 'P&L')),
    is_contra               BOOLEAN NOT NULL,
    is_intercompany         BOOLEAN NOT NULL,
    audit_risk              VARCHAR NOT NULL CHECK (audit_risk IN ('high', 'medium', 'low')),
    rec_type                VARCHAR NOT NULL,
    rec_frequency           VARCHAR NOT NULL,
    prohibited_use          VARCHAR,
    local_regulatory_notes  VARCHAR,
    misuse_summary          VARCHAR,
    active                  BOOLEAN NOT NULL,
    policy_link             VARCHAR,
    procedure_link          VARCHAR,
    work_instruction_link   VARCHAR,
    last_reviewed_at        DATE,
    last_reviewed_by        VARCHAR
);

CREATE TABLE master.gl_master_company (
    company_code        VARCHAR NOT NULL REFERENCES master.companies (company_code),
    account_id          VARCHAR NOT NULL REFERENCES master.gl_master (account_id),
    active_in_company   BOOLEAN NOT NULL,
    restricted_use_note VARCHAR,
    last_reviewed_at    DATE,
    last_reviewed_by    VARCHAR,
    PRIMARY KEY (company_code, account_id)
);

CREATE TABLE master.cost_centres (
    company_code        VARCHAR NOT NULL REFERENCES master.companies (company_code),
    cost_centre_id      VARCHAR NOT NULL,
    name                VARCHAR NOT NULL,
    owner_person_id     VARCHAR REFERENCES master.people (person_id),
    active              BOOLEAN NOT NULL,
    PRIMARY KEY (company_code, cost_centre_id)
);

CREATE TABLE master.vendors (
    vendor_id           VARCHAR PRIMARY KEY,
    vendor_name         VARCHAR NOT NULL,
    country             VARCHAR NOT NULL,
    default_currency    VARCHAR NOT NULL REFERENCES master.currencies (currency_code),
    payment_terms_days  INTEGER NOT NULL,
    payment_method      VARCHAR NOT NULL CHECK (payment_method IN ('bank transfer', 'direct debit', 'card')),
    is_intercompany     BOOLEAN NOT NULL,
    active              BOOLEAN NOT NULL,
    last_reviewed_at    DATE,
    last_reviewed_by    VARCHAR
);

CREATE TABLE master.customers (
    customer_id         VARCHAR PRIMARY KEY,
    customer_name       VARCHAR NOT NULL,
    country             VARCHAR NOT NULL,
    default_currency    VARCHAR NOT NULL REFERENCES master.currencies (currency_code),
    payment_terms_days  INTEGER NOT NULL,
    payment_method      VARCHAR NOT NULL CHECK (payment_method IN ('bank transfer', 'direct debit we collect', 'card')),
    credit_limit        DECIMAL(18,3),
    is_intercompany     BOOLEAN NOT NULL,
    active              BOOLEAN NOT NULL,
    last_reviewed_at    DATE,
    last_reviewed_by    VARCHAR
);

CREATE TABLE master.account_owners (
    assignment_id       VARCHAR PRIMARY KEY,
    role                VARCHAR NOT NULL CHECK (role IN ('owner', 'reviewer', 'approver')),
    person_id           VARCHAR NOT NULL REFERENCES master.people (person_id),
    company_code        VARCHAR NOT NULL REFERENCES master.companies (company_code),
    account_id          VARCHAR REFERENCES master.gl_master (account_id),  -- blank = company owner
    effective_from      DATE NOT NULL,
    effective_to        DATE,
    last_reviewed_at    DATE,
    last_reviewed_by    VARCHAR
);

CREATE TABLE master.thresholds (
    threshold_id            VARCHAR PRIMARY KEY,
    company_code            VARCHAR NOT NULL REFERENCES master.companies (company_code),
    account_id              VARCHAR REFERENCES master.gl_master (account_id),  -- blank = all accounts
    max_audit_risk_for_auto VARCHAR NOT NULL CHECK (max_audit_risk_for_auto IN ('high', 'medium', 'low')),
    balance_threshold       DECIMAL(18,3) NOT NULL,
    max_open_item_age_days  INTEGER NOT NULL,
    effective_from          DATE NOT NULL,
    last_reviewed_at        DATE,
    last_reviewed_by        VARCHAR
);

CREATE TABLE master.parameters (
    company_code        VARCHAR,               -- blank = default for all companies
    parameter_key       VARCHAR NOT NULL,
    parameter_value     VARCHAR NOT NULL,
    unit                VARCHAR,
    description         VARCHAR NOT NULL,
    last_reviewed_at    DATE,
    last_reviewed_by    VARCHAR
);

CREATE TABLE master.misuse_rules (
    rule_id             VARCHAR PRIMARY KEY,
    account_id          VARCHAR NOT NULL REFERENCES master.gl_master (account_id),
    company_code        VARCHAR,               -- blank = all companies
    rule_type           VARCHAR NOT NULL CHECK (rule_type IN ('no_source_document', 'unexpected_posting_type', 'new_cost_centre', 'unexpected_user')),
    rule_setting        VARCHAR,
    active              BOOLEAN NOT NULL,
    last_reviewed_at    DATE,
    last_reviewed_by    VARCHAR
);

CREATE TABLE master.close_periods (
    company_code        VARCHAR NOT NULL REFERENCES master.companies (company_code),
    period              VARCHAR NOT NULL,      -- YYYY-MM
    period_end_date     DATE NOT NULL,
    cutoff_at_utc       TIMESTAMP NOT NULL,
    journals_closed_at  TIMESTAMP,
    status              VARCHAR NOT NULL CHECK (status IN ('open', 'journals closed')),
    PRIMARY KEY (company_code, period)
);

-- erp: the accounting system of record (synthetic) --------------------------

CREATE TABLE erp.journal_lines (
    company_code        VARCHAR NOT NULL,
    document_number     VARCHAR NOT NULL,
    line_number         INTEGER NOT NULL,
    posting_date        DATE NOT NULL,
    entry_timestamp     TIMESTAMP NOT NULL,
    fiscal_period       VARCHAR NOT NULL,
    account_id          VARCHAR NOT NULL,
    cost_centre_id      VARCHAR,
    vendor_id           VARCHAR,
    posting_type        VARCHAR NOT NULL,
    amount_local        DECIMAL(18,3) NOT NULL,
    currency            VARCHAR NOT NULL,
    line_text           VARCHAR,
    entered_by          VARCHAR NOT NULL,
    source_document_id  VARCHAR,
    reverses_document   VARCHAR,
    PRIMARY KEY (company_code, document_number, line_number)
);

CREATE TABLE erp.trial_balance_report (
    company_code        VARCHAR NOT NULL,
    fiscal_period       VARCHAR NOT NULL,
    account_id          VARCHAR NOT NULL,
    opening_balance     DECIMAL(18,3) NOT NULL,
    period_movement     DECIMAL(18,3) NOT NULL,
    closing_balance     DECIMAL(18,3) NOT NULL,
    report_run_at       TIMESTAMP NOT NULL,
    PRIMARY KEY (company_code, fiscal_period, account_id, report_run_at)
);

-- document store --------------------------------------------------------------

CREATE TABLE platform.source_documents (
    source_document_id      VARCHAR NOT NULL,
    version                 INTEGER NOT NULL,
    document_type           VARCHAR NOT NULL CHECK (document_type IN ('invoice', 'contract', 'statement')),
    company_code            VARCHAR NOT NULL,
    vendor_id               VARCHAR,
    document_date           DATE NOT NULL,
    service_start           DATE,
    service_end             DATE,
    amount                  DECIMAL(18,3),
    currency                VARCHAR,
    store_path              VARCHAR NOT NULL,
    content_hash            VARCHAR NOT NULL,  -- SHA-256 of the file
    uploaded_at             TIMESTAMP NOT NULL,
    uploaded_by             VARCHAR NOT NULL,
    superseded_by_version   INTEGER,
    is_deleted              BOOLEAN NOT NULL,
    PRIMARY KEY (source_document_id, version)
);

CREATE TABLE platform.document_links (
    link_id             VARCHAR PRIMARY KEY,
    source_document_id  VARCHAR NOT NULL,
    linked_to           VARCHAR NOT NULL CHECK (linked_to IN ('journal line', 'prepaid item', 'reconciliation')),
    company_code        VARCHAR NOT NULL,
    account_id          VARCHAR,
    period              VARCHAR,               -- for a rec-level link
    document_number     VARCHAR,               -- for a journal line
    line_number         INTEGER,
    prepaid_item_id     VARCHAR,
    link_type           VARCHAR NOT NULL CHECK (link_type IN ('primary', 'supporting')),
    linked_at           TIMESTAMP NOT NULL,
    linked_by           VARCHAR NOT NULL
);

-- platform: certified extracts and builds --------------------------------------

CREATE TABLE platform.journal_lines (
    company_code        VARCHAR NOT NULL,
    document_number     VARCHAR NOT NULL,
    line_number         INTEGER NOT NULL,
    posting_date        DATE NOT NULL,
    entry_timestamp     TIMESTAMP NOT NULL,
    fiscal_period       VARCHAR NOT NULL,
    account_id          VARCHAR NOT NULL,
    cost_centre_id      VARCHAR,
    vendor_id           VARCHAR,
    posting_type        VARCHAR NOT NULL,
    amount_local        DECIMAL(18,3) NOT NULL,
    currency            VARCHAR NOT NULL,
    line_text           VARCHAR,
    entered_by          VARCHAR NOT NULL,
    source_document_id  VARCHAR,
    reverses_document   VARCHAR,
    extracted_at        TIMESTAMP NOT NULL,
    query_id            VARCHAR NOT NULL,
    query_version       VARCHAR NOT NULL,
    PRIMARY KEY (company_code, document_number, line_number)
);

CREATE TABLE platform.prepaid_schedule (
    prepaid_item_id         VARCHAR PRIMARY KEY,
    company_code            VARCHAR NOT NULL,
    account_id              VARCHAR NOT NULL,
    expense_account_id      VARCHAR,               -- from the ERP amortisation run; blank until it first runs
    cost_centre_id          VARCHAR,
    vendor_id               VARCHAR,
    source_document_id      VARCHAR,
    invoice_document_number VARCHAR NOT NULL,
    total_amount            DECIMAL(18,3) NOT NULL,
    service_start           DATE NOT NULL,
    service_end             DATE NOT NULL,
    months                  INTEGER NOT NULL,
    method                  VARCHAR NOT NULL
);

CREATE TABLE platform.fingerprints (
    company_code        VARCHAR NOT NULL,
    account_id          VARCHAR NOT NULL,
    built_from_period   VARCHAR NOT NULL,
    built_to_period     VARCHAR NOT NULL,
    sample_period       VARCHAR,
    dimension           VARCHAR NOT NULL,
    value               VARCHAR NOT NULL,
    line_count          INTEGER NOT NULL,
    share_of_lines      DECIMAL(9,6) NOT NULL,
    built_at            TIMESTAMP NOT NULL
);

-- rec: the living reconciliation ----------------------------------------------

CREATE TABLE rec.signoff_log (
    signoff_id              BIGINT PRIMARY KEY,
    company_code            VARCHAR NOT NULL,
    account_id              VARCHAR NOT NULL,
    period                  VARCHAR NOT NULL,
    step                    VARCHAR NOT NULL CHECK (step IN ('prepared', 'reviewed', 'approved', 'auto signed off', 'reopened', 'rejected')),
    person_id               VARCHAR NOT NULL,
    signed_at               TIMESTAMP NOT NULL,
    rec_checksum_seen       VARCHAR,
    rec_checksum_at_signing VARCHAR,
    outcome                 VARCHAR NOT NULL,
    note                    VARCHAR,
    previous_row_hash       VARCHAR NOT NULL,
    row_hash                VARCHAR NOT NULL
);

CREATE TABLE rec.snapshots (
    snapshot_id         VARCHAR PRIMARY KEY,
    company_code        VARCHAR NOT NULL,
    account_id          VARCHAR NOT NULL,
    period              VARCHAR NOT NULL,
    signoff_id          BIGINT NOT NULL,
    taken_at            TIMESTAMP NOT NULL,
    taken_by            VARCHAR NOT NULL,
    row_count           INTEGER NOT NULL,
    snapshot_checksum   VARCHAR NOT NULL,
    excel_path          VARCHAR
);

-- rec.snapshot_lines copies every column of rec.reconciliation and rec.open_items
-- as values, so it is created from those views when the rec is built.

CREATE TABLE rec.comments (
    comment_id          VARCHAR PRIMARY KEY,
    company_code        VARCHAR NOT NULL,
    account_id          VARCHAR NOT NULL,
    period              VARCHAR NOT NULL,
    prepaid_item_id     VARCHAR,               -- blank for a rec-level comment
    exception_rule_id   VARCHAR,
    author_person_id    VARCHAR NOT NULL,
    created_at          TIMESTAMP NOT NULL,
    comment_text        VARCHAR NOT NULL,
    synced_at           TIMESTAMP NOT NULL
);

-- control: query certification and the tie-out ---------------------------------

CREATE TABLE control.query_register (
    query_id            VARCHAR PRIMARY KEY,
    query_name          VARCHAR NOT NULL,
    purpose             VARCHAR NOT NULL,
    current_version     VARCHAR NOT NULL,
    code_hash           VARCHAR NOT NULL,
    owner_person_id     VARCHAR NOT NULL,
    status              VARCHAR NOT NULL CHECK (status IN ('certified', 'uncertified')),
    last_certified_at   TIMESTAMP,
    last_certified_by   VARCHAR,
    annual_review_due   DATE
);

CREATE TABLE control.query_certifications (
    certification_id    VARCHAR PRIMARY KEY,
    query_id            VARCHAR NOT NULL,
    query_version       VARCHAR NOT NULL,
    code_hash           VARCHAR NOT NULL,
    erp_report          VARCHAR NOT NULL,
    company_code        VARCHAR NOT NULL,
    period              VARCHAR NOT NULL,
    erp_row_count       INTEGER NOT NULL,
    platform_row_count  INTEGER NOT NULL,
    erp_total           DECIMAL(18,3) NOT NULL,
    platform_total      DECIMAL(18,3) NOT NULL,
    difference          DECIMAL(18,3) NOT NULL,
    result              VARCHAR NOT NULL CHECK (result IN ('pass', 'fail')),
    certified_by        VARCHAR NOT NULL,
    certified_at        TIMESTAMP NOT NULL,
    evidence_path       VARCHAR
);

CREATE TABLE control.change_log (
    change_id                   VARCHAR PRIMARY KEY,
    object_type                 VARCHAR NOT NULL CHECK (object_type IN ('query', 'parameter', 'threshold', 'master')),
    object_id                   VARCHAR NOT NULL,
    version_from                VARCHAR,
    version_to                  VARCHAR,
    what_changed                VARCHAR NOT NULL,
    changed_by                  VARCHAR NOT NULL,
    changed_at                  TIMESTAMP NOT NULL,
    why                         VARCHAR NOT NULL,
    recertification_required    BOOLEAN NOT NULL
);

CREATE TABLE control.tie_out_results (
    run_id                      VARCHAR NOT NULL,
    run_at                      TIMESTAMP NOT NULL,
    company_code                VARCHAR NOT NULL,
    fiscal_period               VARCHAR NOT NULL,
    account_id                  VARCHAR NOT NULL,
    erp_closing_balance         DECIMAL(18,3) NOT NULL,
    platform_closing_balance    DECIMAL(18,3) NOT NULL,
    difference                  DECIMAL(18,3) NOT NULL,
    result                      VARCHAR NOT NULL CHECK (result IN ('pass', 'fail')),
    PRIMARY KEY (run_id, company_code, fiscal_period, account_id)
);
