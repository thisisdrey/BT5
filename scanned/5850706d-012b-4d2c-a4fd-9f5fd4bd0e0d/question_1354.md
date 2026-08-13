# Q1354: account_not_frozen_for_authority: flag desynchronization enables forbidden transitions [a-withdraw-borrow-order-flow] [partial-transition]

## Question
Can an unprivileged attacker use `lending_account_withdraw` with a withdraw/borrow/order flow entered through a sibling path instead of the obvious one so `account_not_frozen_for_authority` leaves flags inconsistent with real account state, violating `freeze semantics must block every forbidden value-moving path for the affected authority and account` and enabling `High: unauthorized state mutation or bypass of safety freeze with financial effect`? Focus specifically on whether a partially completed state transition leaves enough residual state for the attacker to finish it incorrectly.

## Target
- File/function: `programs/marginfi/src/state/marginfi_account.rs` / `account_not_frozen_for_authority`
- Entrypoint: `lending_account_withdraw`
- Attacker controls: a withdraw/borrow/order flow entered through a sibling path instead of the obvious one
- Exploit idea: Audit transitions around receivership, flashloan state, frozen state, and migrated accounts for paths that set or clear only part of the state machine. Focus specifically on whether a partially completed state transition leaves enough residual state for the attacker to finish it incorrectly.
- Invariant to test: freeze semantics must block every forbidden value-moving path for the affected authority and account
- Expected Immunefi impact: High: unauthorized state mutation or bypass of safety freeze with financial effect
- Fast validation: Execute the controlled transition and assert flags, counters, and balances remain mutually consistent before and after rollback or success. Force failure after the first state mutation and assert the attacker cannot finalize, replay, or redirect the transition.
