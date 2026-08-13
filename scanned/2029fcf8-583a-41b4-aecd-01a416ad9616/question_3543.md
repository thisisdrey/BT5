# Q3543: close_account: migrated or delegated authority path accepts the wrong signer [an-account-with-stale-vs] [role-reuse]

## Question
Can an unprivileged attacker reach `close_account` from `close_account` with an account with stale vs fresh auxiliary flags so a migrated, delegated, or PDA-owned account accepts the wrong authority, violating `closing an account must never strand value or release a container that still secures live positions` and causing `High: permanent lock or hidden exposure with real financial effect`? Focus specifically on stale authority, migrated-account, or delegated-account reuse after one valid-looking transition.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/close.rs` / `close_account`
- Entrypoint: `close_account`
- Attacker controls: an account with stale vs fresh auxiliary flags
- Exploit idea: Check all alternate authorization paths for mismatched signer identity, stale authority fields, or incorrect PDA derivation assumptions. Focus specifically on stale authority, migrated-account, or delegated-account reuse after one valid-looking transition.
- Invariant to test: closing an account must never strand value or release a container that still secures live positions
- Expected Immunefi impact: High: permanent lock or hidden exposure with real financial effect
- Fast validation: Model authority transfer/migration and verify that only the intended signer path can mutate or close the account at each phase. Model one valid transition first, then retry with the attacker as the next caller and assert authority cannot be reused.
