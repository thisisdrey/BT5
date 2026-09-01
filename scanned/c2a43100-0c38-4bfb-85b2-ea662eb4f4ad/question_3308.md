# Q3308: state_init - gas starvation of the resolver callback (11)

## Question
Given the named receiver account does not exist on chain, can an unprivileged attacker, entering through `ft_withdraw` / `nft_withdraw` / `mt_withdraw` called directly by an account with `auth_by_predecessor_id` enabled, choose `min_gas` / `state_init` / batch size at the entrypoint so the `DeterministicStateInit` callback in `crates/near/promise/src/actions/state_init.rs` runs out of gas after the debit, leaving the balance burned with no refund, breaking the invariant `the resolver callback always executes with enough gas to restore the debited balance` and leading to permanent freezing of user funds (unrecoverable without a privileged action)?

## Target
- File/function: [crates/near/promise/src/actions/state_init.rs](crates/near/promise/src/actions/state_init.rs) - `DeterministicStateInit` (cross-check `deposit` in the same file)
- Entrypoint: `ft_withdraw` / `nft_withdraw` / `mt_withdraw` called directly by an account with `auth_by_predecessor_id` enabled
- Attacker controls: `token`, `receiver_id`, `amount`, `memo`, `msg`, `storage_deposit` and `min_gas`
- Exploit idea: The floors (`FT_TRANSFER_CALL_GAS_MIN`, `MT_BATCH_TRANSFER_GAS_MIN`, `AuthCall::MIN_GAS_DEFAULT`, `STATE_INIT_GAS`) plus `with_unused_gas_weight(0)` are the only protection; probe whether a caller-chosen value passes the floor but starves the resolve step. Set-up: the named receiver account does not exist on chain.
- Invariant to test: the resolver callback always executes with enough gas to restore the debited balance
- Expected Immunefi impact: Critical - Permanent freezing of user funds (unrecoverable without a privileged action)
- Fast validation: Sweep `min_gas` and token-count values; assert the resolver completes for every value the entrypoint accepts.
