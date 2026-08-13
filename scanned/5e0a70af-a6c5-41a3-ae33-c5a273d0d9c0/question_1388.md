# Q1388: account_not_frozen_for_authority: account-state transition skips a mandatory precondition [a-frozen-account-just-after] [partial-transition]

## Question
Can an unprivileged attacker call `lending_account_withdraw` with a frozen account just after order or liquidation flags changed so `account_not_frozen_for_authority` performs a state transition without validating a required precondition, breaking `freeze semantics must block every forbidden value-moving path for the affected authority and account` and causing `High: unauthorized state mutation or bypass of safety freeze with financial effect`? Focus specifically on whether a partially completed state transition leaves enough residual state for the attacker to finish it incorrectly.

## Target
- File/function: `programs/marginfi/src/state/marginfi_account.rs` / `account_not_frozen_for_authority`
- Entrypoint: `lending_account_withdraw`
- Attacker controls: a frozen account just after order or liquidation flags changed
- Exploit idea: Focus on initialize/close/freeze/sync transitions where one branch may skip a check that sibling branches enforce. Focus specifically on whether a partially completed state transition leaves enough residual state for the attacker to finish it incorrectly.
- Invariant to test: freeze semantics must block every forbidden value-moving path for the affected authority and account
- Expected Immunefi impact: High: unauthorized state mutation or bypass of safety freeze with financial effect
- Fast validation: Hit the suspect branch directly in a test and assert it rejects the same invalid pre-state that other equivalent branches reject. Force failure after the first state mutation and assert the attacker cannot finalize, replay, or redirect the transition.
