# Q0077: contract - ownership/admin transfer reachable by a non-owner

## Question
Given the account is registered and still holds a non-zero balance, can an unprivileged attacker, entering through the contract's own public entrypoint called by any account, reach `ft_transfer` in `contracts/poa/token/src/contract.rs` from an account that is not the current owner/admin, or make the transfer land on an attacker-chosen account, breaking the invariant `the account that can transfer ownership == the current owner at the time of the call` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/poa/token/src/contract.rs](contracts/poa/token/src/contract.rs) - `ft_transfer` (cross-check `storage_deposit` in the same file)
- Entrypoint: the contract's own public entrypoint called by any account
- Attacker controls: every argument of the call and the calling account id
- Exploit idea: Probe the guard on the transfer entrypoint, the two-step vs one-step design, and any path where the pending owner is attacker-settable. Set-up: the account is registered and still holds a non-zero balance.
- Invariant to test: the account that can transfer ownership == the current owner at the time of the call
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Call `ft_transfer` from a non-owner; assert rejection.
