# Q1310: is_signer_authorized: freeze semantics can be bypassed through an alternate user flow [a-transfer-while-the-old] [partial-transition]

## Question
Can an unprivileged attacker bypass the intended freeze semantics by invoking `transfer_to_new_account` with a transfer while the old account still carries active balances and flags so `is_signer_authorized` still changes a blocked account, violating `only the canonical account authority or validly derived delegate path may mutate account ownership or migration state` and causing `Critical: unauthorized takeover of another user account or funds`? Focus specifically on whether a partially completed state transition leaves enough residual state for the attacker to finish it incorrectly.

## Target
- File/function: `programs/marginfi/src/state/marginfi_account.rs` / `is_signer_authorized`
- Entrypoint: `transfer_to_new_account`
- Attacker controls: a transfer while the old account still carries active balances and flags
- Exploit idea: Audit alternate user flows that eventually touch the same balances but may not call the common frozen-account guard. Focus specifically on whether a partially completed state transition leaves enough residual state for the attacker to finish it incorrectly.
- Invariant to test: only the canonical account authority or validly derived delegate path may mutate account ownership or migration state
- Expected Immunefi impact: Critical: unauthorized takeover of another user account or funds
- Fast validation: Freeze the account, exercise alternate paths, and assert every value-moving route rejects before any state mutation occurs. Force failure after the first state mutation and assert the attacker cannot finalize, replay, or redirect the transition.
