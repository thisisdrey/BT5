# Q3613: close_account: freeze semantics can be bypassed through an alternate user flow [a-close-attempt-after-tiny] [role-reuse]

## Question
Can an unprivileged attacker bypass the intended freeze semantics by invoking `close_account` with a close attempt after tiny repay/withdraw operations that may zero one side only so `close_account` still changes a blocked account, violating `closing an account must never strand value or release a container that still secures live positions` and causing `High: permanent lock or hidden exposure with real financial effect`? Focus specifically on stale authority, migrated-account, or delegated-account reuse after one valid-looking transition.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/close.rs` / `close_account`
- Entrypoint: `close_account`
- Attacker controls: a close attempt after tiny repay/withdraw operations that may zero one side only
- Exploit idea: Audit alternate user flows that eventually touch the same balances but may not call the common frozen-account guard. Focus specifically on stale authority, migrated-account, or delegated-account reuse after one valid-looking transition.
- Invariant to test: closing an account must never strand value or release a container that still secures live positions
- Expected Immunefi impact: High: permanent lock or hidden exposure with real financial effect
- Fast validation: Freeze the account, exercise alternate paths, and assert every value-moving route rejects before any state mutation occurs. Model one valid transition first, then retry with the attacker as the next caller and assert authority cannot be reused.
