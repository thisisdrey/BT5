# Q1239: is_signer_authorized: migrated or delegated authority path accepts the wrong signer [replay-of-a-previously-valid] [role-reuse]

## Question
Can an unprivileged attacker reach `is_signer_authorized` from `transfer_to_new_account` with replay of a previously valid transfer context with a different signer so a migrated, delegated, or PDA-owned account accepts the wrong authority, violating `only the canonical account authority or validly derived delegate path may mutate account ownership or migration state` and causing `Critical: unauthorized takeover of another user account or funds`? Focus specifically on stale authority, migrated-account, or delegated-account reuse after one valid-looking transition.

## Target
- File/function: `programs/marginfi/src/state/marginfi_account.rs` / `is_signer_authorized`
- Entrypoint: `transfer_to_new_account`
- Attacker controls: replay of a previously valid transfer context with a different signer
- Exploit idea: Check all alternate authorization paths for mismatched signer identity, stale authority fields, or incorrect PDA derivation assumptions. Focus specifically on stale authority, migrated-account, or delegated-account reuse after one valid-looking transition.
- Invariant to test: only the canonical account authority or validly derived delegate path may mutate account ownership or migration state
- Expected Immunefi impact: Critical: unauthorized takeover of another user account or funds
- Fast validation: Model authority transfer/migration and verify that only the intended signer path can mutate or close the account at each phase. Model one valid transition first, then retry with the attacker as the next caller and assert authority cannot be reused.
