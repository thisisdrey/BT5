# Q3289: contract - ownership/admin transfer reachable by a non-owner (19)

## Question
Given an honest deployment for the same derived id is already in flight, can an unprivileged attacker, entering through a race against an honest party's deployment or initialisation call, reach `ft_balance_of` in `contracts/poa/token/src/contract.rs` from an account that is not the current owner/admin, or make the transfer land on an attacker-chosen account, breaking the invariant `the account that can transfer ownership == the current owner at the time of the call` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/poa/token/src/contract.rs](contracts/poa/token/src/contract.rs) - `ft_balance_of` (cross-check `ft_resolve_transfer` in the same file)
- Entrypoint: a race against an honest party's deployment or initialisation call
- Attacker controls: the timing and the arguments of the competing call
- Exploit idea: Probe the guard on the transfer entrypoint, the two-step vs one-step design, and any path where the pending owner is attacker-settable. Set-up: an honest deployment for the same derived id is already in flight.
- Invariant to test: the account that can transfer ownership == the current owner at the time of the call
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Call `ft_balance_of` from a non-owner; assert rejection.
