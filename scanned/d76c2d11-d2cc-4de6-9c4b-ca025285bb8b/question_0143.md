# Q0143: error - ownership/admin transfer reachable by a non-owner

## Question
Given the account is registered and still holds a non-zero balance, can an unprivileged attacker, entering through the contract's own public entrypoint called by any account, reach `Error` in `contracts/global-deployer/src/error.rs` from an account that is not the current owner/admin, or make the transfer land on an attacker-chosen account, breaking the invariant `the account that can transfer ownership == the current owner at the time of the call` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/global-deployer/src/error.rs](contracts/global-deployer/src/error.rs) - `Error`
- Entrypoint: the contract's own public entrypoint called by any account
- Attacker controls: every argument of the call and the calling account id
- Exploit idea: Probe the guard on the transfer entrypoint, the two-step vs one-step design, and any path where the pending owner is attacker-settable. Set-up: the account is registered and still holds a non-zero balance.
- Invariant to test: the account that can transfer ownership == the current owner at the time of the call
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Call `Error` from a non-owner; assert rejection.
