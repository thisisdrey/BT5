# Q3564: close_account: account-state transition skips a mandatory precondition [an-account-that-just-exited] [partial-transition]

## Question
Can an unprivileged attacker call `close_account` with an account that just exited a flashloan or receivership state so `close_account` performs a state transition without validating a required precondition, breaking `closing an account must never strand value or release a container that still secures live positions` and causing `High: permanent lock or hidden exposure with real financial effect`? Focus specifically on whether a partially completed state transition leaves enough residual state for the attacker to finish it incorrectly.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/close.rs` / `close_account`
- Entrypoint: `close_account`
- Attacker controls: an account that just exited a flashloan or receivership state
- Exploit idea: Focus on initialize/close/freeze/sync transitions where one branch may skip a check that sibling branches enforce. Focus specifically on whether a partially completed state transition leaves enough residual state for the attacker to finish it incorrectly.
- Invariant to test: closing an account must never strand value or release a container that still secures live positions
- Expected Immunefi impact: High: permanent lock or hidden exposure with real financial effect
- Fast validation: Hit the suspect branch directly in a test and assert it rejects the same invalid pre-state that other equivalent branches reject. Force failure after the first state mutation and assert the attacker cannot finalize, replay, or redirect the transition.
