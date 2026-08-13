# Q1225: is_signer_authorized: flag desynchronization enables forbidden transitions [mixed-group-account-contexts-that] [role-reuse]

## Question
Can an unprivileged attacker use `transfer_to_new_account` with mixed group/account contexts that share similar structural fields so `is_signer_authorized` leaves flags inconsistent with real account state, violating `only the canonical account authority or validly derived delegate path may mutate account ownership or migration state` and enabling `Critical: unauthorized takeover of another user account or funds`? Focus specifically on stale authority, migrated-account, or delegated-account reuse after one valid-looking transition.

## Target
- File/function: `programs/marginfi/src/state/marginfi_account.rs` / `is_signer_authorized`
- Entrypoint: `transfer_to_new_account`
- Attacker controls: mixed group/account contexts that share similar structural fields
- Exploit idea: Audit transitions around receivership, flashloan state, frozen state, and migrated accounts for paths that set or clear only part of the state machine. Focus specifically on stale authority, migrated-account, or delegated-account reuse after one valid-looking transition.
- Invariant to test: only the canonical account authority or validly derived delegate path may mutate account ownership or migration state
- Expected Immunefi impact: Critical: unauthorized takeover of another user account or funds
- Fast validation: Execute the controlled transition and assert flags, counters, and balances remain mutually consistent before and after rollback or success. Model one valid transition first, then retry with the attacker as the next caller and assert authority cannot be reused.
