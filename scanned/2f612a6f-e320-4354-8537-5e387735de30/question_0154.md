# Q0154: event - ownership/admin transfer reachable by a non-owner

## Question
Given the account is registered and still holds a non-zero balance, can an unprivileged attacker, entering through the contract's own public entrypoint called by any account, reach `Event` in `contracts/treasury-logger/src/event.rs` from an account that is not the current owner/admin, or make the transfer land on an attacker-chosen account, breaking the invariant `the account that can transfer ownership == the current owner at the time of the call` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/treasury-logger/src/event.rs](contracts/treasury-logger/src/event.rs) - `Event`
- Entrypoint: the contract's own public entrypoint called by any account
- Attacker controls: every argument of the call and the calling account id
- Exploit idea: Probe the guard on the transfer entrypoint, the two-step vs one-step design, and any path where the pending owner is attacker-settable. Set-up: the account is registered and still holds a non-zero balance.
- Invariant to test: the account that can transfer ownership == the current owner at the time of the call
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Call `Event` from a non-owner; assert rejection.
