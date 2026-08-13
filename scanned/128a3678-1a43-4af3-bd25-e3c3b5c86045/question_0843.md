# Q843: initialize: flag desynchronization enables forbidden transitions [init-plus-a-later-transfer] [role-reuse]

## Question
Can an unprivileged attacker use `initialize_account` with init plus a later transfer-account path in the same investigation so `initialize` leaves flags inconsistent with real account state, violating `new account initialization must bind authority, group, and flags canonically and never let a stranger create a controllable victim context` and enabling `High: unauthorized state change or durable victim fund freeze`? Focus specifically on stale authority, migrated-account, or delegated-account reuse after one valid-looking transition.

## Target
- File/function: `programs/marginfi/src/state/marginfi_account.rs` / `initialize`
- Entrypoint: `initialize_account`
- Attacker controls: init plus a later transfer-account path in the same investigation
- Exploit idea: Audit transitions around receivership, flashloan state, frozen state, and migrated accounts for paths that set or clear only part of the state machine. Focus specifically on stale authority, migrated-account, or delegated-account reuse after one valid-looking transition.
- Invariant to test: new account initialization must bind authority, group, and flags canonically and never let a stranger create a controllable victim context
- Expected Immunefi impact: High: unauthorized state change or durable victim fund freeze
- Fast validation: Execute the controlled transition and assert flags, counters, and balances remain mutually consistent before and after rollback or success. Model one valid transition first, then retry with the attacker as the next caller and assert authority cannot be reused.
