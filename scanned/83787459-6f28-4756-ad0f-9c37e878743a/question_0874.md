# Q874: initialize: account-state transition skips a mandatory precondition [repeated-init-attempts-against-a] [partial-transition]

## Question
Can an unprivileged attacker call `initialize_account` with repeated init attempts against a partially initialized object so `initialize` performs a state transition without validating a required precondition, breaking `new account initialization must bind authority, group, and flags canonically and never let a stranger create a controllable victim context` and causing `High: unauthorized state change or durable victim fund freeze`? Focus specifically on whether a partially completed state transition leaves enough residual state for the attacker to finish it incorrectly.

## Target
- File/function: `programs/marginfi/src/state/marginfi_account.rs` / `initialize`
- Entrypoint: `initialize_account`
- Attacker controls: repeated init attempts against a partially initialized object
- Exploit idea: Focus on initialize/close/freeze/sync transitions where one branch may skip a check that sibling branches enforce. Focus specifically on whether a partially completed state transition leaves enough residual state for the attacker to finish it incorrectly.
- Invariant to test: new account initialization must bind authority, group, and flags canonically and never let a stranger create a controllable victim context
- Expected Immunefi impact: High: unauthorized state change or durable victim fund freeze
- Fast validation: Hit the suspect branch directly in a test and assert it rejects the same invalid pre-state that other equivalent branches reject. Force failure after the first state mutation and assert the attacker cannot finalize, replay, or redirect the transition.
