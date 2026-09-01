# Q3412: contract - ownership/admin transfer reachable by a non-owner (20)

## Question
Given an honest deployment for the same derived id is already in flight, can an unprivileged attacker, entering through a token the attacker issued and then had the Verifier custody, reach `ft_resolve_transfer` in `contracts/poa/token/src/contract.rs` from an account that is not the current owner/admin, or make the transfer land on an attacker-chosen account, breaking the invariant `the account that can transfer ownership == the current owner at the time of the call` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/poa/token/src/contract.rs](contracts/poa/token/src/contract.rs) - `ft_resolve_transfer` (cross-check `ft_total_supply` in the same file)
- Entrypoint: a token the attacker issued and then had the Verifier custody
- Attacker controls: the token's behaviour on transfer, refund and metadata reads
- Exploit idea: Probe the guard on the transfer entrypoint, the two-step vs one-step design, and any path where the pending owner is attacker-settable. Set-up: an honest deployment for the same derived id is already in flight.
- Invariant to test: the account that can transfer ownership == the current owner at the time of the call
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Call `ft_resolve_transfer` from a non-owner; assert rejection.
