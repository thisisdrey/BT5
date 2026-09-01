# Q0153: lib - ownership/admin transfer reachable by a non-owner (2)

## Question
Given the account is registered and still holds a non-zero balance, can an unprivileged attacker, entering through the contract's own public entrypoint called by any account, reach `WITHDRAW_MEMO_PREFIX` in `contracts/poa/token/src/lib.rs` from an account that is not the current owner/admin, or make the transfer land on an attacker-chosen account, breaking the invariant `the account that can transfer ownership == the current owner at the time of the call` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/poa/token/src/lib.rs](contracts/poa/token/src/lib.rs) - `WITHDRAW_MEMO_PREFIX` (cross-check `withdraw_to` in the same file)
- Entrypoint: the contract's own public entrypoint called by any account
- Attacker controls: every argument of the call and the calling account id
- Exploit idea: Probe the guard on the transfer entrypoint, the two-step vs one-step design, and any path where the pending owner is attacker-settable. Set-up: the account is registered and still holds a non-zero balance.
- Invariant to test: the account that can transfer ownership == the current owner at the time of the call
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Call `WITHDRAW_MEMO_PREFIX` from a non-owner; assert rejection.
