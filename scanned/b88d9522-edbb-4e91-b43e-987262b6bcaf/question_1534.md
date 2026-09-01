# Q1534: contract - ownership/admin transfer reachable by a non-owner (16)

## Question
Given the account is registered and still holds a non-zero balance, can an unprivileged attacker, entering through a token the attacker issued and then had the Verifier custody, reach `oa_admin_id` in `contracts/outlayer/app/src/contract.rs` from an account that is not the current owner/admin, or make the transfer land on an attacker-chosen account, breaking the invariant `the account that can transfer ownership == the current owner at the time of the call` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/outlayer/app/src/contract.rs](contracts/outlayer/app/src/contract.rs) - `oa_admin_id` (cross-check `oa_code_url` in the same file)
- Entrypoint: a token the attacker issued and then had the Verifier custody
- Attacker controls: the token's behaviour on transfer, refund and metadata reads
- Exploit idea: Probe the guard on the transfer entrypoint, the two-step vs one-step design, and any path where the pending owner is attacker-settable. Set-up: the account is registered and still holds a non-zero balance.
- Invariant to test: the account that can transfer ownership == the current owner at the time of the call
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Call `oa_admin_id` from a non-owner; assert rejection.
