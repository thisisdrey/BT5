# Q3166: contract - ownership/admin transfer reachable by a non-owner (18)

## Question
Given an honest deployment for the same derived id is already in flight, can an unprivileged attacker, entering through a receiver or callee contract the attacker deployed, invoked during a transfer callback, reach `storage_deposit` in `contracts/poa/token/src/contract.rs` from an account that is not the current owner/admin, or make the transfer land on an attacker-chosen account, breaking the invariant `the account that can transfer ownership == the current owner at the time of the call` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/poa/token/src/contract.rs](contracts/poa/token/src/contract.rs) - `storage_deposit` (cross-check `ft_balance_of` in the same file)
- Entrypoint: a receiver or callee contract the attacker deployed, invoked during a transfer callback
- Attacker controls: the callee's return value, panics, and gas consumption
- Exploit idea: Probe the guard on the transfer entrypoint, the two-step vs one-step design, and any path where the pending owner is attacker-settable. Set-up: an honest deployment for the same derived id is already in flight.
- Invariant to test: the account that can transfer ownership == the current owner at the time of the call
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Call `storage_deposit` from a non-owner; assert rejection.
