# Q4169: state_init - state_init (NEP-616) deployment changes the callee identity (13)

## Question
Given the named receiver account does not exist on chain, can an unprivileged attacker, entering through `mt_transfer_call` / `mt_batch_transfer_call` to a receiver contract the attacker deployed, use the `state_init` field reached through `estimate_gas` in `crates/near/promise/src/actions/state_init.rs` to deploy code at the deterministic account id right before the call, so the contract that receives `on_auth`/`mt_on_transfer` is not the one the signer expected, breaking the invariant `the code executing at the `state_init` target == the code the derived `AccountId` commits to` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [crates/near/promise/src/actions/state_init.rs](crates/near/promise/src/actions/state_init.rs) - `estimate_gas` (cross-check `deposit` in the same file)
- Entrypoint: `mt_transfer_call` / `mt_batch_transfer_call` to a receiver contract the attacker deployed
- Attacker controls: `receiver_id`, `token_ids`, `amounts`, `memo`, `msg`, and the receiver's return value
- Exploit idea: The derived id commits to the global contract id plus initial storage; probe whether the same id can be reached with different code, or whether an existing account is silently reused. Set-up: the named receiver account does not exist on chain.
- Invariant to test: the code executing at the `state_init` target == the code the derived `AccountId` commits to
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Derive an id, deploy independently, then send a `state_init` call; assert the pre-existing code is not silently used.
