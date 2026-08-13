# Q1209: is_signer_authorized: close path leaves live exposure behind [mixed-group-account-contexts-that] [role-reuse]

## Question
Can an unprivileged attacker route `transfer_to_new_account` through `is_signer_authorized` with mixed group/account contexts that share similar structural fields so an account or balance is considered closable while live exposure still exists, breaking `only the canonical account authority or validly derived delegate path may mutate account ownership or migration state` and causing `Critical: unauthorized takeover of another user account or funds`? Focus specifically on stale authority, migrated-account, or delegated-account reuse after one valid-looking transition.

## Target
- File/function: `programs/marginfi/src/state/marginfi_account.rs` / `is_signer_authorized`
- Entrypoint: `transfer_to_new_account`
- Attacker controls: mixed group/account contexts that share similar structural fields
- Exploit idea: Check whether closeability is computed from stale flags, stale counts, or partial balance scans rather than the full economic state. Focus specifically on stale authority, migrated-account, or delegated-account reuse after one valid-looking transition.
- Invariant to test: only the canonical account authority or validly derived delegate path may mutate account ownership or migration state
- Expected Immunefi impact: Critical: unauthorized takeover of another user account or funds
- Fast validation: Force the controlled residual state and assert close attempts fail until every active balance and obligation is actually gone. Model one valid transition first, then retry with the attacker as the next caller and assert authority cannot be reused.
