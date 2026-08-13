# Q3361: lending_account_close_balance: authority binding bypass on account state mutation [a-balance-with-tiny-residual] [role-reuse]

## Question
Can an unprivileged attacker call `lending_account_close_balance` and make `lending_account_close_balance` accept a balance with tiny residual shares just above or below zero thresholds so another user's account state mutates without valid authority, violating `closing a balance must require truly zero economic exposure and must not drop live shares, debt, or blocking state` and leading to `High: permanent value loss, account bricking, or hidden debt`? Focus specifically on stale authority, migrated-account, or delegated-account reuse after one valid-looking transition.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/close_balance.rs` / `lending_account_close_balance`
- Entrypoint: `lending_account_close_balance`
- Attacker controls: a balance with tiny residual shares just above or below zero thresholds
- Exploit idea: Probe signer checks, transferred authorities, PDA ownership, and migrated-account state so public calls cannot rewrite who controls a margin account. Focus specifically on stale authority, migrated-account, or delegated-account reuse after one valid-looking transition.
- Invariant to test: closing a balance must require truly zero economic exposure and must not drop live shares, debt, or blocking state
- Expected Immunefi impact: High: permanent value loss, account bricking, or hidden debt
- Fast validation: Use two users and assert the controlled call cannot mutate the victim account's owner, flags, balances, or closeability. Model one valid transition first, then retry with the attacker as the next caller and assert authority cannot be reused.
