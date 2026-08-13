# Q3519: close_account: close path leaves live exposure behind [an-account-with-historical-disabled] [role-reuse]

## Question
Can an unprivileged attacker route `close_account` through `close_account` with an account with historical disabled/bankrupted markers so an account or balance is considered closable while live exposure still exists, breaking `closing an account must never strand value or release a container that still secures live positions` and causing `High: permanent lock or hidden exposure with real financial effect`? Focus specifically on stale authority, migrated-account, or delegated-account reuse after one valid-looking transition.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/close.rs` / `close_account`
- Entrypoint: `close_account`
- Attacker controls: an account with historical disabled/bankrupted markers
- Exploit idea: Check whether closeability is computed from stale flags, stale counts, or partial balance scans rather than the full economic state. Focus specifically on stale authority, migrated-account, or delegated-account reuse after one valid-looking transition.
- Invariant to test: closing an account must never strand value or release a container that still secures live positions
- Expected Immunefi impact: High: permanent lock or hidden exposure with real financial effect
- Fast validation: Force the controlled residual state and assert close attempts fail until every active balance and obligation is actually gone. Model one valid transition first, then retry with the attacker as the next caller and assert authority cannot be reused.
