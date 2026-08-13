# Q910: initialize: indexer-facing flags diverge from enforceable state [an-init-call-where-the] [partial-transition]

## Question
Can an unprivileged attacker make `initialize_account` drive `initialize` with an init call where the target account key has historical state from another workflow so indexer or auxiliary flags diverge from enforceable state and later unlock `High: unauthorized state change or durable victim fund freeze` by violating `new account initialization must bind authority, group, and flags canonically and never let a stranger create a controllable victim context`? Focus specifically on whether a partially completed state transition leaves enough residual state for the attacker to finish it incorrectly.

## Target
- File/function: `programs/marginfi/src/state/marginfi_account.rs` / `initialize`
- Entrypoint: `initialize_account`
- Attacker controls: an init call where the target account key has historical state from another workflow
- Exploit idea: Look for user-triggered sync or bookkeeping paths that appear informational but influence later authorization or closeability decisions. Focus specifically on whether a partially completed state transition leaves enough residual state for the attacker to finish it incorrectly.
- Invariant to test: new account initialization must bind authority, group, and flags canonically and never let a stranger create a controllable victim context
- Expected Immunefi impact: High: unauthorized state change or durable victim fund freeze
- Fast validation: Mutate the controlled flags, then immediately call dependent instructions and assert they still enforce the true on-chain state. Force failure after the first state mutation and assert the attacker cannot finalize, replay, or redirect the transition.
