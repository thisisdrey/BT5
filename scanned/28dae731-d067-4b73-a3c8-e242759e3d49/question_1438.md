# Q1438: account_not_frozen_for_authority: freeze semantics can be bypassed through an alternate user flow [a-frozen-account-with-dust] [partial-transition]

## Question
Can an unprivileged attacker bypass the intended freeze semantics by invoking `lending_account_withdraw` with a frozen account with dust-sized balances around closeability checks so `account_not_frozen_for_authority` still changes a blocked account, violating `freeze semantics must block every forbidden value-moving path for the affected authority and account` and causing `High: unauthorized state mutation or bypass of safety freeze with financial effect`? Focus specifically on whether a partially completed state transition leaves enough residual state for the attacker to finish it incorrectly.

## Target
- File/function: `programs/marginfi/src/state/marginfi_account.rs` / `account_not_frozen_for_authority`
- Entrypoint: `lending_account_withdraw`
- Attacker controls: a frozen account with dust-sized balances around closeability checks
- Exploit idea: Audit alternate user flows that eventually touch the same balances but may not call the common frozen-account guard. Focus specifically on whether a partially completed state transition leaves enough residual state for the attacker to finish it incorrectly.
- Invariant to test: freeze semantics must block every forbidden value-moving path for the affected authority and account
- Expected Immunefi impact: High: unauthorized state mutation or bypass of safety freeze with financial effect
- Fast validation: Freeze the account, exercise alternate paths, and assert every value-moving route rejects before any state mutation occurs. Force failure after the first state mutation and assert the attacker cannot finalize, replay, or redirect the transition.
