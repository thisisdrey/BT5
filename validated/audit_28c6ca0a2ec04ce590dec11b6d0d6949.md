### Title
Unprivileged vote on a succeeded proposal perpetually refreshes `RESOLVABLE_TIME_METADATA_KEY`, enabling DoS of governance proposal resolution — (`aptos-move/framework/aptos-framework/sources/voting.move`)

---

### Summary

`voting::vote()` unconditionally overwrites the `RESOLVABLE_TIME_METADATA_KEY` metadata field with `timestamp::now_seconds()` on **every** vote, including votes cast after a proposal has already reached its early-resolution threshold. `is_proposal_resolvable()` enforces a **strict** `timestamp::now_seconds() > resolvable_time` check. Any actor with remaining voting power on a succeeded proposal can submit a vote transaction in the same block as a resolution transaction, refreshing `resolvable_time` to the current block's timestamp and causing the resolution to abort with `ERESOLUTION_CANNOT_BE_ATOMIC`. By repeating this every block, the attacker can block resolution until the proposal expires.

---

### Finding Description

**Root cause — `voting::vote()` always updates `RESOLVABLE_TIME_METADATA_KEY`:** [1](#0-0) 

Every call to `vote()` writes `timestamp::now_seconds()` into the proposal's metadata under `RESOLVABLE_TIME_METADATA_KEY`, regardless of whether the proposal has already accumulated enough votes to be resolved early.

**Root cause — `is_proposal_resolvable()` uses strict `>`:** [2](#0-1) 

Resolution requires `now_seconds() > resolvable_time`. If a vote is cast in the same second as the resolution attempt, `resolvable_time` equals `now_seconds()`, and the strict inequality fails.

**Voting is explicitly permitted after early-resolution threshold is met:** [3](#0-2) 

The `vote()` function only checks that the voting period has not expired and the proposal is not yet resolved. It does **not** block votes once `can_be_resolved_early()` returns true. The code comment explicitly acknowledges this design.

**`aptos_governance::vote_internal()` has no guard against voting on a succeeded proposal:** [4](#0-3) 

`vote_internal()` only checks that the voter has remaining voting power (`> 0`) and that the proposal has not expired. It does not check whether the proposal state is `PROPOSAL_STATE_SUCCEEDED`.

**Aptos timestamp granularity enables same-second collision:** [5](#0-4) 

`now_seconds()` truncates microseconds to seconds. All transactions in the same block share the same block timestamp. A malicious validator (or any actor who can get their transaction included in the same block as the resolve transaction) can reliably trigger this collision.

---

### Impact Explanation

An attacker with any remaining voting power on a governance proposal that has already succeeded can:

1. Monitor the mempool for a `resolve()` or `resolve_proposal_v2()` transaction.
2. Submit a `partial_vote()` (or `vote()`) transaction targeting the same proposal in the same block.
3. The vote updates `resolvable_time` to the current block's second.
4. The resolution transaction aborts with `ERESOLUTION_CANNOT_BE_ATOMIC` (error code `0x30008`).
5. Repeat every block until the proposal's `expiration_secs` is reached, permanently preventing execution.

Since Aptos governance proposals control framework upgrades, parameter changes, and other critical network operations, permanently blocking resolution constitutes a material chain availability failure. A malicious validator has deterministic control over transaction ordering within their proposed block, making the attack reliable without any race condition.

---

### Likelihood Explanation

- Any stake pool holder (validator or delegator via `delegation_pool::vote()`) with remaining voting power on the target proposal can execute this attack.
- A malicious validator can guarantee the ordering within their own proposed block.
- With partial voting enabled, the attacker can spread their voting power across many transactions, sustaining the DoS for the entire voting duration.
- The cost is only gas; the attacker's stake is not consumed.

---

### Recommendation

1. **Stop accepting votes once the proposal can be resolved early.** Add a check in `voting::vote()`:
   ```move
   assert!(!can_be_resolved_early(proposal), error::invalid_state(EPROPOSAL_VOTING_ALREADY_ENDED));
   ```
   This is the cleanest fix and matches the intent of early resolution.

2. **Alternatively, freeze `resolvable_time` once the proposal first reaches the resolution threshold.** Only update `RESOLVABLE_TIME_METADATA_KEY` if the proposal has not yet reached `PROPOSAL_STATE_SUCCEEDED`.

3. **Do not use `>` (strict) for the resolution time check** if option 2 is chosen; the current strict inequality is necessary for the flashloan guard but becomes exploitable when `resolvable_time` can be refreshed after the proposal succeeds.

---

### Proof of Concept

```
1. Governance proposal P is created with early_resolution_vote_threshold = 50% of total supply.
2. Legitimate voters cast enough yes votes at time T. resolvable_time = T.
3. At time T+1, the resolver submits aptos_governance::resolve() (or resolve_multi_step_proposal()).
4. Attacker (stake pool S with remaining voting power on P) submits aptos_governance::partial_vote(S, P, 1, true) in the same block.
5. The vote executes first (or in the same block), writing resolvable_time = T+1.
6. The resolve executes at T+1: assert!(T+1 > T+1) → FALSE → abort ERESOLUTION_CANNOT_BE_ATOMIC.
7. Attacker repeats each block until proposal expires at expiration_secs.
Result: The governance proposal is never executed.
```

The `vote()` function accepts `num_votes = 0` at the `voting.move` layer (no minimum check there), but `aptos_governance::vote_internal()` enforces `voting_power > 0`. With partial voting, the attacker can cast `1` unit of voting power per transaction, sustaining the attack for as many blocks as they have voting power units. [6](#0-5) [7](#0-6) [8](#0-7)

### Citations

**File:** aptos-move/framework/aptos-framework/sources/voting.move (L350-389)
```text
    public fun vote<ProposalType: store>(
        _proof: &ProposalType,
        voting_forum_address: address,
        proposal_id: u64,
        num_votes: u64,
        should_pass: bool,
    ) acquires VotingForum {
        let voting_forum = borrow_global_mut<VotingForum<ProposalType>>(voting_forum_address);
        let proposal = voting_forum.proposals.borrow_mut(proposal_id);
        // Voting might still be possible after the proposal has enough yes votes to be resolved early. This would only
        // lead to possible proposal resolution failure if the resolve early threshold is not definitive (e.g. < 50% + 1
        // of the total voting token's supply). In this case, more voting might actually still be desirable.
        // Governance mechanisms built on this voting module can apply additional rules on when voting is closed as
        // appropriate.
        assert!(!is_voting_period_over(proposal), error::invalid_state(EPROPOSAL_VOTING_ALREADY_ENDED));
        assert!(!proposal.is_resolved, error::invalid_state(EPROPOSAL_ALREADY_RESOLVED));
        // Assert this proposal is single-step, or if the proposal is multi-step, it is not in execution yet.
        assert!(!proposal.metadata.contains_key(&utf8(IS_MULTI_STEP_PROPOSAL_IN_EXECUTION_KEY))
            || *proposal.metadata.borrow(&utf8(IS_MULTI_STEP_PROPOSAL_IN_EXECUTION_KEY)) == to_bytes(
            &false
        ),
            error::invalid_state(EMULTI_STEP_PROPOSAL_IN_EXECUTION));

        if (should_pass) {
            proposal.yes_votes += (num_votes as u128);
        } else {
            proposal.no_votes += (num_votes as u128);
        };

        // Record the resolvable time to ensure that resolution has to be done non-atomically.
        let timestamp_secs_bytes = to_bytes(&timestamp::now_seconds());
        let key = utf8(RESOLVABLE_TIME_METADATA_KEY);
        if (proposal.metadata.contains_key(&key)) {
            *proposal.metadata.borrow_mut(&key) = timestamp_secs_bytes;
        } else {
            proposal.metadata.add(key, timestamp_secs_bytes);
        };

        event::emit(Vote { proposal_id, num_votes });
    }
```

**File:** aptos-move/framework/aptos-framework/sources/voting.move (L391-412)
```text
    /// Common checks on if a proposal is resolvable, regardless if the proposal is single-step or multi-step.
    fun is_proposal_resolvable<ProposalType: store>(
        voting_forum_address: address,
        proposal_id: u64,
    ) acquires VotingForum {
        let proposal_state = get_proposal_state<ProposalType>(voting_forum_address, proposal_id);
        assert!(proposal_state == PROPOSAL_STATE_SUCCEEDED, error::invalid_state(EPROPOSAL_CANNOT_BE_RESOLVED));

        let voting_forum = borrow_global_mut<VotingForum<ProposalType>>(voting_forum_address);
        let proposal = voting_forum.proposals.borrow_mut(proposal_id);
        assert!(!proposal.is_resolved, error::invalid_state(EPROPOSAL_ALREADY_RESOLVED));

        // We need to make sure that the resolution is happening in
        // a separate transaction from the last vote to guard against any potential flashloan attacks.
        let resolvable_time = to_u64(*proposal.metadata.borrow(&utf8(RESOLVABLE_TIME_METADATA_KEY)));
        assert!(timestamp::now_seconds() > resolvable_time, error::invalid_state(ERESOLUTION_CANNOT_BE_ATOMIC));

        assert!(
            transaction_context::get_script_hash() == proposal.execution_hash,
            error::invalid_argument(EPROPOSAL_EXECUTION_HASH_NOT_MATCHING),
        );
    }
```

**File:** aptos-move/framework/aptos-framework/sources/aptos_governance.move (L522-548)
```text
    fun vote_internal(
        voter: &signer,
        stake_pool: address,
        proposal_id: u64,
        voting_power: u64,
        should_pass: bool,
    ) acquires ApprovedExecutionHashes, VotingRecords, VotingRecordsV2 {
        let voter_address = signer::address_of(voter);
        assert!(stake::get_delegated_voter(stake_pool) == voter_address, error::invalid_argument(ENOT_DELEGATED_VOTER));

        assert_proposal_expiration(stake_pool, proposal_id);

        // If a stake pool has already voted on a proposal before partial governance voting is enabled,
        // `get_remaining_voting_power` returns 0.
        let staking_pool_voting_power = get_remaining_voting_power(stake_pool, proposal_id);
        voting_power = min(voting_power, staking_pool_voting_power);

        // Short-circuit if the voter has no voting power.
        assert!(voting_power > 0, error::invalid_argument(ENO_VOTING_POWER));

        voting::vote<GovernanceProposal>(
            &governance_proposal::create_empty_proposal(),
            @aptos_framework,
            proposal_id,
            voting_power,
            should_pass,
        );
```

**File:** aptos-move/framework/aptos-framework/sources/timestamp.move (L67-69)
```text
    public fun now_seconds(): u64 acquires CurrentTimeMicroseconds {
        now_microseconds() / MICRO_CONVERSION_FACTOR
    }
```
