# Q1424: account_not_frozen_for_authority: indexer-facing flags diverge from enforceable state [a-frozen-account-where-sync] [partial-transition]

## Question
Can an unprivileged attacker make `lending_account_withdraw` drive `account_not_frozen_for_authority` with a frozen account where sync-like helper paths run before the main action so indexer or auxiliary flags diverge from enforceable state and later unlock `High: unauthorized state mutation or bypass of safety freeze with financial effect` by violating `freeze semantics must block every forbidden value-moving path for the affected authority and account`? Focus specifically on whether a partially completed state transition leaves enough residual state for the attacker to finish it incorrectly.

## Target
- File/function: `programs/marginfi/src/state/marginfi_account.rs` / `account_not_frozen_for_authority`
- Entrypoint: `lending_account_withdraw`
- Attacker controls: a frozen account where sync-like helper paths run before the main action
- Exploit idea: Look for user-triggered sync or bookkeeping paths that appear informational but influence later authorization or closeability decisions. Focus specifically on whether a partially completed state transition leaves enough residual state for the attacker to finish it incorrectly.
- Invariant to test: freeze semantics must block every forbidden value-moving path for the affected authority and account
- Expected Immunefi impact: High: unauthorized state mutation or bypass of safety freeze with financial effect
- Fast validation: Mutate the controlled flags, then immediately call dependent instructions and assert they still enforce the true on-chain state. Force failure after the first state mutation and assert the attacker cannot finalize, replay, or redirect the transition.
