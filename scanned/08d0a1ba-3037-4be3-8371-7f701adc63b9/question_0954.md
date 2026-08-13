# Q954: can_be_closed: close path leaves live exposure behind [a-migrated-account-pair-with] [partial-transition]

## Question
Can an unprivileged attacker route `close_account` through `can_be_closed` with a migrated account pair with old and new account state both present so an account or balance is considered closable while live exposure still exists, breaking `closeability must require the full absence of live economic exposure, blocking flags, and value-bearing side state` and causing `High: permanent loss, stranding, or unauthorized release of live exposure`? Focus specifically on whether a partially completed state transition leaves enough residual state for the attacker to finish it incorrectly.

## Target
- File/function: `programs/marginfi/src/state/marginfi_account.rs` / `can_be_closed`
- Entrypoint: `close_account`
- Attacker controls: a migrated account pair with old and new account state both present
- Exploit idea: Check whether closeability is computed from stale flags, stale counts, or partial balance scans rather than the full economic state. Focus specifically on whether a partially completed state transition leaves enough residual state for the attacker to finish it incorrectly.
- Invariant to test: closeability must require the full absence of live economic exposure, blocking flags, and value-bearing side state
- Expected Immunefi impact: High: permanent loss, stranding, or unauthorized release of live exposure
- Fast validation: Force the controlled residual state and assert close attempts fail until every active balance and obligation is actually gone. Force failure after the first state mutation and assert the attacker cannot finalize, replay, or redirect the transition.
