# Q1542: lib - token transfer path allows self-transfer or zero-amount accounting drift (4)

## Question
Given the account is registered and still holds a non-zero balance, can an unprivileged attacker, entering through a token the attacker issued and then had the Verifier custody, call `Contract` in `contracts/treasury-logger/src/lib.rs` with `receiver_id == sender_id`, a zero amount, or a duplicated entry in a batch so the balance accounting double-counts, breaking the invariant `total supply and the sum of balances are unchanged by any transfer` and leading to unauthorized minting / balance inflation: a balance is credited with no matching asset received?

## Target
- File/function: [contracts/treasury-logger/src/lib.rs](contracts/treasury-logger/src/lib.rs) - `Contract` (cross-check `mt_on_transfer` in the same file)
- Entrypoint: a token the attacker issued and then had the Verifier custody
- Attacker controls: the token's behaviour on transfer, refund and metadata reads
- Exploit idea: Self-transfer must be a no-op or rejected; a batch containing the same account twice can add before subtracting. Set-up: the account is registered and still holds a non-zero balance.
- Invariant to test: total supply and the sum of balances are unchanged by any transfer
- Expected Immunefi impact: Critical - Unauthorized minting / balance inflation: a balance is credited with no matching asset received
- Fast validation: Self-transfer and duplicate-entry batch; assert supply invariance.
