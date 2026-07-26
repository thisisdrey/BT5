### Title
Stale Total Supply Snapshot in `early_resolution_vote_threshold` Allows Early Governance Resolution Below True 50% Threshold — (`aptos-move/framework/aptos-framework/sources/aptos_governance.move`)

---

### Summary

`aptos_governance::create_proposal_v2_impl` snapshots `coin::supply<AptosCoin>()` at proposal-creation time and stores `total_supply / 2 + 1` as the proposal's `early_resolution_vote_threshold`. Because APT supply changes continuously (staking rewards mint APT every epoch; gas fees burn APT every transaction), this stored value becomes stale during the voting window. Any voter who accumulates votes equal to the stale threshold can trigger early resolution even though they represent less than 50% of the *current* supply — the exact invariant the threshold is meant to enforce.

---

### Finding Description

In `create_proposal_v2_impl`:

```move
let total_voting_token_supply = coin::supply<AptosCoin>();
let early_resolution_vote_threshold = option::none<u128>();
if (total_voting_token_supply.is_some()) {
    let total_supply = *total_voting_token_supply.borrow();
    // 50% + 1 to avoid rounding errors.
    early_resolution_vote_threshold = option::some(total_supply / 2 + 1);
};
``` [1](#0-0) 

This value is stored verbatim in the `Proposal` struct field `early_resolution_vote_threshold`: [2](#0-1) 

It is later checked in `can_be_resolved_early`, which reads the stored (stale) value directly from the proposal:

```move
public fun can_be_resolved_early<ProposalType: store>(proposal: &Proposal<ProposalType>): bool {
    if (proposal.early_resolution_vote_threshold.is_some()) {
        let early_resolution_threshold = *proposal.early_resolution_vote_threshold.borrow();
        if (proposal.yes_votes >= early_resolution_threshold || proposal.no_votes >= early_resolution_threshold) {
            return true
        };
    };
    false
}
``` [3](#0-2) 

Critically, while the threshold is frozen at creation, the votes that accumulate against it are **not** frozen. `get_remaining_voting_power` uses the **current live** stake:

```move
get_voting_power(stake_pool) - used_voting_power
``` [4](#0-3) 

The code comment in `create_proposal_v2_impl` explicitly acknowledges this asymmetry:

> "This doesn't take into subsequent inflation/deflation (rewards are issued every epoch and gas fees are burnt after every transaction), but inflation/deflation is very unlikely to have a major impact on total supply during the voting period." [5](#0-4) 

The spec file also acknowledges the live-stake behavior:

> "Note: a stake pool's voting power on a proposal could increase over time (e.g. rewards/new stake)." [6](#0-5) 

---

### Impact Explanation

The `early_resolution_vote_threshold` is intended to represent "> 50% of total APT supply has voted," enabling a supermajority to short-circuit the full voting window. Because the threshold is frozen at proposal creation while the actual supply grows (staking rewards are minted every epoch), the threshold drifts below 50% of the current supply over the voting period.

**Concrete scenario:**
- At proposal creation: total supply = S, threshold stored = S/2 + 1
- After N epochs of staking rewards: total supply = S′ > S
- A coalition holding S/2 + 1 votes (which is < S′/2 + 1, i.e., less than 50% of current supply) can trigger early resolution
- The proposal is resolved before the full voting window, preventing other token holders from casting votes

Because `get_remaining_voting_power` uses live stake (including rewards earned after proposal creation), voters' power grows during the window while the threshold stays fixed. This widens the gap over time.

The inverse also holds: if supply decreases (gas burns exceed rewards), the threshold becomes too high, making early resolution harder than intended — a governance availability degradation.

---

### Likelihood Explanation

APT staking rewards are distributed every epoch (~2 hours). Over a 7-day voting window (~84 epochs), the supply grows by a small but non-zero percentage. The discrepancy is proportional to the reward rate and the voting duration. Under current mainnet parameters this is a small absolute drift, but:

1. The effect is **always present** on every proposal — it is not a corner case.
2. Any actor who accumulates votes near the threshold benefits from the drift.
3. The attack is **unprivileged**: any validator or stake pool operator can vote; no special role is required.
4. The code itself acknowledges the issue, indicating it is a known design gap rather than an oversight.

---

### Recommendation

Compute `early_resolution_vote_threshold` at the time `is_voting_closed` / `can_be_resolved_early` is evaluated, using the live `coin::supply<AptosCoin>()`, rather than storing a snapshot at proposal creation. This mirrors the OpenZeppelin `GovernorVotesQuorumFraction` approach cited in the external report.

If a live lookup is undesirable (e.g., for gas or determinism reasons), store the threshold as a **fraction** (numerator/denominator) rather than an absolute value, and recompute the absolute threshold on each check against the current supply.

---

### Proof of Concept

1. At block B, total APT supply = 1,000,000,000. A proposer calls `create_proposal_v2`. The stored `early_resolution_vote_threshold` = 500,000,001.
2. Over the next 7 days (84 epochs), staking rewards mint ~0.5% additional APT. Total supply grows to ~1,005,000,000.
3. A coalition of validators whose combined current stake = 500,000,001 APT (≈ 49.75% of current supply) calls `vote` / `partial_vote` with `should_pass = true`.
4. `can_be_resolved_early` reads the stored threshold (500,000,001) and finds `yes_votes >= early_resolution_threshold` → returns `true`.
5. `is_voting_closed` returns `true`; `get_proposal_state` returns `PROPOSAL_STATE_SUCCEEDED` (assuming `yes_votes > no_votes` and `min_vote_threshold` is met).
6. `add_approved_script_hash` is called; the proposal is executable — resolved with less than 50% of the current supply, violating the intended invariant. [7](#0-6) [8](#0-7)

### Citations

**File:** aptos-move/framework/aptos-framework/sources/aptos_governance.move (L301-302)
```text
    /// Return remaining voting power of a stake pool on a proposal.
    /// Note: a stake pool's voting power on a proposal could increase over time(e.g. rewards/new stake).
```

**File:** aptos-move/framework/aptos-framework/sources/aptos_governance.move (L329-330)
```text
        let used_voting_power = *VotingRecordsV2[@aptos_framework].votes.borrow_with_default(record_key, &0);
        get_voting_power(stake_pool) - used_voting_power
```

**File:** aptos-move/framework/aptos-framework/sources/aptos_governance.move (L403-458)
```text
    public fun create_proposal_v2_impl(
        proposer: &signer,
        stake_pool: address,
        execution_hash: vector<u8>,
        metadata_location: vector<u8>,
        metadata_hash: vector<u8>,
        is_multi_step_proposal: bool,
    ): u64 acquires GovernanceConfig {
        let proposer_address = signer::address_of(proposer);
        assert!(
            stake::get_delegated_voter(stake_pool) == proposer_address,
            error::invalid_argument(ENOT_DELEGATED_VOTER)
        );

        // The proposer's stake needs to be at least the required bond amount.
        let governance_config = borrow_global<GovernanceConfig>(@aptos_framework);
        let stake_balance = get_voting_power(stake_pool);
        assert!(
            stake_balance >= governance_config.required_proposer_stake,
            error::invalid_argument(EINSUFFICIENT_PROPOSER_STAKE),
        );

        // The proposer's stake needs to be locked up at least as long as the proposal's voting period.
        let current_time = timestamp::now_seconds();
        let proposal_expiration = current_time + governance_config.voting_duration_secs;
        assert!(
            stake_pool_is_eligible_to_vote(stake_pool, proposal_expiration),
            error::invalid_argument(EINSUFFICIENT_STAKE_LOCKUP),
        );

        // Create and validate proposal metadata.
        let proposal_metadata = create_proposal_metadata(metadata_location, metadata_hash);

        // We want to allow early resolution of proposals if more than 50% of the total supply of the network coins
        // has voted. This doesn't take into subsequent inflation/deflation (rewards are issued every epoch and gas fees
        // are burnt after every transaction), but inflation/delation is very unlikely to have a major impact on total
        // supply during the voting period.
        let total_voting_token_supply = coin::supply<AptosCoin>();
        let early_resolution_vote_threshold = option::none<u128>();
        if (total_voting_token_supply.is_some()) {
            let total_supply = *total_voting_token_supply.borrow();
            // 50% + 1 to avoid rounding errors.
            early_resolution_vote_threshold = option::some(total_supply / 2 + 1);
        };

        let proposal_id = voting::create_proposal_v2(
            proposer_address,
            @aptos_framework,
            governance_proposal::create_proposal(),
            execution_hash,
            governance_config.min_voting_threshold,
            proposal_expiration,
            early_resolution_vote_threshold,
            proposal_metadata,
            is_multi_step_proposal,
        );
```

**File:** aptos-move/framework/aptos-framework/sources/voting.move (L112-116)
```text
        /// Optional. Early resolution threshold. If specified, the proposal can be resolved early if the total
        /// number of yes or no votes passes this threshold.
        /// For example, this can be set to 50% of the total supply of the voting token, so if > 50% vote yes or no,
        /// the proposal can be resolved before expiration.
        early_resolution_vote_threshold: Option<u128>,
```

**File:** aptos-move/framework/aptos-framework/sources/voting.move (L541-558)
```text
    #[view]
    public fun is_voting_closed<ProposalType: store>(
        voting_forum_address: address,
        proposal_id: u64
    ): bool acquires VotingForum {
        let proposal = get_proposal<ProposalType>(voting_forum_address, proposal_id);
        can_be_resolved_early(proposal) || is_voting_period_over(proposal)
    }

    /// Return true if the proposal has reached early resolution threshold (if specified).
    public fun can_be_resolved_early<ProposalType: store>(proposal: &Proposal<ProposalType>): bool {
        if (proposal.early_resolution_vote_threshold.is_some()) {
            let early_resolution_threshold = *proposal.early_resolution_vote_threshold.borrow();
            if (proposal.yes_votes >= early_resolution_threshold || proposal.no_votes >= early_resolution_threshold) {
                return true
            };
        };
        false
```
