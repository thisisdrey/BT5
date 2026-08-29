### Title
Protocol treasury reward is silently dropped from spendable balance when the treasury account is itself a staked validator - (`chain/epoch-manager/src/reward_calculator.rs`)

### Summary
`RewardCalculator::calculate_reward` seeds the rewards map with `epoch_protocol_treasury` keyed by `protocol_treasury_account`, then iterates `validator_block_chunk_stats` and unconditionally overwrites `res[account_id] = reward` for every validator, including the treasury account if it is itself a staked validator. This silently discards the treasury's protocol-reward share from the map that is later used to credit on-chain balances, even though the discarded amount is still counted toward `total_supply`.

### Finding Description
In `chain/epoch-manager/src/reward_calculator.rs:84`, the treasury entry is inserted first: `res.insert(self.protocol_treasury_account.clone(), epoch_protocol_treasury)`. Then the per-validator loop at lines 94–144 does `res.insert(account_id, reward)` (line 142) for every account in `validator_block_chunk_stats` with no check for whether `account_id == self.protocol_treasury_account`. If the treasury account is a validator that is staked and producing blocks/chunks in that epoch, its map entry is overwritten from `epoch_protocol_treasury` to just its own validator `reward`, losing the treasury component from the map entry.

Critically, the *scalar* `epoch_actual_reward` (returned as `minted_amount`, used to bump `total_supply`, `core/primitives/src/block.rs:193`) is computed independently and correctly: it starts at `epoch_protocol_treasury` (line 90) and adds every validator's `reward` including the treasury-as-validator's own reward (line 143). So `total_supply` grows by the full, correct sum.

The corrupted map, however, is what actually reaches account balances. It is stored unmodified as `EpochInfo::validator_reward` via `proposals_to_epoch_info` (`chain/epoch-manager/src/lib.rs:1206-1213`), later retrieved by `compute_stake_return_info` (`chain/epoch-manager/src/lib.rs:1687`, returned as-is), and consumed in `chain/chain/src/runtime/mod.rs:242-273` to build `ValidatorAccountsUpdate { validator_rewards, stake_info, protocol_treasury_account_id, .. }`.

In `Runtime::update_validator_accounts` (`runtime/runtime/src/lib.rs:1663-1759`):
- For accounts in `stake_info` (i.e., staked validators), line 1670: `if let Some(reward) = validator_accounts_update.validator_rewards.get(account_id) { account.set_locked(locked + reward) }` — this credits only the (already-overwritten) map value, i.e., the treasury's own validator reward, not `epoch_protocol_treasury + reward`.
- The special treasury-crediting branch at lines 1728-1754 is explicitly skipped when the treasury is a validator: `if !validator_accounts_update.stake_info.contains_key(account_id)`. The comment at line 1729 ("If protocol treasury stakes, then the rewards was already distributed above") is *incorrect* in this scenario, because the map value used above was already clobbered by the bug in `calculate_reward` and no longer contains `epoch_protocol_treasury`.

Net effect: `epoch_protocol_treasury` yoctoNEAR are minted into `total_supply` every epoch but never credited to any account's `locked`/`amount` balance — a real, on-chain, per-epoch loss of protocol treasury funds, and the loss is permanent (repeats every epoch as long as the treasury account remains staked).

No existing checks (signature, nonce, access-key, gas, storage-staking) are relevant here because this is a pure epoch-transition/runtime accounting bug, not something gated by a specific attacker transaction — it triggers automatically whenever the treasury account is configured/kept as an active validator.

### Impact Explanation
This is a token-inflation/loss defect: NEAR is minted (counted in `total_supply`) but the treasury's share becomes permanently unbacked by any spendable balance — value is created on the ledger's accounting side but destroyed on the account-balance side. This falls under "token inflation or loss" per the bounty categories. The scoped impact matches exactly what the question describes: the treasury account's real balance increases only by its validator `reward`, not `reward + epoch_protocol_treasury`, while `total_supply` still increases by the full sum, breaking value conservation between minted supply and where those funds actually reside.

