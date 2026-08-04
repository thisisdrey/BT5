### Title
Reward payout marks era page as claimed even when the underlying token transfer to the payee silently fails, permanently losing staker/validator rewards - (File: `substrate/frame/staking-async/src/pallet/impls.rs`)

### Summary
`pallet-staking-async`'s non-minting payout path pays stakers and validators by transferring real tokens out of a per-era reward pot with `T::Currency::transfer(..., Preservation::Expendable)`. Both `make_payout_from_provider` and `transfer_validator_incentive` swallow any transfer failure: they log a warning/emit an `Unexpected`/`ValidatorIncentiveTransferFailed` event and simply return `None`, instead of propagating a `DispatchError`. The outer page-payout function (`payout_from_provider`) and `do_payout_stakers_by_page` always return `Ok(...)`, so the extrinsic that invoked them succeeds unconditionally and the era/page is recorded as claimed regardless of whether any individual transfer actually reached the payee. This is the direct structural analog of the reported bug class: a native-value transfer that can fail for a legitimate recipient (e.g. an account below the existential deposit, a reaped/dead account, or one whose `provider` references have gone to zero) is used without any mechanism to detect and recover from that failure, resulting in silent, permanent loss of the intended payout. [1](#0-0) [2](#0-1) 

### Finding Description
`make_payout_from_provider` resolves the payee account and then calls `T::Currency::transfer(&staker_rewards_pot, &payout_account, amount, Preservation::Expendable)`. If this fails, the function only logs an error and returns `None` — the caller has no way to distinguish "reward was zero/opted out" from "reward transfer failed": [3](#0-2) 

The same swallow-and-continue pattern is used for `transfer_validator_incentive`, which even has a `defensive!` marker acknowledging the failure is unexpected but still does not abort or retry: [4](#0-3) 

`payout_from_provider` (the page-level driver) calls `make_payout_from_provider` for the validator and every nominator in a loop, only using the `Some`/`None` result to decide whether to emit a `Rewarded` event and count the nominator payout for weight accounting — it never aborts the extrinsic or signals a partial failure back to storage state: [5](#0-4) 

`do_payout_stakers_by_page` (the enclosing function) unconditionally returns `Ok(...)` after calling `payout_from_provider`/`payout_legacy_mint`: [6](#0-5) 

Because the dispatchable succeeds regardless of individual transfer outcomes, the page/era's claim-tracking state (`ClaimedRewards`, tracked and pruned per the pruning-step machinery seen in `era_rotation.rs`) advances as if the payout were fully settled, even though the money for one or more payees never left the era pot's ledger to reach them. This violates the required invariant that "payout state must only advance after ... settlement succeed[s] atomically" — the settlement (the currency transfer) and the state advance (marking the page claimed) are decoupled, with the failure path only visible via a log line and a best-effort `Unexpected`/`ValidatorIncentiveTransferFailed` event that no on-chain logic reacts to.

Because `Preservation::Expendable` is used, a `transfer` call to a payout account that would end up below the existential deposit (e.g. a previously-reaped account, or one with zero providers and a payout amount smaller than ED) returns an error rather than crediting a partial amount; that is exactly the kind of predictable, attacker-uninvolved failure mode that the original report describes for `.transfer()` calls to smart-contract recipients without a compatible fallback — a payee that "looks valid" but cannot actually receive the funds under the chosen preservation/existence rules.

### Impact Explanation
Any staker/validator whose payout account cannot currently accept the exact reward amount under `Preservation::Expendable` (e.g. dust amounts to an account that has been fully reaped, or an account whose existence depends on this very transfer and the amount is below ED) permanently loses that reward with no retry path: the page is marked claimed, so `payout_stakers`/`payout_stakers_by_page` cannot be called again for that (era, validator, page) tuple, and the era pot's leftover balance is eventually swept to `UnclaimedRewardHandler` once `HistoryDepth` expires rather than being recovered by the affected user. This is an unbacked, unrecoverable loss of legitimately earned staking rewards — a direct violation of the "conserve value and settle exactly once to the rightful beneficiary" pivot.

### Likelihood Explanation
No malicious actor, governance action, or privileged party is required. The failure is triggered purely by ordinary account-lifecycle conditions (dust rewards, previously-reaped nominator/validator stash, or any other condition causing `Currency::transfer` to return an `Err` under `Expendable` preservation) which are common in a live staking system with many small nominators. The code path is on the default `payout_stakers` / `payout_stakers_by_page` extrinsic, callable by anyone for any era/validator, making the failure condition easily and repeatedly observable in production without any privileged access.

### Recommendation
Propagate transfer failures out of `make_payout_from_provider`/`transfer_validator_incentive` instead of converting them into a logged `None`, and make the page-claim bookkeeping conditional on all transfers in the page actually succeeding (or, alternatively, retain a per-payee "pending"/"failed" record that permits a future retry or a dedicated withdrawal by the affected account instead of unconditionally sweeping the leftover pot balance to `UnclaimedRewardHandler`). At minimum, do not mark `ClaimedRewards` for the page until every transfer attempted within it succeeds, or track/refund per-payee failures individually so no reward is lost, mirroring the low-level-call-with-explicit-error-handling recommendation from the source report.

### Proof of Concept
1. Let stash `S` be a nominator with `RewardDestination::Account(A)` where `A` currently has zero free balance and zero provider references (fully reaped, e.g., dusted out or never funded).
2. Let the nominator's exposure-derived `nominator_reward` for era `E`/page `P` be a small amount below the configured existential deposit.
3. Anyone calls `payout_stakers_by_page(validator_stash, era=E, page=P)`.
4. Inside, `make_payout_from_provider` calls `T::Currency::transfer(&staker_rewards_pot, &A, nominator_reward, Preservation::Expendable)`, which fails because crediting `A` below ED with no existing providers is rejected by the balances pallet.
5. The error is logged (`"Failed to transfer reward from pot..."`), `None` is returned, and no `Rewarded` event is emitted for `A` — but `payout_from_provider`/`do_payout_stakers_by_page` still returns `Ok(...)`.
6. The extrinsic succeeds, `ClaimedRewards` is recorded for `(E, validator_stash, page P)`, permanently preventing this page from being paid out again.
7. `nominator_reward` remains stranded in the era pot until `HistoryDepth` passes, at which point `EraRewardManager::drain` sweeps it to `T::UnclaimedRewardHandler` — never reaching `A`, the rightful beneficiary. [7](#0-6)

### Citations

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L450-477)
```rust

		// Determine whether to use dap payout or legacy path.
		let use_dap_payout =
			DisableMintingGuard::<T>::get().is_some_and(|guard_era| era >= guard_era);

		let nominator_payout_count: u32 = if use_dap_payout {
			Self::payout_from_provider(
				era,
				&stash,
				validator_staker_payout_for_page,
				&exposure,
				overview_own,
				reward_split.nominator_payout,
			)
		} else {
			Self::payout_legacy_mint(
				era,
				&stash,
				validator_staker_payout_for_page,
				&exposure,
				overview_own,
				reward_split.nominator_payout,
			)
		};

		debug_assert!(nominator_payout_count <= T::MaxExposurePageSize::get());

		Ok(Some(T::WeightInfo::payout_stakers_alive_staked(nominator_payout_count)).into())
```

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L481-516)
```rust
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

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L760-803)
```rust
	/// Transfer validator incentive from era pot to the validator's payout account.
	///
	/// This is a direct liquid transfer. Future PRs may introduce vesting via a trait.
	fn transfer_validator_incentive(era: EraIndex, stash: &T::AccountId, amount: BalanceOf<T>) {
		let Some(dest) = Self::payee(Stash(stash.clone())) else {
			Self::deposit_event(Event::<T>::Unexpected(UnexpectedKind::MissingPayee {
				era,
				stash: stash.clone(),
			}));
			return;
		};
		let Some(payout_account) = Self::payout_account_for_dest(stash, &dest) else {
			// Destination is `None`; intentional opt-out.
			return;
		};

		let incentive_pot = T::RewardPots::pot_account(crate::RewardPot::Era(
			era,
			crate::RewardKind::ValidatorSelfStake,
		));

		match T::Currency::transfer(
			&incentive_pot,
			&payout_account,
			amount,
			Preservation::Expendable,
		) {
			Ok(_) => {
				Self::deposit_event(Event::<T>::ValidatorIncentivePaid {
					era,
					validator_stash: stash.clone(),
					dest,
					amount,
				});
			},
			Err(e) => {
				log!(warn, "Failed to transfer liquid incentive: {:?}", e);
				Self::deposit_event(Event::<T>::Unexpected(
					UnexpectedKind::ValidatorIncentiveTransferFailed { era },
				));
				defensive!("Validator incentive liquid transfer failed");
			},
		}
	}
```

**File:** substrate/frame/staking-async/src/reward.rs (L156-203)
```rust
	/// Drains an era pot's remaining balance to the unclaimed reward handler.
	///
	/// The pot account itself is kept alive (provider retained) so the same slot
	/// can be reused by a future era. No-op if the pot was never created (e.g.
	/// the era ran in legacy minting mode).
	pub(crate) fn drain(era: EraIndex, kind: RewardKind) {
		let pot_account = T::RewardPots::pot_account(RewardPot::Era(era, kind));

		// Skip if pot was never created (legacy mode doesn't create pots).
		if frame_system::Pallet::<T>::providers(&pot_account) == 0 {
			return;
		}

		let remaining = T::Currency::balance(&pot_account);

		if remaining.is_zero() {
			return;
		}

		match T::Currency::withdraw(
			&pot_account,
			remaining,
			Precision::BestEffort,
			Preservation::Expendable,
			Fortitude::Force,
		) {
			Ok(credit) => {
				T::UnclaimedRewardHandler::on_unbalanced(credit);
				log!(
					debug,
					"Drained {:?} unclaimed rewards from era {:?} {:?} pot",
					remaining,
					era,
					kind
				);
			},
			Err(e) => {
				defensive!("Failed to withdraw unclaimed rewards from era pot");
				log!(
					error,
					"Era {:?} {:?}: unclaimed reward withdrawal failed: {:?}",
					era,
					kind,
					e
				);
			},
		}
	}
```
