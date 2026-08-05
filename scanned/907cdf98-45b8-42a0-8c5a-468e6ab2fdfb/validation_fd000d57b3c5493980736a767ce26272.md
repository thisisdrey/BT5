### Title
Reward page is marked "claimed" before the pot-to-staker transfer succeeds, permanently burning rewards on insufficient pot liquidity - (File: substrate/frame/staking-async/src/pallet/impls.rs)

### Summary
`pallet-staking-async`'s DAP (transfer-based, non-minting) reward payout path marks an era/validator/page as claimed *before* the actual token transfer from the era reward pot to the staker is attempted. If the reward pot lacks sufficient free balance to satisfy `Preservation::Expendable` transfers (e.g. partially-funded pot, existential-deposit edge cases, or a pot drained by a prior payout in the same page), the transfer silently fails, the reward is dropped, and the page can never be retried because it is already flagged as claimed.

### Finding Description
The public extrinsic handler in `substrate/frame/staking-async/src/pallet/impls.rs` calls `Eras::<T>::set_rewards_as_claimed(era, &stash, page)` at line 386, *before* any nominator/validator payout is actually transferred: [1](#0-0) 

Later, when the DAP (transfer, no minting) path is active, `payout_from_provider` invokes `make_payout_from_provider` for the validator and every nominator: [2](#0-1) 

`make_payout_from_provider` performs `T::Currency::transfer(&staker_rewards_pot, &payout_account, amount, Preservation::Expendable)`. If this transfer errors (e.g. `InsufficientBalance` because the pot doesn't hold enough liquidity for this particular payout), the function only logs the error and returns `None` — the caller (`payout_from_provider`) treats this exactly like a zero-reward case and moves on to the next nominator, without ever surfacing an error to the dispatchable: [3](#0-2) 

Because `set_rewards_as_claimed` already executed unconditionally before any transfer attempt, and the extrinsic call in `do_payout_stakers`-style logic returns `Ok(...)` regardless of whether `make_payout_from_provider` succeeded or silently failed, `Eras::<T>::is_rewards_claimed(era, &stash, page)` will return `true` forever afterward: [4](#0-3) 

This is structurally identical to the LiFuelFacet issue: a payout/transfer path that has no check for available liquidity on the paying side before marking the operation as done, so insufficient liquidity results in funds becoming permanently unclaimable rather than merely delayed.

### Impact Explanation
Any nominator or validator whose reward pot for an era happens to be underfunded (e.g., due to reward pot funding lag, rounding dust across many payout pages, partial slashing of the pot, or any discrepancy between `era_payout` calculation and what was actually deposited into `RewardPot::Era(era, RewardKind::StakerRewards)`) permanently loses that reward. Since `set_rewards_as_claimed` is unconditional and irreversible from user-facing dispatchables, there is no legitimate retry path — the funds are neither transferred to the staker nor returned/reallocated; they remain stuck in (or absent from) the pot with a claimed marker preventing recovery. This satisfies the "permanent user-fund lock" and "duplicate/incorrect settlement" impact categories described for this program, since state (claimed=true) advances before settlement (transfer) is confirmed to succeed, violating the required invariant that "payout state must only advance after ... settlement succeed[s] atomically."

### Likelihood Explanation
The path is reachable by any unprivileged account calling the public `payout_stakers` extrinsic (or its page variant) for an era/validator/page — this is by design a permissionless dispatchable that anyone can call on behalf of a staker. No malicious validator, relayer, governance action, or privileged actor is required. The only precondition is that the era reward pot's balance temporarily or structurally can't cover a payout on a given page (a plausible operational condition, particularly for pots holding many small pages or subject to rounding across `Perbill::mul_floor` computations and existential-deposit edges under `Preservation::Expendable`).

### Recommendation
- Perform the pot-to-staker transfer(s) for the page (or at minimum verify the pot's free balance is sufficient to cover the whole page's payout) before calling `set_rewards_as_claimed`.
- If a transfer within `make_payout_from_provider` fails, propagate the error (or record the specific failed sub-payout) instead of silently returning `None`, and avoid marking the page claimed until all constituent transfers are confirmed, or introduce a distinct "partially failed / retryable" state instead of `claimed`.
- Alternatively, keep an explicit ledger of amounts still owed per (era, stash) so failed transfers can be retried later even if the page is marked claimed for bookkeeping purposes.

### Proof of Concept
1. Configure a runtime using `pallet-staking-async` with the DAP path enabled (`DisableMintingGuard` set for the target era) as in `payout_from_provider`.
2. Ensure the era's staker rewards pot (`RewardPot::Era(era, RewardKind::StakerRewards)`) is funded with less than the total `validator_staker_payout_for_page + nominator payouts` for a page, e.g., by only partially depositing rewards into the pot (or by triggering a pot balance drop via a legitimate concurrent claim on another page sharing insufficient float).
3. Call the public `payout_stakers` extrinsic for that era/validator/page from any account.
4. Observe: `Eras::<T>::set_rewards_as_claimed` executes (visible via `is_rewards_claimed` returning `true`), while `T::Currency::transfer` inside `make_payout_from_provider` fails and only logs an error — no `Rewarded` event is emitted for the affected staker(s), and the extrinsic itself returns `Ok(...)`.
5. Call `payout_stakers` again for the same era/validator/page: it now fails with `Error::<T>::AlreadyClaimed` (from the check at lines 381-384), proving the reward is permanently unclaimable and the staker's funds are lost. [4](#0-3) [5](#0-4)

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

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L600-616)
```rust
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