### Likelihood Explanation
This requires no attacker action at all — it is a protocol-level/network-configuration precondition (the `protocol_treasury_account` being enrolled as an active staked validator) rather than something exploitable by an unprivileged client. It reproduces deterministically on every epoch boundary as long as that precondition holds, with 100% repeatability in a test-loop/integration environment. On current mainnet, the treasury account is not staked, so the bug is latent, but the code path is reachable and would trigger immediately were the treasury account ever staked. Because this scenario is not attacker-triggerable (no ordinary client action can force the treasury to become a validator), it falls outside the "unprivileged attacker" threat model specified in the rules — it is a latent protocol-invariant bug rather than an externally exploitable vulnerability. Per the rules ("misconfiguration-only paths" are excluded, and "attacker is unprivileged only"), this does not qualify as a valid finding under the stated threat model, since there's no reachable transaction sequence an ordinary funded account/contract deployer/meta-tx relayer can execute to trigger or benefit from it.

### Recommendation
In `RewardCalculator::calculate_reward`, when inserting/overwriting a validator's reward into `res`, check if `account_id == self.protocol_treasury_account` and, if so, add the validator reward to the existing treasury entry (`res.entry(account_id).and_modify(|r| *r = r.checked_add(reward).unwrap()).or_insert(reward)`) instead of overwriting it. Correspondingly, `update_validator_accounts` in `runtime/runtime/src/lib.rs` should not assume the "stakes ⇒ already distributed above" invariant unless the map is guaranteed to combine both components.

### Proof of Concept
Test-loop / integration test plan (in `chain/epoch-manager/src/reward_calculator.rs` unit-test style, extended to a runtime/test-loop integration test):
1. Configure `protocol_treasury_account` (e.g., `"near"`) to also appear as a validator in `validator_stake` and `validator_block_chunk_stats` with full uptime.
2. Call `calculate_reward(...)` and assert `result.0.get("near") == Some(validator_reward_for_near)` (NOT `epoch_protocol_treasury + validator_reward_for_near`), while `result.1 (minted_amount) == epoch_protocol_treasury + sum_of_all_validator_rewards_including_near`.
3. In a full test-loop apply of an epoch-boundary block with "near" staked as validator, assert:
   - `total_supply` after the epoch increases by the full `minted_amount`.
   - The treasury account's on-chain balance (`locked` + `amount`) increases by only its validator `reward`, not `reward + epoch_protocol_treasury`.
   - This demonstrates `epoch_protocol_treasury` yoctoNEAR are minted into `total_supply` but never appear in any account balance, breaking value conservation. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** chain/epoch-manager/src/reward_calculator.rs (L78-90)
```rust
        let epoch_protocol_treasury = Balance::from_yoctonear(
            (U256::from(epoch_total_reward.as_yoctonear())
                * U256::from(*protocol_reward_rate.numer() as u64)
                / U256::from(*protocol_reward_rate.denom() as u64))
            .as_u128(),
        );
        res.insert(self.protocol_treasury_account.clone(), epoch_protocol_treasury);
        if num_validators == 0 {
            return (res, Balance::ZERO);
        }
        let epoch_validator_reward =
            epoch_total_reward.checked_sub(epoch_protocol_treasury).unwrap();
        let mut epoch_actual_reward = epoch_protocol_treasury;
```

