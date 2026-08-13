# Q3604: close_account: freeze semantics can be bypassed through an alternate user flow [an-account-immediately-after-order] [partial-transition]

## Question
Can an unprivileged attacker bypass the intended freeze semantics by invoking `close_account` with an account immediately after order or liquidation activity so `close_account` still changes a blocked account, violating `closing an account must never strand value or release a container that still secures live positions` and causing `High: permanent lock or hidden exposure with real financial effect`? Focus specifically on whether a partially completed state transition leaves enough residual state for the attacker to finish it incorrectly.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/close.rs` / `close_account`
- Entrypoint: `close_account`
- Attacker controls: an account immediately after order or liquidation activity
- Exploit idea: Audit alternate user flows that eventually touch the same balances but may not call the common frozen-account guard. Focus specifically on whether a partially completed state transition leaves enough residual state for the attacker to finish it incorrectly.
- Invariant to test: closing an account must never strand value or release a container that still secures live positions
- Expected Immunefi impact: High: permanent lock or hidden exposure with real financial effect
- Fast validation: Freeze the account, exercise alternate paths, and assert every value-moving route rejects before any state mutation occurs. Force failure after the first state mutation and assert the attacker cannot finalize, replay, or redirect the transition.
