### Title
Protocol treasury account self-deletion causes a fatal, unrecoverable `StorageInconsistentState` error during validator-reward distribution - (File: `runtime/runtime/src/lib.rs`)

### Summary
`update_validator_accounts` unconditionally assumes the `protocol_treasury_account_id` designated in genesis config still exists when it is due an epoch reward. If the holder of that account deletes it with an ordinary `DeleteAccount` action, the very next epoch-boundary reward distribution hits an `.ok_or_else(StorageError::StorageInconsistentState(...))` and aborts, mirroring the reported `vault.move` pattern where a "creator"/designated-role account can legitimately withdraw/delete its own position while other code unconditionally assumes that position still exists.

### Finding Description
`GenesisConfig::protocol_treasury_account` designates an ordinary, non-privileged account id (default `"near"`, e.g. `treasury.near` on mainnet) as the recipient of the protocol's epoch reward share [1](#0-0) . Nothing in the protocol prevents the owner of that account from issuing a completely normal `DeleteAccount` transaction against their own account — `action_delete_account` has no special-case exemption for the treasury account, it only checks storage size / gas-key-burn limits and unconditionally removes the account and sets `*account = None` [2](#0-1) .

At the next epoch boundary, `update_validator_accounts` distributes the protocol reward to `protocol_treasury_account_id`. When the account is not a staking validator itself, the code unconditionally does:

