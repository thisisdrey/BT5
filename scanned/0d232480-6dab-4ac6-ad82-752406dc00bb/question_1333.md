# Q1333: account_not_frozen_for_authority: close path leaves live exposure behind [a-same-slot-sequence-that] [role-reuse]

## Question
Can an unprivileged attacker route `lending_account_withdraw` through `account_not_frozen_for_authority` with a same-slot sequence that freezes and then attempts a value-moving action so an account or balance is considered closable while live exposure still exists, breaking `freeze semantics must block every forbidden value-moving path for the affected authority and account` and causing `High: unauthorized state mutation or bypass of safety freeze with financial effect`? Focus specifically on stale authority, migrated-account, or delegated-account reuse after one valid-looking transition.

## Target
- File/function: `programs/marginfi/src/state/marginfi_account.rs` / `account_not_frozen_for_authority`
- Entrypoint: `lending_account_withdraw`
- Attacker controls: a same-slot sequence that freezes and then attempts a value-moving action
- Exploit idea: Check whether closeability is computed from stale flags, stale counts, or partial balance scans rather than the full economic state. Focus specifically on stale authority, migrated-account, or delegated-account reuse after one valid-looking transition.
- Invariant to test: freeze semantics must block every forbidden value-moving path for the affected authority and account
- Expected Immunefi impact: High: unauthorized state mutation or bypass of safety freeze with financial effect
- Fast validation: Force the controlled residual state and assert close attempts fail until every active balance and obligation is actually gone. Model one valid transition first, then retry with the attacker as the next caller and assert authority cannot be reused.
