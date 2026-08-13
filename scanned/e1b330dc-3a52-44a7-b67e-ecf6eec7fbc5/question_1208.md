# Q1208: is_signer_authorized: close path leaves live exposure behind [replay-of-a-previously-valid] [partial-transition]

## Question
Can an unprivileged attacker route `transfer_to_new_account` through `is_signer_authorized` with replay of a previously valid transfer context with a different signer so an account or balance is considered closable while live exposure still exists, breaking `only the canonical account authority or validly derived delegate path may mutate account ownership or migration state` and causing `Critical: unauthorized takeover of another user account or funds`? Focus specifically on whether a partially completed state transition leaves enough residual state for the attacker to finish it incorrectly.

## Target
- File/function: `programs/marginfi/src/state/marginfi_account.rs` / `is_signer_authorized`
- Entrypoint: `transfer_to_new_account`
- Attacker controls: replay of a previously valid transfer context with a different signer
- Exploit idea: Check whether closeability is computed from stale flags, stale counts, or partial balance scans rather than the full economic state. Focus specifically on whether a partially completed state transition leaves enough residual state for the attacker to finish it incorrectly.
- Invariant to test: only the canonical account authority or validly derived delegate path may mutate account ownership or migration state
- Expected Immunefi impact: Critical: unauthorized takeover of another user account or funds
- Fast validation: Force the controlled residual state and assert close attempts fail until every active balance and obligation is actually gone. Force failure after the first state mutation and assert the attacker cannot finalize, replay, or redirect the transition.
