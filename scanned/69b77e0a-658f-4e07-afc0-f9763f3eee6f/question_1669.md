# Q1669: contract - token transfer path allows self-transfer or zero-amount accounting drift

## Question
Given the account is registered and still holds a non-zero balance, can an unprivileged attacker, entering through the contract's own public entrypoint called by any account, call `ft_transfer` in `contracts/poa/token/src/contract.rs` with `receiver_id == sender_id`, a zero amount, or a duplicated entry in a batch so the balance accounting double-counts, breaking the invariant `total supply and the sum of balances are unchanged by any transfer` and leading to unauthorized minting / balance inflation: a balance is credited with no matching asset received?

## Target
- File/function: [contracts/poa/token/src/contract.rs](contracts/poa/token/src/contract.rs) - `ft_transfer` (cross-check `ft_resolve_transfer` in the same file)
- Entrypoint: the contract's own public entrypoint called by any account
- Attacker controls: every argument of the call and the calling account id
- Exploit idea: Self-transfer must be a no-op or rejected; a batch containing the same account twice can add before subtracting. Set-up: the account is registered and still holds a non-zero balance.
- Invariant to test: total supply and the sum of balances are unchanged by any transfer
- Expected Immunefi impact: Critical - Unauthorized minting / balance inflation: a balance is credited with no matching asset received
- Fast validation: Self-transfer and duplicate-entry batch; assert supply invariance.
