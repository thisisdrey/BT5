# Q1036: can_be_closed: indexer-facing flags diverge from enforceable state [an-account-that-just-ended] [partial-transition]

## Question
Can an unprivileged attacker make `close_account` drive `can_be_closed` with an account that just ended a flashloan or receivership phase so indexer or auxiliary flags diverge from enforceable state and later unlock `High: permanent loss, stranding, or unauthorized release of live exposure` by violating `closeability must require the full absence of live economic exposure, blocking flags, and value-bearing side state`? Focus specifically on whether a partially completed state transition leaves enough residual state for the attacker to finish it incorrectly.

## Target
- File/function: `programs/marginfi/src/state/marginfi_account.rs` / `can_be_closed`
- Entrypoint: `close_account`
- Attacker controls: an account that just ended a flashloan or receivership phase
- Exploit idea: Look for user-triggered sync or bookkeeping paths that appear informational but influence later authorization or closeability decisions. Focus specifically on whether a partially completed state transition leaves enough residual state for the attacker to finish it incorrectly.
- Invariant to test: closeability must require the full absence of live economic exposure, blocking flags, and value-bearing side state
- Expected Immunefi impact: High: permanent loss, stranding, or unauthorized release of live exposure
- Fast validation: Mutate the controlled flags, then immediately call dependent instructions and assert they still enforce the true on-chain state. Force failure after the first state mutation and assert the attacker cannot finalize, replay, or redirect the transition.
