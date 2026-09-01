# Q5285: tokens - state_init (NEP-616) deployment changes the callee identity (3)

## Question
Given the receiver is a contract the attacker deployed that returns a crafted JSON value, can an unprivileged attacker, entering through `mt_transfer_call` / `mt_batch_transfer_call` to a receiver contract the attacker deployed, use the `state_init` field reached through `min_gas` in `contracts/defuse/core/src/intents/tokens.rs` to deploy code at the deterministic account id right before the call, so the contract that receives `on_auth`/`mt_on_transfer` is not the one the signer expected, breaking the invariant `the code executing at the `state_init` target == the code the derived `AccountId` commits to` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/defuse/core/src/intents/tokens.rs](contracts/defuse/core/src/intents/tokens.rs) - `min_gas` (cross-check `NFT_TRANSFER_CALL_GAS_DEFAULT` in the same file)
- Entrypoint: `mt_transfer_call` / `mt_batch_transfer_call` to a receiver contract the attacker deployed
- Attacker controls: `receiver_id`, `token_ids`, `amounts`, `memo`, `msg`, and the receiver's return value
- Exploit idea: The derived id commits to the global contract id plus initial storage; probe whether the same id can be reached with different code, or whether an existing account is silently reused. Set-up: the receiver is a contract the attacker deployed that returns a crafted JSON value.
- Invariant to test: the code executing at the `state_init` target == the code the derived `AccountId` commits to
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Derive an id, deploy independently, then send a `state_init` call; assert the pre-existing code is not silently used.