**File:** chain/epoch-manager/src/reward_calculator.rs (L94-145)
```rust
        for (account_id, stats) in validator_block_chunk_stats {
            let production_ratio =
                get_validator_online_ratio(&stats, online_thresholds.endorsement_cutoff_threshold);
            let average_produced_numer = production_ratio.numer();
            let average_produced_denom = production_ratio.denom();

            let expected_blocks = stats.block_stats.expected;
            let expected_chunks = stats.chunk_stats.expected();
            let expected_endorsements = stats.chunk_stats.endorsement_stats().expected;

            let online_min_numer =
                U256::from(*online_thresholds.online_min_threshold.numer() as u64);
            let online_min_denom =
                U256::from(*online_thresholds.online_min_threshold.denom() as u64);
            // If average of produced blocks below online min threshold, validator gets 0 reward.
            let reward = if average_produced_numer * online_min_denom
                < online_min_numer * average_produced_denom
                || (expected_chunks == 0 && expected_blocks == 0 && expected_endorsements == 0)
            {
                Balance::ZERO
            } else {
                // cspell:ignore denum
                let stake = *validator_stake
                    .get(&account_id)
                    .unwrap_or_else(|| panic!("{} is not a validator", account_id));
                // Online reward multiplier is min(1., (uptime - online_threshold_min) / (online_threshold_max - online_threshold_min).
                let online_max_numer =
                    U256::from(*online_thresholds.online_max_threshold.numer() as u64);
                let online_max_denom =
                    U256::from(*online_thresholds.online_max_threshold.denom() as u64);
                let online_numer =
                    online_max_numer * online_min_denom - online_min_numer * online_max_denom;
                let mut uptime_numer = (average_produced_numer * online_min_denom
                    - online_min_numer * average_produced_denom)
                    * online_max_denom;
                let uptime_denum = online_numer * average_produced_denom;
                // Apply min between 1. and computed uptime.
                uptime_numer =
                    if uptime_numer > uptime_denum { uptime_denum } else { uptime_numer };
                Balance::from_yoctonear(
                    (U512::from(epoch_validator_reward.as_yoctonear())
                        * U512::from(uptime_numer)
                        * U512::from(stake.as_yoctonear())
                        / U512::from(uptime_denum)
                        / U512::from(total_stake.as_yoctonear()))
                    .as_u128(),
                )
            };
            res.insert(account_id, reward);
            epoch_actual_reward = epoch_actual_reward.checked_add(reward).unwrap();
        }
        (res, epoch_actual_reward)
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

**File:** chain/chain/src/runtime/mod.rs (L241-273)
```rust
            if epoch_manager.is_next_block_epoch_start(prev_block_hash)? {
                let (stake_info, validator_reward) =
                    epoch_manager.compute_stake_return_info(prev_block_hash)?;
                let stake_info = stake_info
                    .into_iter()
                    .filter(|(account_id, _)| {
                        shard_layout.account_id_to_shard_id(account_id) == shard_id
                    })
                    .collect();
                let validator_rewards = validator_reward
                    .into_iter()
                    .filter(|(account_id, _)| {
                        shard_layout.account_id_to_shard_id(account_id) == shard_id
                    })
                    .collect();
                let last_proposals = last_validator_proposals
                    .filter(|v| shard_layout.account_id_to_shard_id(v.account_id()) == shard_id)
                    .fold(HashMap::new(), |mut acc, v| {
                        let (account_id, stake) = v.account_and_stake();
                        acc.insert(account_id, stake);
                        acc
                    });
                Some(ValidatorAccountsUpdate {
                    stake_info,
                    validator_rewards,
                    last_proposals,
                    protocol_treasury_account_id: Some(
                        self.genesis_config.protocol_treasury_account.clone(),
                    )
                    .filter(|account_id| {
                        shard_layout.account_id_to_shard_id(account_id) == shard_id
                    }),
                })
```

**File:** chain/epoch-manager/src/lib.rs (L1206-1213)
```rust
        let next_next_epoch_info = match proposals_to_epoch_info(
            &next_next_epoch_config,
            rng_seed,
            &next_epoch_info,
            all_proposals,
            validator_kickout,
            validator_reward,
            minted_amount,
```

**File:** chain/epoch-manager/src/lib.rs (L1677-1688)
```rust
        let mut stake_info = HashMap::new();
        for account_id in all_keys {
            let new_stake = *stake_change.get(account_id).unwrap_or(&Balance::ZERO);
            let prev_stake = *prev_stake_change.get(account_id).unwrap_or(&Balance::ZERO);
            let prev_prev_stake = *prev_prev_stake_change.get(account_id).unwrap_or(&Balance::ZERO);
            let max_of_stakes =
                vec![prev_prev_stake, prev_stake, new_stake].into_iter().max().unwrap();
            stake_info.insert(account_id.clone(), max_of_stakes);
        }
        tracing::debug!(target: "epoch_manager", ?stake_info, ?validator_reward);
        Ok((stake_info, validator_reward))
    }
```
