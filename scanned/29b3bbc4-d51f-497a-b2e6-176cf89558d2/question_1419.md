# Q1419: account_not_frozen_for_authority: indexer-facing flags diverge from enforceable state [a-frozen-account-just-after] [role-reuse]

## Question
Can an unprivileged attacker make `lending_account_withdraw` drive `account_not_frozen_for_authority` with a frozen account just after order or liquidation flags changed so indexer or auxiliary flags diverge from enforceable state and later unlock `High: unauthorized state mutation or bypass of safety freeze with financial effect` by violating `freeze semantics must block every forbidden value-moving path for the affected authority and account`? Focus specifically on stale authority, migrated-account, or delegated-account reuse after one valid-looking transition.

## Target
- File/function: `programs/marginfi/src/state/marginfi_account.rs` / `account_not_frozen_for_authority`
- Entrypoint: `lending_account_withdraw`
- Attacker controls: a frozen account just after order or liquidation flags changed
- Exploit idea: Look for user-triggered sync or bookkeeping paths that appear informational but influence later authorization or closeability decisions. Focus specifically on stale authority, migrated-account, or delegated-account reuse after one valid-looking transition.
- Invariant to test: freeze semantics must block every forbidden value-moving path for the affected authority and account
- Expected Immunefi impact: High: unauthorized state mutation or bypass of safety freeze with financial effect
- Fast validation: Mutate the controlled flags, then immediately call dependent instructions and assert they still enforce the true on-chain state. Model one valid transition first, then retry with the attacker as the next caller and assert authority cannot be reused.
