# Q1357: account_not_frozen_for_authority: flag desynchronization enables forbidden transitions [a-frozen-account-with-dust] [role-reuse]

## Question
Can an unprivileged attacker use `lending_account_withdraw` with a frozen account with dust-sized balances around closeability checks so `account_not_frozen_for_authority` leaves flags inconsistent with real account state, violating `freeze semantics must block every forbidden value-moving path for the affected authority and account` and enabling `High: unauthorized state mutation or bypass of safety freeze with financial effect`? Focus specifically on stale authority, migrated-account, or delegated-account reuse after one valid-looking transition.

## Target
- File/function: `programs/marginfi/src/state/marginfi_account.rs` / `account_not_frozen_for_authority`
- Entrypoint: `lending_account_withdraw`
- Attacker controls: a frozen account with dust-sized balances around closeability checks
- Exploit idea: Audit transitions around receivership, flashloan state, frozen state, and migrated accounts for paths that set or clear only part of the state machine. Focus specifically on stale authority, migrated-account, or delegated-account reuse after one valid-looking transition.
- Invariant to test: freeze semantics must block every forbidden value-moving path for the affected authority and account
- Expected Immunefi impact: High: unauthorized state mutation or bypass of safety freeze with financial effect
- Fast validation: Execute the controlled transition and assert flags, counters, and balances remain mutually consistent before and after rollback or success. Model one valid transition first, then retry with the attacker as the next caller and assert authority cannot be reused.
