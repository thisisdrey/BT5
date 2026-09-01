# Q0949: contract - ownership/admin transfer reachable by a non-owner (10)

## Question
Given the account is registered and still holds a non-zero balance, can an unprivileged attacker, entering through the contract's own public entrypoint called by any account, reach `set_code` in `contracts/outlayer/app/src/contract.rs` from an account that is not the current owner/admin, or make the transfer land on an attacker-chosen account, breaking the invariant `the account that can transfer ownership == the current owner at the time of the call` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/outlayer/app/src/contract.rs](contracts/outlayer/app/src/contract.rs) - `set_code` (cross-check `is_admin` in the same file)
- Entrypoint: the contract's own public entrypoint called by any account
- Attacker controls: every argument of the call and the calling account id
- Exploit idea: Probe the guard on the transfer entrypoint, the two-step vs one-step design, and any path where the pending owner is attacker-settable. Set-up: the account is registered and still holds a non-zero balance.
- Invariant to test: the account that can transfer ownership == the current owner at the time of the call
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Call `set_code` from a non-owner; assert rejection.
