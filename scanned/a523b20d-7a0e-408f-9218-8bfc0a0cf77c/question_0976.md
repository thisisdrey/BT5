# Q976: can_be_closed: flag desynchronization enables forbidden transitions [an-account-where-active-order] [partial-transition]

## Question
Can an unprivileged attacker use `close_account` with an account where active order count and balance count can diverge at boundaries so `can_be_closed` leaves flags inconsistent with real account state, violating `closeability must require the full absence of live economic exposure, blocking flags, and value-bearing side state` and enabling `High: permanent loss, stranding, or unauthorized release of live exposure`? Focus specifically on whether a partially completed state transition leaves enough residual state for the attacker to finish it incorrectly.

## Target
- File/function: `programs/marginfi/src/state/marginfi_account.rs` / `can_be_closed`
- Entrypoint: `close_account`
- Attacker controls: an account where active order count and balance count can diverge at boundaries
- Exploit idea: Audit transitions around receivership, flashloan state, frozen state, and migrated accounts for paths that set or clear only part of the state machine. Focus specifically on whether a partially completed state transition leaves enough residual state for the attacker to finish it incorrectly.
- Invariant to test: closeability must require the full absence of live economic exposure, blocking flags, and value-bearing side state
- Expected Immunefi impact: High: permanent loss, stranding, or unauthorized release of live exposure
- Fast validation: Execute the controlled transition and assert flags, counters, and balances remain mutually consistent before and after rollback or success. Force failure after the first state mutation and assert the attacker cannot finalize, replay, or redirect the transition.
