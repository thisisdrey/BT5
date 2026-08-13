# Q1230: is_signer_authorized: flag desynchronization enables forbidden transitions [a-transfer-while-the-old] [partial-transition]

## Question
Can an unprivileged attacker use `transfer_to_new_account` with a transfer while the old account still carries active balances and flags so `is_signer_authorized` leaves flags inconsistent with real account state, violating `only the canonical account authority or validly derived delegate path may mutate account ownership or migration state` and enabling `Critical: unauthorized takeover of another user account or funds`? Focus specifically on whether a partially completed state transition leaves enough residual state for the attacker to finish it incorrectly.

## Target
- File/function: `programs/marginfi/src/state/marginfi_account.rs` / `is_signer_authorized`
- Entrypoint: `transfer_to_new_account`
- Attacker controls: a transfer while the old account still carries active balances and flags
- Exploit idea: Audit transitions around receivership, flashloan state, frozen state, and migrated accounts for paths that set or clear only part of the state machine. Focus specifically on whether a partially completed state transition leaves enough residual state for the attacker to finish it incorrectly.
- Invariant to test: only the canonical account authority or validly derived delegate path may mutate account ownership or migration state
- Expected Immunefi impact: Critical: unauthorized takeover of another user account or funds
- Fast validation: Execute the controlled transition and assert flags, counters, and balances remain mutually consistent before and after rollback or success. Force failure after the first state mutation and assert the attacker cannot finalize, replay, or redirect the transition.
