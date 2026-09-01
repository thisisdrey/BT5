# Q1143: client - ownership/admin transfer reachable by a non-owner (4)

## Question
Given the account is registered and still holds a non-zero balance, can an unprivileged attacker, entering through a receiver or callee contract the attacker deployed, invoked during a transfer callback, reach `oa_set_code` in `contracts/outlayer/app/src/client.rs` from an account that is not the current owner/admin, or make the transfer land on an attacker-chosen account, breaking the invariant `the account that can transfer ownership == the current owner at the time of the call` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/outlayer/app/src/client.rs](contracts/outlayer/app/src/client.rs) - `oa_set_code` (cross-check `OaSetCodeArgs` in the same file)
- Entrypoint: a receiver or callee contract the attacker deployed, invoked during a transfer callback
- Attacker controls: the callee's return value, panics, and gas consumption
- Exploit idea: Probe the guard on the transfer entrypoint, the two-step vs one-step design, and any path where the pending owner is attacker-settable. Set-up: the account is registered and still holds a non-zero balance.
- Invariant to test: the account that can transfer ownership == the current owner at the time of the call
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Call `oa_set_code` from a non-owner; assert rejection.
