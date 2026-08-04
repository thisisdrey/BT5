### Title
`payout_stakers` marks a reward page as claimed before verifying the pot-to-staker transfer succeeded, causing silent permanent loss of nominator/validator rewards — ([File: substrate/frame/staking-async/src/pallet/impls.rs])

### Summary
`pallet-staking-async`'s DAP (non-minting) payout path pays stakers by transferring funds out of a per-era pot account instead of minting. The transfer's `Result` is checked, but only to decide whether to emit an event or silently skip the individual payout — it is never propagated as a dispatch error. Meanwhile, the page/claim state is marked "claimed" *before* any individual transfer is attempted. If a transfer fails (e.g., the shared era pot is drained), `payout_stakers`/`payout_stakers_by_page` still returns `Ok(..)`, the page is permanently marked claimed, and the affected staker's reward is lost with no retry path — an unchecked-external-value-transfer pattern analogous to the ScopeLift `.call` finding, but here manifesting as unbacked/uncaptured value loss instead of a revert.

### Finding Description
In `do_payout_stakers_by_page`, `Eras::<T>::set_rewards_as_claimed(era, &stash, page)` is executed unconditionally, before any payout is attempted: [1](#0-0) 

Later, `payout_from_provider` (the DAP/non-minting path, used whenever `DisableMintingGuard` applies to the era) calls `make_payout_from_provider` for the validator and each nominator: [2](#0-1) 

Inside `make_payout_from_provider`, the actual value movement is a `T::Currency::transfer` from the era's `StakerRewards` pot to the payout account. Its `Result` is checked, but only to log an error and `return None` — the caller treats `None` exactly like "nothing to pay" (e.g., zero-value reward), and does **not** propagate a `DispatchError` or abort the extrinsic: [3](#0-2) 

Because the claimed-flag was already set beforehand (line 386), a failed transfer can never be retried — the page is permanently locked in the "claimed" state with the reward never delivered. Crucially, all stashes drawing rewards for a given era share a single common pot account, `T::RewardPots::pot_account(RewardPot::Era(era, RewardKind::StakerRewards))`: [4](#0-3) 

`payout_stakers`/`claim_payout` is a public, unpermissioned extrinsic — any account can trigger payout for any validator/era/page. Since the pot is shared across all validators for that era and its balance is a fixed snapshot from `snapshot_era_rewards` at era-end (not re-topped-up on demand), any sequence of calls that reduces the pot's `Preservation::Expendable`-transferable balance below what a later legitimate payout needs (rounding dust accumulation across many pages/nominators, or the pot balance falling under the existential deposit for a subsequent transfer) will make a later `T::Currency::transfer` fail with the page nonetheless marked claimed and the call returning success.

This mirrors the external report's root cause exactly: a value-moving `call`/`transfer` whose failure is not surfaced to the caller/committer of state, letting a state-changing action "appear successful" while the actual fund movement silently did not happen — except here it costs the *staker* their reward rather than the caller.

### Impact Explanation
This falls under "duplicate settlement or payout" / "permanent user-fund lock" in the impact gate: message/payout state (`RewardsClaimed`/page-claimed flag) advances to a terminal state even though the settlement (actual currency transfer) did not succeed atomically with it. The result is unrecoverable loss of a nominator's or validator's era reward with no compensating mechanism — the extrinsic returns `Ok`, emits `PayoutStarted`, but the transfer failure is only logged (`log!(error, ...)`), never causing a revert of the "claimed" mark.

### Likelihood Explanation
Likelihood depends on being able to drive the shared era pot balance to a level where an individual transfer fails (e.g., below the transfer amount, or below ED for `Preservation::Expendable` in some edge configurations, or via floor-rounding dust accumulating across many nominator payouts in a page). Because `payout_stakers` is callable by anyone for any validator/era/page and pages are processed independently and out of order, an attacker (or even innocuous concurrent activity) claiming many pages/validators first can exhaust the shared pot before a legitimate late claimant executes, and that claimant's page gets marked claimed with silent transfer failure. This requires no privileged role, governance, relayer, or validator/collator/malicious-peer assumption — it is achievable purely through ordinary use of the public `payout_stakers` extrinsic under a resource-exhaustion condition on the shared pot, so it is a live, in-scope, unprivileged-attacker pattern rather than a purely theoretical one. Exact triggerability (whether normal reward accounting ever leaves the pot short) could not be fully confirmed from the index alone — a full repository session would be needed to trace `StakerRewardCalculator`/rounding guarantees and confirm whether the pot is provably always sufficient.

### Recommendation
- Do not mark a reward page as claimed until all associated payouts (validator + all nominators for that page) have been confirmed successfully transferred.
- Propagate the `Err` from `T::Currency::transfer` in `make_payout_from_provider` out of `payout_from_provider`/`do_payout_stakers_by_page` as a `DispatchError`, rather than converting it to `None`/silently skipping — mirroring the recommended `require(ok, "call failed")` fix in the original report.
- Alternatively, if partial success must be tolerated, add a persistent "unpaid" record per stash/era/page so that failed transfers can be retried without needing to re-mark the page as unclaimed globally, and emit a dedicated failure event that is distinguishable from `Rewarded`.

### Proof of Concept
Conceptual reproduction (requires a Devin session with a running test harness to fully execute):
1. Set up `pallet-staking-async` in non-minting (DAP) mode with `DisableMintingGuard` active for era `E`, per `substrate/frame/staking-async/src/tests/legacy_reward.rs` / `era_rotation.rs` test scaffolding.
2. Let era `E` end normally so `snapshot_era_rewards` funds the era's `StakerRewards` pot with the full validator payout for era `E`.
3. Before the intended payout, forcibly reduce the era pot's balance below what is needed for a later page/stash payout — e.g., by having many nominator payouts for other validators/pages already drain it via rounding, or by directly using `Balances::set_balance` in a test to simulate a drained pot (as the `era_rotation.rs`/`validator_incentive.rs` tests already do to inspect pot balances, e.g. `Balances::balance(&staker_pot_78)` pattern at `substrate/frame/staking-async/src/tests/era_rotation.rs:254-259`).
4. Call `Staking::payout_stakers(RuntimeOrigin::signed(any_account), validator_stash, E)` for the affected page.
5. Observe: the call returns `Ok(..)`, `PayoutStarted`/no `Rewarded` event for the affected stash, but `Eras::<T>::is_rewards_claimed(era, &stash, page)` returns `true` and no follow-up `payout_stakers` call can ever re-attempt the payout for that stash/page — the reward is permanently lost while the pallet reports success.

### Citations

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L381-391)
```rust
		if Eras::<T>::is_rewards_claimed(era, &stash, page) {
			return Err(Error::<T>::AlreadyClaimed
				.with_weight(T::WeightInfo::payout_stakers_alive_staked(0)));
		}

		Eras::<T>::set_rewards_as_claimed(era, &stash, page);

		let exposure = Eras::<T>::get_paged_exposure(era, &stash, page).ok_or_else(|| {
			Error::<T>::InvalidEraToReward
				.with_weight(T::WeightInfo::payout_stakers_alive_staked(0))
		})?;
```

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L480-516)
```rust
	/// Payout stakers from an era reward pot (transfer-based, no minting).
	fn payout_from_provider(
		era: EraIndex,
		stash: &T::AccountId,
		validator_payout: BalanceOf<T>,
		exposure: &crate::PagedExposure<T::AccountId, BalanceOf<T>>,
		overview_own: BalanceOf<T>,
		total_nominator_payout: BalanceOf<T>,
	) -> u32 {
		let mut nominator_payout_count: u32 = 0;

		if let Some((amount, dest)) = Self::make_payout_from_provider(era, stash, validator_payout)
		{
			Self::deposit_event(Event::<T>::Rewarded { stash: stash.clone(), dest, amount });
		}

		let total_nominator_stake = exposure.total().saturating_sub(overview_own);
		for nominator in exposure.others().iter() {
			let nominator_exposure_part =
				Perbill::from_rational(nominator.value, total_nominator_stake);
			let nominator_reward: BalanceOf<T> =
				nominator_exposure_part.mul_floor(total_nominator_payout);

			if let Some((amount, dest)) =
				Self::make_payout_from_provider(era, &nominator.who, nominator_reward)
			{
				nominator_payout_count.saturating_inc();
				Self::deposit_event(Event::<T>::Rewarded {
					stash: nominator.who.clone(),
					dest,
					amount,
				});
			}
		}

		nominator_payout_count
	}
```

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L598-617)
```rust
		let payout_account = Self::payout_account_for_dest(stash, &dest)?;

		let staker_rewards_pot =
			T::RewardPots::pot_account(RewardPot::Era(era, RewardKind::StakerRewards));
		if let Err(e) = T::Currency::transfer(
			&staker_rewards_pot,
			&payout_account,
			amount,
			Preservation::Expendable,
		) {
			log!(
				error,
				"Failed to transfer reward from pot for era {:?}, stash {:?}: {:?}",
				era,
				stash,
				e
			);
			return None;
		}

```
