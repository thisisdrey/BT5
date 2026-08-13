# Q1283: is_signer_authorized: indexer-facing flags diverge from enforceable state [a-migrated-account-context-where] [role-reuse]

## Question
Can an unprivileged attacker make `transfer_to_new_account` drive `is_signer_authorized` with a migrated-account context where both old and new accounts are present so indexer or auxiliary flags diverge from enforceable state and later unlock `Critical: unauthorized takeover of another user account or funds` by violating `only the canonical account authority or validly derived delegate path may mutate account ownership or migration state`? Focus specifically on stale authority, migrated-account, or delegated-account reuse after one valid-looking transition.

## Target
- File/function: `programs/marginfi/src/state/marginfi_account.rs` / `is_signer_authorized`
- Entrypoint: `transfer_to_new_account`
- Attacker controls: a migrated-account context where both old and new accounts are present
- Exploit idea: Look for user-triggered sync or bookkeeping paths that appear informational but influence later authorization or closeability decisions. Focus specifically on stale authority, migrated-account, or delegated-account reuse after one valid-looking transition.
- Invariant to test: only the canonical account authority or validly derived delegate path may mutate account ownership or migration state
- Expected Immunefi impact: Critical: unauthorized takeover of another user account or funds
- Fast validation: Mutate the controlled flags, then immediately call dependent instructions and assert they still enforce the true on-chain state. Model one valid transition first, then retry with the attacker as the next caller and assert authority cannot be reused.
