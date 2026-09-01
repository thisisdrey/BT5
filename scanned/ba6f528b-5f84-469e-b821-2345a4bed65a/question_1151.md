# Q1151: event - token transfer path allows self-transfer or zero-amount accounting drift (2)

## Question
Given the account is registered and still holds a non-zero balance, can an unprivileged attacker, entering through a receiver or callee contract the attacker deployed, invoked during a transfer callback, call `Event` in `contracts/treasury-logger/src/event.rs` with `receiver_id == sender_id`, a zero amount, or a duplicated entry in a batch so the balance accounting double-counts, breaking the invariant `total supply and the sum of balances are unchanged by any transfer` and leading to unauthorized minting / balance inflation: a balance is credited with no matching asset received?

## Target
- File/function: [contracts/treasury-logger/src/event.rs](contracts/treasury-logger/src/event.rs) - `Event`
- Entrypoint: a receiver or callee contract the attacker deployed, invoked during a transfer callback
- Attacker controls: the callee's return value, panics, and gas consumption
- Exploit idea: Self-transfer must be a no-op or rejected; a batch containing the same account twice can add before subtracting. Set-up: the account is registered and still holds a non-zero balance.
- Invariant to test: total supply and the sum of balances are unchanged by any transfer
- Expected Immunefi impact: Critical - Unauthorized minting / balance inflation: a balance is credited with no matching asset received
- Fast validation: Self-transfer and duplicate-entry batch; assert supply invariance.
