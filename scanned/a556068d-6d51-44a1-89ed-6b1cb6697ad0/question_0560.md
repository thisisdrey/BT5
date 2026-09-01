# Q0560: state - ownership/admin transfer reachable by a non-owner (5)

## Question
Given the account is registered and still holds a non-zero balance, can an unprivileged attacker, entering through a race against an honest party's deployment or initialisation call, reach `with_code_hash` in `contracts/outlayer/app/src/state.rs` from an account that is not the current owner/admin, or make the transfer land on an attacker-chosen account, breaking the invariant `the account that can transfer ownership == the current owner at the time of the call` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/outlayer/app/src/state.rs](contracts/outlayer/app/src/state.rs) - `with_code_hash` (cross-check `as_storage` in the same file)
- Entrypoint: a race against an honest party's deployment or initialisation call
- Attacker controls: the timing and the arguments of the competing call
- Exploit idea: Probe the guard on the transfer entrypoint, the two-step vs one-step design, and any path where the pending owner is attacker-settable. Set-up: the account is registered and still holds a non-zero balance.
- Invariant to test: the account that can transfer ownership == the current owner at the time of the call
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Call `with_code_hash` from a non-owner; assert rejection.
