# Q1311: is_signer_authorized: freeze semantics can be bypassed through an alternate user flow [a-transfer-where-the-attacker] [role-reuse]

## Question
Can an unprivileged attacker bypass the intended freeze semantics by invoking `transfer_to_new_account` with a transfer where the attacker controls the fee-payer and destination accounts so `is_signer_authorized` still changes a blocked account, violating `only the canonical account authority or validly derived delegate path may mutate account ownership or migration state` and causing `Critical: unauthorized takeover of another user account or funds`? Focus specifically on stale authority, migrated-account, or delegated-account reuse after one valid-looking transition.

## Target
- File/function: `programs/marginfi/src/state/marginfi_account.rs` / `is_signer_authorized`
- Entrypoint: `transfer_to_new_account`
- Attacker controls: a transfer where the attacker controls the fee-payer and destination accounts
- Exploit idea: Audit alternate user flows that eventually touch the same balances but may not call the common frozen-account guard. Focus specifically on stale authority, migrated-account, or delegated-account reuse after one valid-looking transition.
- Invariant to test: only the canonical account authority or validly derived delegate path may mutate account ownership or migration state
- Expected Immunefi impact: Critical: unauthorized takeover of another user account or funds
- Fast validation: Freeze the account, exercise alternate paths, and assert every value-moving route rejects before any state mutation occurs. Model one valid transition first, then retry with the attacker as the next caller and assert authority cannot be reused.
