# Q3506: close_account: close path leaves live exposure behind [an-account-with-only-dust] [partial-transition]

## Question
Can an unprivileged attacker route `close_account` through `close_account` with an account with only dust-sized residual balances so an account or balance is considered closable while live exposure still exists, breaking `closing an account must never strand value or release a container that still secures live positions` and causing `High: permanent lock or hidden exposure with real financial effect`? Focus specifically on whether a partially completed state transition leaves enough residual state for the attacker to finish it incorrectly.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/close.rs` / `close_account`
- Entrypoint: `close_account`
- Attacker controls: an account with only dust-sized residual balances
- Exploit idea: Check whether closeability is computed from stale flags, stale counts, or partial balance scans rather than the full economic state. Focus specifically on whether a partially completed state transition leaves enough residual state for the attacker to finish it incorrectly.
- Invariant to test: closing an account must never strand value or release a container that still secures live positions
- Expected Immunefi impact: High: permanent lock or hidden exposure with real financial effect
- Fast validation: Force the controlled residual state and assert close attempts fail until every active balance and obligation is actually gone. Force failure after the first state mutation and assert the attacker cannot finalize, replay, or redirect the transition.
