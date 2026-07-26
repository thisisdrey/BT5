### Title
Stake Pool Can Vote Both YES and NO on the Same Governance Proposal via `partial_vote` — (`aptos-move/framework/aptos-framework/sources/aptos_governance.move`)

---

### Summary

`VotingRecordsV2` tracks only the total `used_voting_power` per `(stake_pool, proposal_id)` pair, with no field for vote direction. Because `vote_internal` enforces only that remaining power is positive, a stake pool can call `partial_vote` first with `should_pass = true` and then again with `should_pass = false` (or vice versa), splitting its voting power across both sides of the same proposal. This is the direct analog of the Munchables missing-disapproval-check bug: the mutual-exclusion invariant between YES and NO votes is absent.

---

### Finding Description

`RecordKey` is defined as:

```move
struct RecordKey has copy, drop, store {
    stake_pool: address,
    proposal_id: u64,
}
``` [1](#0-0) 

`VotingRecordsV2` maps this key to a plain `u64` representing total consumed power:

```move
struct VotingRecordsV2 has key {
    votes: SmartTable<RecordKey, u64>
}
``` [2](#0-1) 

`get_remaining_voting_power` returns `total_power - used_power` with no awareness of direction:

```move
let used_voting_power = *VotingRecordsV2[@aptos_framework].votes.borrow_with_default(record_key, &0);
get_voting_power(stake_pool) - used_voting_power
``` [3](#0-2) 

`vote_internal` consumes remaining power and passes `should_pass` directly to `voting::vote`, which independently accumulates `yes_votes` and `no_votes`:

```move
let staking_pool_voting_power = get_remaining_voting_power(stake_pool, proposal_id);
voting_power = min(voting_power, staking_pool_voting_power);
assert!(voting_power > 0, error::invalid_argument(ENO_VOTING_POWER));
voting::vote<GovernanceProposal>(..., voting_power, should_pass);
...
*used_voting_power += voting_power;
``` [4](#0-3) 

Inside `voting::vote`, the two counters are completely independent:

```move
if (should_pass) {
    proposal.yes_votes += (num_votes as u128);
} else {
    proposal.no_votes += (num_votes as u128);
};
``` [5](#0-4) 

There is no check that prevents a stake pool that has already cast YES votes from subsequently casting NO votes (or vice versa). The existing `test_cannot_double_vote` test only guards against voting the **same direction** twice:

```move
vote(&voter_1, signer::address_of(&voter_1), 0, true);
vote(&voter_1, signer::address_of(&voter_1), 0, true); // aborts
``` [6](#0-5) 

No test or runtime guard covers the cross-direction case.

---

### Impact Explanation

A stake pool owner can:

1. Vote YES with a large fraction of their power to push `yes_votes` past `min_voting_threshold`.
2. Immediately vote NO with the remaining power to inflate `no_votes`, preventing early resolution (which requires `yes_votes > early_resolution_vote_threshold`, typically >50% of total supply).
3. Alternatively, vote NO first to defeat a proposal, then vote YES with remaining power to revive it — or vice versa — depending on the current tally.

Because `yes_votes` and `no_votes` are both permanently incremented in `voting::vote` with no rollback, the corrupted tally persists for the lifetime of the proposal. This can cause a governance proposal to pass that should fail, or fail that should pass, directly affecting which on-chain framework upgrades or parameter changes are executed. Framework upgrades control APT minting, staking parameters, and module bytecode — making governance integrity a high-value target.

---

### Likelihood Explanation

The `partial_vote` entry point is public and unprivileged — any stake pool delegated voter can call it. The partial governance voting feature flag is enabled on mainnet. The attack requires only two sequential transactions from the same delegated voter address, with no special setup beyond holding a stake pool with non-zero voting power.

---

### Recommendation

Add a per-direction record to `VotingRecordsV2` (or a separate map) so that once a stake pool has cast votes in one direction, it cannot cast votes in the opposite direction on the same proposal. The simplest fix is to store the direction alongside the used power:

```move
struct VoteRecord has copy, drop, store {
    used_voting_power: u64,
    should_pass: bool,
}

struct VotingRecordsV2 has key {
    votes: SmartTable<RecordKey, VoteRecord>
}
```

In `vote_internal`, after retrieving the existing record, assert that `should_pass` matches the previously recorded direction (if any), and abort with a new `EVOTE_DIRECTION_MISMATCH` error if it does not.

---

### Proof of Concept

```move
// Assume partial governance voting feature is enabled.
// voter_1 controls a stake pool with 100 voting power.

// Step 1: vote YES with 60 power
partial_vote(&voter_1, voter_1_addr, proposal_id, 60, true);
// yes_votes += 60, used_power = 60

// Step 2: vote NO with remaining 40 power — succeeds, no guard exists
partial_vote(&voter_1, voter_1_addr, proposal_id, 40, false);
// no_votes += 40, used_power = 100

// Result: voter_1 has contributed 60 YES and 40 NO to the same proposal.
// If min_voting_threshold = 50 and yes > no is required, the proposal passes
// (60 > 40), but voter_1 has also injected 40 artificial NO votes that
// dilute the majority and block early resolution.
// Alternatively, voter_1 could vote NO(60) then YES(40) to flip a close outcome.
```

The `get_remaining_voting_power` call at line 536 returns 40 on the second call because it only subtracts `used_power` (60) from `total_power` (100), with no awareness that the first 60 were cast YES. [7](#0-6)

### Citations

**File:** aptos-move/framework/aptos-framework/sources/aptos_governance.move (L95-98)
```text
    struct RecordKey has copy, drop, store {
        stake_pool: address,
        proposal_id: u64,
    }
```

**File:** aptos-move/framework/aptos-framework/sources/aptos_governance.move (L105-108)
```text
    /// Records to track the voting power usage of each stake pool on each proposal.
    struct VotingRecordsV2 has key {
        votes: SmartTable<RecordKey, u64>
    }
```

**File:** aptos-move/framework/aptos-framework/sources/aptos_governance.move (L329-330)
```text
        let used_voting_power = *VotingRecordsV2[@aptos_framework].votes.borrow_with_default(record_key, &0);
        get_voting_power(stake_pool) - used_voting_power
```

**File:** aptos-move/framework/aptos-framework/sources/aptos_governance.move (L522-556)
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

        let record_key = RecordKey {
            stake_pool,
            proposal_id,
        };
        let used_voting_power = VotingRecordsV2[@aptos_framework].votes.borrow_mut_with_default(record_key, 0);
        // This calculation should never overflow because the used voting cannot exceed the total voting power of this stake pool.
        *used_voting_power += voting_power;
```

**File:** aptos-move/framework/aptos-framework/sources/aptos_governance.move (L969-971)
```text
        // Double voting should throw an error.
        vote(&voter_1, signer::address_of(&voter_1), 0, true);
        vote(&voter_1, signer::address_of(&voter_1), 0, true);
```

**File:** aptos-move/framework/aptos-framework/sources/voting.move (L373-377)
```text
        if (should_pass) {
            proposal.yes_votes += (num_votes as u128);
        } else {
            proposal.no_votes += (num_votes as u128);
        };
```
