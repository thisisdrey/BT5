# Q1255: is_signer_authorized: account-state transition skips a mandatory precondition [replay-of-a-previously-valid] [role-reuse]

## Question
Can an unprivileged attacker call `transfer_to_new_account` with replay of a previously valid transfer context with a different signer so `is_signer_authorized` performs a state transition without validating a required precondition, breaking `only the canonical account authority or validly derived delegate path may mutate account ownership or migration state` and causing `Critical: unauthorized takeover of another user account or funds`? Focus specifically on stale authority, migrated-account, or delegated-account reuse after one valid-looking transition.

## Target
- File/function: `programs/marginfi/src/state/marginfi_account.rs` / `is_signer_authorized`
- Entrypoint: `transfer_to_new_account`
- Attacker controls: replay of a previously valid transfer context with a different signer
- Exploit idea: Focus on initialize/close/freeze/sync transitions where one branch may skip a check that sibling branches enforce. Focus specifically on stale authority, migrated-account, or delegated-account reuse after one valid-looking transition.
- Invariant to test: only the canonical account authority or validly derived delegate path may mutate account ownership or migration state
- Expected Immunefi impact: Critical: unauthorized takeover of another user account or funds
- Fast validation: Hit the suspect branch directly in a test and assert it rejects the same invalid pre-state that other equivalent branches reject. Model one valid transition first, then retry with the attacker as the next caller and assert authority cannot be reused.
