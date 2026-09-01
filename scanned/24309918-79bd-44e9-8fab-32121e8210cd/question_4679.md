# Q4679: auth_call - state_init (NEP-616) deployment changes the callee identity (6)

## Question
Given the receiver accepts the assets and then panics, can an unprivileged attacker, entering through `ft_withdraw` / `nft_withdraw` / `mt_withdraw` called directly by an account with `auth_by_predecessor_id` enabled, use the `state_init` field reached through `DO_AUTH_CALL_MIN_GAS` in `contracts/defuse/src/contract/intents/auth_call.rs` to deploy code at the deterministic account id right before the call, so the contract that receives `on_auth`/`mt_on_transfer` is not the one the signer expected, breaking the invariant `the code executing at the `state_init` target == the code the derived `AccountId` commits to` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/defuse/src/contract/intents/auth_call.rs](contracts/defuse/src/contract/intents/auth_call.rs) - `DO_AUTH_CALL_MIN_GAS` (cross-check `STATE_INIT_GAS` in the same file)
- Entrypoint: `ft_withdraw` / `nft_withdraw` / `mt_withdraw` called directly by an account with `auth_by_predecessor_id` enabled
- Attacker controls: `token`, `receiver_id`, `amount`, `memo`, `msg`, `storage_deposit` and `min_gas`
- Exploit idea: The derived id commits to the global contract id plus initial storage; probe whether the same id can be reached with different code, or whether an existing account is silently reused. Set-up: the receiver accepts the assets and then panics.
- Invariant to test: the code executing at the `state_init` target == the code the derived `AccountId` commits to
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Derive an id, deploy independently, then send a `state_init` call; assert the pre-existing code is not silently used.
