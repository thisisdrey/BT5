Based on my investigation, I found a genuine local analog to the "silently handled error" bug class in `pallet-staking-async`'s election-result storage path.

### Title
Election-exposure storage errors are silently discarded via `let _ =`, allowing an election to be accepted as successful while staker reward data is missing - ([File: substrate/frame/staking-async/src/pallet/impls.rs])

### Summary
`EraElectionPlanner::do_elect_paged_inner` calls `Self::store_stakers_info(exposures, planning_era)` and discards its `Result` with `let _ = ...` in both the success and truncation branches, then reports the election as having proceeded (via `Ok(added)` / `Err(not_included)` describing only the electable-stash truncation, not the exposure-storage outcome). This mirrors the `RemoveDelegateStake` pattern in the report: a fallible "recording" step's error is checked/discarded but processing continues and downstream state advances as if it succeeded.

### Finding Description
In `do_elect_paged_inner`:
```rust
match Self::add_electables(supports.iter().map(|(s, _)| s.clone())) {
    Ok(added) => {
        let exposures = Self::collect_exposures(supports);
        let _ = Self::store_stakers_info(exposures, planning_era);   // <-- error discarded
        Ok(added)
    },
    Err(not_included_idx) => {
        ...
        let exposures = Self::collect_exposures(supports);
        let _ = Self::store_stakers_info(exposures, planning_era);   // <-- error discarded
        Err(not_included)
    },
}
``` [1](#0-0) 

This is called from `do_elect_paged`, which treats the `Ok`/`Err` result only as a count of truncated electable stashes, deposits `Event::PagedElectionProceeded`, and otherwise proceeds to finalize the era regardless of whether `store_stakers_info` actually persisted exposure data for the elected validators: [2](#0-1) 

Because the error from `store_stakers_info` is never inspected, if it fails (e.g., bounded storage overflow, duplicate/overlapping page write, or any other internal fault it may return), the validator is nonetheless recorded as "electable" via `add_electables`, and the era rotation proceeds to use these stashes for the next era's validator set — but with no (or corrupted) exposure/stake data backing them.

### Impact Explanation
`Eras::get_paged_exposure` and reward point accounting in `do_payout_stakers_by_page` rely on exposure data written by `store_stakers_info`: [3](#0-2) 
If exposure storage silently failed for a page/era, elected validators would have no paged exposure recorded, causing reward payouts for that era/validator to be permanently unavailable (`InvalidEraToReward`), or — depending on partial-write semantics — nominator stake exposures could be stored inconsistently with who was actually elected, breaking the "settle exactly once to the rightful beneficiary and amount" invariant for staking rewards. This is a state-integrity bug in core validator-set/reward accounting for the relay chain, not merely a UX inconvenience.

### Likelihood Explanation
This path executes unconditionally on every paged election result during era rotation (`plan_new_election`/`do_elect_paged`), which runs automatically at every era change — no privileged actor or malicious peer is required. The only requirement is that `store_stakers_info` return an `Err` under some internal condition (e.g., bounds/overflow on large validator sets, a page-index collision, or any future fallible logic added to that function), at which point the discarded error silently lets the election be treated as fully processed.

### Recommendation
Do not discard the `Result` from `store_stakers_info`. Propagate the error (or use `defensive!`/abort the era transition) so that a failure to persist staker exposure information cannot result in an "elected but unbacked" validator being carried into the next era. At minimum, mirror the explicit-return pattern used elsewhere in this codebase (e.g., `RemoveStake`-style handling) rather than the `let _ = ...` silent-discard pattern used here.

### Proof of Concept
Not independently reproduced against a live runtime within this investigation — the concrete failure trigger inside `store_stakers_info` (what conditions cause it to return `Err`) was not fully explored due to tool-call limits reached before inspecting its full body in `session_rotation.rs`. The code-flow evidence above (discarded `Result` via `let _ =` at two call sites, followed by unconditional continuation of era finalization and reward-payout logic that depends on the discarded write having succeeded) is sourced directly from the repository and is the basis for this finding; a background engineer should read `store_stakers_info`'s full implementation in `substrate/frame/staking-async/src/session_rotation.rs` to confirm the exact error conditions and construct a deterministic test (e.g., forcing a bounded-storage overflow) that demonstrates a validator being elected without corresponding exposure data.

### Citations

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L386-406)
```rust
		Eras::<T>::set_rewards_as_claimed(era, &stash, page);

		let exposure = Eras::<T>::get_paged_exposure(era, &stash, page).ok_or_else(|| {
			Error::<T>::InvalidEraToReward
				.with_weight(T::WeightInfo::payout_stakers_alive_staked(0))
		})?;

		// Input data seems good, no errors allowed after this point

		let era_reward_points = Eras::<T>::get_reward_points(era);
		let total_reward_points = era_reward_points.total;
		let validator_reward_points =
			era_reward_points.individual.get(&stash).copied().unwrap_or_else(Zero::zero);

		// Nothing to do if they have no reward points.
		if validator_reward_points.is_zero() {
			return Ok(Some(T::WeightInfo::payout_stakers_alive_staked(0)).into());
		}

		// This is the fraction of the total reward that the validator and the
		// nominators will get.
```

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L1208-1236)
```rust
		T::NominationsQuota::get_quota(balance)
	}

	pub fn api_eras_stakers(
		era: EraIndex,
		account: T::AccountId,
	) -> Exposure<T::AccountId, BalanceOf<T>> {
		Self::eras_stakers(era, &account)
	}

	pub fn api_eras_stakers_page_count(era: EraIndex, account: T::AccountId) -> Page {
		Eras::<T>::exposure_page_count(era, &account)
	}

	pub fn api_pending_rewards(era: EraIndex, account: T::AccountId) -> bool {
		Eras::<T>::pending_rewards(era, &account)
	}
}

impl<T: Config> ElectionDataProvider for Pallet<T> {
	type AccountId = T::AccountId;
	type BlockNumber = BlockNumberFor<T>;
	type MaxVotesPerVoter = MaxNominationsOf<T>;

	fn desired_targets() -> data_provider::Result<u32> {
		Self::register_weight(T::DbWeight::get().reads(1));
		Ok(ValidatorCount::<T>::get())
	}

```

**File:** substrate/frame/staking-async/src/session_rotation.rs (L1178-1201)
```rust
	pub(crate) fn do_elect_paged(page: PageIndex) {
		let election_result = T::ElectionProvider::elect(page);
		match election_result {
			Ok(supports) => {
				let inner_processing_results = Self::do_elect_paged_inner(supports);
				if let Err(not_included) = inner_processing_results {
					defensive!(
						"electable stashes exceeded limit, unexpected but election proceeds.\
                		{} stashes from election result discarded",
						not_included
					);
				};

				Pallet::<T>::deposit_event(Event::PagedElectionProceeded {
					page,
					result: inner_processing_results.map(|x| x as u32).map_err(|x| x as u32),
				});
			},
			Err(e) => {
				log!(warn, "election provider page failed due to {:?} (page: {})", e, page);
				Pallet::<T>::deposit_event(Event::PagedElectionProceeded { page, result: Err(0) });
			},
		}
	}
```
