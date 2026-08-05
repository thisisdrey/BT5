Audit Report

## Title
Staking reward page marked as claimed before payout succeeds, permanently losing rewards on pot underfunding - (File: `substrate/frame/staking-async/src/pallet/impls.rs`)

## Summary
`do_payout_stakers_by_page` calls `Eras::<T>::set_rewards_as_claimed(era, &stash, page)` unconditionally before any actual currency transfer to the validator or its nominators is attempted, and the subsequent transfer-mode payout loop in `payout_from_provider` iterates every nominator, invoking `make_payout_from_provider` per recipient, which silently swallows transfer failures (e.g., insufficient balance in the shared per-era pot) by logging an error and returning `None` rather than propagating a `DispatchError`. Since the claimed flag is already set, a nominator whose transfer fails due to pot underfunding has no way to retry, resulting in a permanent, silent loss of their entitled reward.

## Finding Description
In `do_payout_stakers_by_page`, the claim check and claim-set operations occur before the exposure/payout calculation and before any transfer: [1](#0-0) 

The transfer-mode payout path (`use_dap_payout` true) calls `payout_from_provider`, which loops over `exposure.others()` and calls `make_payout_from_provider` for each nominator, transferring from the shared `StakerRewards` era pot account: [2](#0-1) 

`make_payout_from_provider` performs the actual `T::Currency::transfer` from the shared pot account and, on failure, only logs the error and returns `None` without aborting the extrinsic or reverting the already-set claimed state: [3](#0-2) 

Because `set_rewards_as_claimed` runs before these transfers and is not conditioned on their outcome, a subsequent call to `payout_stakers_by_page` for the same `(era, stash, page)` is rejected via the `AlreadyClaimed` check at lines 381-384, permanently foreclosing retry for any nominator whose transfer failed.

I was unable to fully verify, within the available tooling, whether the `StakerRewards` pot account is guaranteed to always hold sufficient funds at the time `payout_stakers_by_page` executes (e.g., via an invariant enforced elsewhere in `reward.rs`, `budget.rs`, or `pallet/mod.rs` that funds the pot exactly once by minting/transferring the full `era_payout` before any claims are possible). If such an invariant exists and is correctly enforced, the "underfunding" precondition in the claim may not be reachable in practice by an unprivileged caller; I could not conclusively confirm or rule this out with the tools available, and no test in `substrate/frame/staking-async/src/tests/payout_stakers.rs` exercises this exact underfunded-pot scenario.

## Impact Explanation
If the shared per-era `StakerRewards` pot account can ever hold less than the sum of all entitled recipients' payouts for a page (whether via rounding/dust accumulation across `Perbill::mul_floor` calls, partial prior draining, or budget misconfiguration), affected nominators lose their reward permanently and silently, with no error surfaced to the caller and no way to retry. This matches the "duplicate settlement or payout" / "permanent user-fund lock" impact category in the Polkadot SDK impact gate, since settlement state (`claimed`) is finalized independently of whether funds actually moved to the rightful beneficiary.

## Likelihood Explanation
The reachability of this bug hinges entirely on whether the pot can be underfunded relative to the sum of per-page claims through normal usage — a precondition I could not fully verify against the pot-funding invariant enforced elsewhere in the pallet (e.g., in `reward.rs`/budget-recipient minting logic). Absent confirmation that such underfunding is achievable via unprivileged, public extrinsic calls without relying on prior privileged/governance manipulation of the pot, I cannot establish with confidence that this is exploitable by an ordinary user under the stated impact gate, which explicitly requires a reachable exploit path from unprivileged input to bad payout state.

## Recommendation
Given the uncertainty about whether the pot-funding invariant already prevents this condition, the underlying pattern is still fragile: `Eras::<T>::set_rewards_as_claimed` should not be finalized before all transfers in the page are confirmed to have succeeded, or per-recipient claim/reclaim tracking should be added so a failed transfer can be retried rather than being silently and permanently dropped. Transfer failures in `make_payout_from_provider` should be aggregated and either cause the extrinsic to fail atomically (enabling retry) or leave the specific failed recipient's claim state open.

## Proof of Concept
Unable to construct a concrete, verified PoC without confirming the pot-funding invariant (whether `StakerRewards` pot balance is always guaranteed ≥ sum of a page's entitled payouts before `payout_stakers_by_page` is called). A reproducible test would need to: (1) configure transfer/DAP mode via `DisableMintingGuard`, (2) artificially reduce the `StakerRewards` pot account balance below the page's total entitled payout, (3) call `payout_stakers_by_page`, and (4) confirm the claimed flag is set at line 386 while `Rewarded` is not emitted for the underfunded nominator and a retry returns `AlreadyClaimed`. This scenario could not be validated against existing pot-funding safeguards with the tools available in this session.

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

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L577-616)
```rust
	/// Make a payment to a staker from an era reward pot (transfer, not mint).
	fn make_payout_from_provider(
		era: EraIndex,
		stash: &T::AccountId,
		amount: BalanceOf<T>,
	) -> Option<(BalanceOf<T>, RewardDestination<T::AccountId>)> {
		if amount.is_zero() {
			return None;
		}

		let dest = match Self::payee(Stash(stash.clone())) {
			Some(d) => d,
			None => {
				Self::deposit_event(Event::<T>::Unexpected(UnexpectedKind::MissingPayee {
					era,
					stash: stash.clone(),
				}));
				return None;
			},
		};

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
