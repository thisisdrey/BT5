# Q1385: account_not_frozen_for_authority: account-state transition skips a mandatory precondition [a-withdraw-borrow-order-flow] [role-reuse]

## Question
Can an unprivileged attacker call `lending_account_withdraw` with a withdraw/borrow/order flow entered through a sibling path instead of the obvious one so `account_not_frozen_for_authority` performs a state transition without validating a required precondition, breaking `freeze semantics must block every forbidden value-moving path for the affected authority and account` and causing `High: unauthorized state mutation or bypass of safety freeze with financial effect`? Focus specifically on stale authority, migrated-account, or delegated-account reuse after one valid-looking transition.

## Target
- File/function: `programs/marginfi/src/state/marginfi_account.rs` / `account_not_frozen_for_authority`
- Entrypoint: `lending_account_withdraw`
- Attacker controls: a withdraw/borrow/order flow entered through a sibling path instead of the obvious one
- Exploit idea: Focus on initialize/close/freeze/sync transitions where one branch may skip a check that sibling branches enforce. Focus specifically on stale authority, migrated-account, or delegated-account reuse after one valid-looking transition.
- Invariant to test: freeze semantics must block every forbidden value-moving path for the affected authority and account
- Expected Immunefi impact: High: unauthorized state mutation or bypass of safety freeze with financial effect
- Fast validation: Hit the suspect branch directly in a test and assert it rejects the same invalid pre-state that other equivalent branches reject. Model one valid transition first, then retry with the attacker as the next caller and assert authority cannot be reused.
