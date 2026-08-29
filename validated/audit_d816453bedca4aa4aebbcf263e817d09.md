#No vulnerability found for this question.

The premise itself is not realizable by an unprivileged attacker. `all_proposals` in `EpochInfoAggregator` is populated exclusively from `block_info.proposals_iter()`, which are validator proposals produced by actual `Stake` action execution and carried in chunk headers / block header `prev_validator_proposals` [1](#0-0) . There is no path for an ordinary transfer, function call, or meta-transaction from an unrelated account to inject an entry into this map — the entry only exists if that specific `account_id` itself submits a `Stake` action, which is exactly the "Unstake action" the question claims is missing.

Additionally, `stake_change` is not a stale, cross-epoch-persistent map as hypothesized: it is freshly rebuilt every epoch by `apply_epoch_update_to_proposals`, sourced only from `prev_epoch_info.validators_iter()` plus current-epoch proposals [2](#0-1) . Once an account is kicked out, it drops out of `validators_iter()` in the following epoch and cannot reappear in `stake_change` unless it submits a genuine new proposal — so there is no "stale zero-stake ValidatorStake" that a colliding account could ride on. The `Unstaked` kickout logic at `collect_blocks_info` (lib.rs:926-937) also only inspects `all_proposals`/`next_epoch_info.stake_change()`, both of which are scoped to real stake-action activity [3](#0-2) .

Finally, NEAR `AccountId`s are globally unique and permanently bound to whoever controls their access keys; there is no mechanism by which an "unrelated unprivileged sender" could acquire the same `account_id` as a former validator to trigger this path — the question itself concedes this ("NEAR account ids are permanent, so this tests for ID reuse impossibility"). Locked-balance accounting also isn't performed inside epoch-manager's `stake_change`/`validator_kickout` structures; it is computed via `compute_stake_return_info` purely from real proposal/validator data, not injectable by unprivileged transactions [4](#0-3) .

### Citations

**File:** chain/epoch-manager/src/epoch_info_aggregator.rs (L200-203)
```rust
        // Step 4: update proposals
        for proposal in block_info.proposals_iter() {
            self.all_proposals.entry(proposal.account_id().clone()).or_insert(proposal);
        }
```

**File:** chain/epoch-manager/src/validator_selection.rs (L290-326)
```rust
fn apply_epoch_update_to_proposals(
    proposals: Vec<ValidatorStake>,
    prev_epoch_info: &EpochInfo,
    validator_reward: &HashMap<AccountId, Balance>,
    validator_kickout: &HashMap<AccountId, ValidatorKickoutReason>,
    stake_change: &mut BTreeMap<AccountId, Balance>,
) -> HashMap<AccountId, ValidatorStake> {
    let mut proposals_by_account = HashMap::new();
    for p in proposals {
        let account_id = p.account_id();
        if validator_kickout.contains_key(account_id) {
            let account_id = p.take_account_id();
            stake_change.insert(account_id, Balance::ZERO);
        } else if let Some(ValidatorKickoutReason::ProtocolVersionTooOld { .. }) =
            prev_epoch_info.validator_kickout().get(account_id)
        {
            // If the validator was kicked out because of an old protocol version in T-1,
            // it is not allowed back in T.
            continue;
        } else {
            stake_change.insert(account_id.clone(), p.stake());
            proposals_by_account.insert(account_id.clone(), p);
        }
    }

    for r in prev_epoch_info.validators_iter() {
        let account_id = r.account_id().clone();
        if validator_kickout.contains_key(&account_id) {
            stake_change.insert(account_id, Balance::ZERO);
            continue;
        }
        let p = proposals_by_account.entry(account_id).or_insert(r);
        if let Some(reward) = validator_reward.get(p.account_id()) {
            *p.stake_mut() = p.stake().checked_add(*reward).unwrap();
        }
        stake_change.insert(p.account_id().clone(), p.stake());
    }
```

**File:** chain/epoch-manager/src/lib.rs (L926-937)
```rust
        // Kickout unstaked validators.
        for (account_id, proposal) in all_proposals {
            if proposal.stake().is_zero()
                && !next_epoch_info
                    .stake_change()
                    .get(&account_id)
                    .unwrap_or(&Balance::ZERO)
                    .is_zero()
            {
                validator_kickout.insert(account_id.clone(), ValidatorKickoutReason::Unstaked);
            }
            proposals.push(proposal.clone());
```

**File:** chain/epoch-manager/src/lib.rs (L1648-1687)
```rust
    pub fn compute_stake_return_info(
        &self,
        last_block_hash: &CryptoHash,
    ) -> Result<(HashMap<AccountId, Balance>, HashMap<AccountId, Balance>), EpochError> {
        let next_next_epoch_id = EpochId(*last_block_hash);
        let validator_reward = self.get_epoch_info(&next_next_epoch_id)?.validator_reward().clone();

        let next_epoch_id = self.get_next_epoch_id(last_block_hash)?;
        let epoch_id = self.get_epoch_id(last_block_hash)?;
        tracing::debug!(target: "epoch_manager",
            epoch_id = ?next_next_epoch_id,
            prev_epoch_id = ?next_epoch_id,
            prev_prev_epoch_id= ?epoch_id,
        );

        // Since stake changes for epoch T are stored in epoch info for T+2, the one stored by epoch_id
        // is the prev_prev_stake_change.
        let prev_prev_stake_change = self.get_epoch_info(&epoch_id)?.stake_change().clone();
        let prev_stake_change = self.get_epoch_info(&next_epoch_id)?.stake_change().clone();
        let stake_change = self.get_epoch_info(&next_next_epoch_id)?.stake_change().clone();
        tracing::debug!(target: "epoch_manager",
            ?prev_prev_stake_change,
            ?prev_stake_change,
            ?stake_change,
        );
        let all_stake_changes =
            prev_prev_stake_change.iter().chain(&prev_stake_change).chain(&stake_change);
        let all_keys: HashSet<&AccountId> = all_stake_changes.map(|(key, _)| key).collect();

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
```