```
let mut account = get_account(state_update, account_id)?.ok_or_else(|| {
    StorageError::StorageInconsistentState(format!(
        "Protocol treasury account {} is not found",
        account_id
    ))
})?;
``` [3](#0-2) 

This is functionally identical to the reported bug class: a function assumes a designated-role account's on-chain state ("position"/`Account`) always exists, and unconditionally does an operation that aborts (`ok_or_else`/`borrow_mut` panic-equivalent) instead of checking for and gracefully handling its absence — exactly as described for `vault.move`'s `withdraw_preload()`/`fill_withdrawal_requests()` unconditionally borrowing `vault.creator`'s position.

`StorageInconsistentState` is nearcore's convention for a fatal, "should never happen" invariant violation — the same function marks an analogous condition explicitly as `"FATAL: staking invariant does not hold"` just a few lines above [4](#0-3) . Once this branch is entered, `update_validator_accounts` returns `Err(RuntimeError::StorageError(...))` and never calls `state_update.commit(...)`, aborting the epoch-transition portion of `apply` for that shard [5](#0-4) .

### Impact Explanation
Because block/state-transition execution is deterministic and every honest validator processes the same genesis config and the same deleted-account state, all validators (and any full node) tracking that shard will hit the identical fatal `StorageInconsistentState` error at the same epoch boundary. This is not a per-node malicious-input crash but a deterministic, protocol-level dead end: the shard can no longer progress past that epoch transition without an emergency protocol/genesis patch, which is consistent with the "shard-halting panic" acceptance criterion. The condition is also effectively permanent — the treasury account cannot be un-deleted, so every subsequent epoch boundary would hit the same fatal path.

### Likelihood Explanation
Triggering this requires only an ordinary, unprivileged `DeleteAccount` transaction signed by whoever holds the access key to the configured `protocol_treasury_account` — no protocol-level admin permission, no cross-shard trickery, and no validator/staking status is needed (the treasury account need not be a validator). The only precondition is that the account is small enough to satisfy `DeleteAccountWithLargeState`'s size cap and (if it holds gas keys) below the gas-key burn cap, both of which are trivially satisfiable for a simple treasury account holding just a balance. Whether this is exploitable by a fully "external, unprivileged attacker" hinges only on whether the attacker controls (or can trick the holder of) the treasury account's private key — if that key is compromised or if the operator ever performs key rotation via delete+recreate without realizing the reward-distribution dependency, this fires deterministically at the very next epoch boundary.

### Recommendation
In `update_validator_accounts` (`runtime/runtime/src/lib.rs:1728-1755`), do not treat a missing protocol-treasury account as a fatal invariant violation. Mirror the pattern already used for validator stake_info accounts (`if let Some(mut account) = get_account(...) { ... } else { ... }`, `lib.rs:1669-1725`): if the account no longer exists, skip crediting the reward gracefully (optionally logging a warning, and/or redirecting/burning the reward instead of crashing) rather than returning `StorageError::StorageInconsistentState`. Additionally consider whether `action_delete_account` should reject deletion of an account that is the currently configured `protocol_treasury_account_id`, analogous to how manager-created vaults should check the creator's position before unconditionally distributing fees to it.

### Proof of Concept
1. Deploy/observe a chain where `GenesisConfig::protocol_treasury_account` is set to a normal account (e.g., `treasury.near`), as in the default and mainnet configs [6](#0-5) .
2. The holder of `treasury.near`'s access key submits a standard transaction with a single `Action::DeleteAccount(DeleteAccountAction { beneficiary_id: <any_account> })`, exactly as exercised in `test_delete_account_after_unstake`/`meta_tx_delete_account` [7](#0-6) . `action_delete_account` accepts this unconditionally and removes the account [2](#0-1) .
3. Wait for the next epoch boundary. `EpochManager`/`RewardCalculator::calculate_reward` still inserts a reward entry for `protocol_treasury_account` regardless of whether the account exists [8](#0-7) .
4. When the runtime applies the `ValidatorAccountsUpdate` for that epoch, `update_validator_accounts` calls `get_account(state_update, account_id)?.ok_or_else(|| StorageError::StorageInconsistentState(...))?` and returns a fatal error because the account no longer exists [3](#0-2) , aborting shard state application for that block on every node.

### Citations

**File:** core/chain-configs/src/genesis_config.rs (L179-181)
```rust
    /// Protocol treasury account
    #[default("near".parse().unwrap())]
    pub protocol_treasury_account: AccountId,
```

**File:** runtime/runtime/src/actions.rs (L364-389)
```rust
    // We use current amount as a pay out to beneficiary.
    let account_balance = account_ref.amount();
    if account_balance > Balance::ZERO {
        result
            .new_receipts
            .push(Receipt::new_balance_refund(&delete_account.beneficiary_id, account_balance));
    }
    let remove_result = remove_account(state_update, account_id)?;
    result.tokens_burnt =
        result.tokens_burnt.checked_add(gas_key_balance_to_burn).ok_or_else(|| {
            StorageError::StorageInconsistentState("tokens_burnt overflow".to_string())
        })?;
    if remove_result.gas_key_nonce_count > 0 {
        let compute = storage_removes_compute(
            &config.wasm_config.ext_costs,
            remove_result.gas_key_nonce_count,
            remove_result.gas_key_nonce_total_key_bytes,
            AccessKey::NONCE_VALUE_LEN * remove_result.gas_key_nonce_count,
        );
        result.compute_usage = safe_add_compute(result.compute_usage, compute).map_err(|_| {
            StorageError::StorageInconsistentState("compute_usage overflow".to_string())
        })?;
    }
    *actor_id = receipt.predecessor_id().clone();
    *account = None;
    Ok(())
```

**File:** runtime/runtime/src/lib.rs (L1663-1759)
```rust
    fn update_validator_accounts(
        &self,
        state_update: &mut TrieUpdate,
        validator_accounts_update: &ValidatorAccountsUpdate,
    ) -> Result<(), RuntimeError> {
        for (account_id, max_of_stakes) in &validator_accounts_update.stake_info {
            if let Some(mut account) = get_account(state_update, account_id)? {
                if let Some(reward) = validator_accounts_update.validator_rewards.get(account_id) {
                    tracing::debug!(target: "runtime", %account_id, %reward, locked = %account.locked(), "account adding reward to stake");
                    let locked = account.locked().checked_add(*reward).ok_or_else(|| {
                        RuntimeError::UnexpectedIntegerOverflow("update_validator_accounts".into())
                    })?;
                    account.set_locked(locked).or_inconsistent_state(account_id)?;
                }

                tracing::debug!(target: "runtime",
                       %account_id, locked = %account.locked(), %max_of_stakes,
                       "account stake and max of stakes"
                );
                if account.locked() < *max_of_stakes {
                    return Err(StorageError::StorageInconsistentState(format!(
                        "FATAL: staking invariant does not hold. \
                         Account stake {} is less than maximum of stakes {} in the past three epochs",
                        account.locked(),
                        max_of_stakes)).into());
                }
                let last_proposal = *validator_accounts_update
                    .last_proposals
                    .get(account_id)
                    .unwrap_or(&Balance::ZERO);
                let return_stake = account
                    .locked()
                    .checked_sub(max(*max_of_stakes, last_proposal))
                    .ok_or_else(|| {
                        RuntimeError::UnexpectedIntegerOverflow(
                            "update_validator_accounts - return stake".into(),
                        )
                    })?;
                tracing::debug!(target: "runtime", %account_id, %return_stake, "account return stake");
                let locked = account.locked().checked_sub(return_stake).ok_or_else(|| {
                    RuntimeError::UnexpectedIntegerOverflow(
                        "update_validator_accounts - set_locked".into(),
                    )
                })?;
                account.set_locked(locked).or_inconsistent_state(account_id)?;
                account.set_amount(account.amount().checked_add(return_stake).ok_or_else(
                    || {
                        RuntimeError::UnexpectedIntegerOverflow(
                            "update_validator_accounts - set_amount".into(),
                        )
                    },
                )?);

                set_account(state_update, account_id.clone(), &account);
            } else if *max_of_stakes > Balance::ZERO {
                // if max_of_stakes > 0, it means that the account must have locked balance
                // and therefore must exist
                return Err(StorageError::StorageInconsistentState(format!(
                    "Account {} with max of stakes {} is not found",
                    account_id, max_of_stakes
                ))
                .into());
            }
        }

        if let Some(account_id) = &validator_accounts_update.protocol_treasury_account_id {
            // If protocol treasury stakes, then the rewards was already distributed above.
            if !validator_accounts_update.stake_info.contains_key(account_id) {
                let mut account = get_account(state_update, account_id)?.ok_or_else(|| {
                    StorageError::StorageInconsistentState(format!(
                        "Protocol treasury account {} is not found",
                        account_id
                    ))
                })?;
                let treasury_reward = *validator_accounts_update
                    .validator_rewards
                    .get(account_id)
                    .ok_or_else(|| {
                        StorageError::StorageInconsistentState(format!(
                            "Validator reward for the protocol treasury account {} is not found",
                            account_id
                        ))
                    })?;
                account.set_amount(account.amount().checked_add(treasury_reward).ok_or_else(
                    || {
                        RuntimeError::UnexpectedIntegerOverflow(
                            "update_validator_accounts - treasure_reward".into(),
                        )
                    },
                )?);
                set_account(state_update, account_id.clone(), &account);
            }
        }
        state_update.commit(StateChangeCause::ValidatorAccountsUpdate);

        Ok(())
    }
```

**File:** utils/mainnet-res/res/mainnet_genesis.json (L243-246)
```json
  "total_supply": "999999999792372916156395166000000",
  "num_blocks_per_year": 31536000,
  "protocol_treasury_account": "treasury.near",
  "fishermen_threshold": "340282366920938463463374607431768211455",
```

**File:** chain/chain/src/runtime/tests.rs (L1375-1386)
```rust
    let delete_account_transaction = SignedTransaction::from_actions(
        4,
        signers[1].get_account_id(),
        signers[1].get_account_id(),
        &signers[1],
        vec![Action::DeleteAccount(DeleteAccountAction {
            beneficiary_id: signers[0].get_account_id(),
        })],
        // runtime does not validate block history
        CryptoHash::default(),
    );
    env.step_default(vec![delete_account_transaction]);
```

**File:** chain/epoch-manager/src/reward_calculator.rs (L78-84)
```rust
        let epoch_protocol_treasury = Balance::from_yoctonear(
            (U256::from(epoch_total_reward.as_yoctonear())
                * U256::from(*protocol_reward_rate.numer() as u64)
                / U256::from(*protocol_reward_rate.denom() as u64))
            .as_u128(),
        );
        res.insert(self.protocol_treasury_account.clone(), epoch_protocol_treasury);
```
