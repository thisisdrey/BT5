# Q1187: is_signer_authorized: authority binding bypass on account state mutation [a-migrated-account-context-where] [role-reuse]

## Question
Can an unprivileged attacker call `transfer_to_new_account` and make `is_signer_authorized` accept a migrated-account context where both old and new accounts are present so another user's account state mutates without valid authority, violating `only the canonical account authority or validly derived delegate path may mutate account ownership or migration state` and leading to `Critical: unauthorized takeover of another user account or funds`? Focus specifically on stale authority, migrated-account, or delegated-account reuse after one valid-looking transition.

## Target
- File/function: `programs/marginfi/src/state/marginfi_account.rs` / `is_signer_authorized`
- Entrypoint: `transfer_to_new_account`
- Attacker controls: a migrated-account context where both old and new accounts are present
- Exploit idea: Probe signer checks, transferred authorities, PDA ownership, and migrated-account state so public calls cannot rewrite who controls a margin account. Focus specifically on stale authority, migrated-account, or delegated-account reuse after one valid-looking transition.
- Invariant to test: only the canonical account authority or validly derived delegate path may mutate account ownership or migration state
- Expected Immunefi impact: Critical: unauthorized takeover of another user account or funds
- Fast validation: Use two users and assert the controlled call cannot mutate the victim account's owner, flags, balances, or closeability. Model one valid transition first, then retry with the attacker as the next caller and assert authority cannot be reused.
