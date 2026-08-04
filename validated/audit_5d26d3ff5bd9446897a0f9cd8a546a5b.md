### Title
Per-recipient staking reward transfer failures are silently swallowed, permanently losing nominator payouts while the era page is marked as paid - (File: `substrate/frame/staking-async/src/pallet/impls.rs`)

### Summary
The external report's core invariant is: a value-transfer call whose return/result is not checked can fail silently, letting the caller believe funds moved when they didn't, corrupting downstream accounting. The same broken invariant exists in `pallet-staking-async`'s reward-payout path: `make_payout_from_provider` performs a `T::Currency::transfer` from an era reward pot to a nominator/validator payout account, and if that transfer errors, the failure is only logged and the function returns `None` — the caller (`payout_from_provider`) simply skips the recipient and moves on. The enclosing extrinsic (`payout_stakers_*`) still returns `Ok(...)`, the `PayoutStarted`/page-claimed bookkeeping for that era/validator/page has already advanced, and there is no retry or compensating mechanism, so the reward for that recipient is permanently lost.

### Finding Description
`Pallet::<T>::make_payout_from_provider` (`substrate/frame/staking-async/src/pallet/impls.rs:578-630`) transfers a computed reward amount from the `staker_rewards_pot` to the resolved `payout_account` using `Preservation::Expendable`: [1](#0-0) 

If `T::Currency::transfer` returns an `Err` (e.g. the payout account cannot receive the amount, the pot's remaining balance would violate a provider-reference/consumer constraint, or any other fallible currency condition), the code path only logs an error and returns `None`: [2](#0-1) 

The caller, `payout_from_provider`, treats `None` exactly like "reward was zero" — it simply doesn't count or emit an event for that nominator, and continues to the next one: [3](#0-2) 

Crucially, the surrounding extrinsic dispatch (`payout_stakers_alive_staked`) still reports `Ok(...)` for the whole call, and the era/validator/page progression (`PayoutStarted` event and the "next claimable page" cursor) has already been recorded before this per-nominator loop runs, e.g.: [4](#0-3) 

Because pages are claimed monotonically (there is no mechanism to re-claim a page once its cursor has advanced), a transfer failure for any individual nominator inside that page is unrecoverable: the pot's balance is retained (transfer didn't happen) but the page is marked as paid, so the reward can never be claimed again by that nominator through the normal `payout_stakers` flow.

### Impact Explanation
This directly matches the accepted impact class "balances/staking payouts must conserve value and settle exactly once to the rightful beneficiary and amount" — here, on transfer failure, the payout settles to *nobody*, yet the runtime state treats the page as fully paid. Funds intended for a nominator are permanently stuck in the era reward pot with no code path to recover or re-deliver them to the affected staker, an unbacked-value/fund-lock condition triggered by ordinary, unprivileged calls to `payout_stakers`/`payout_stakers_by_page`.

### Likelihood Explanation
No privileged actor is required — any account can call the public `payout_stakers` extrinsic. Currency transfer failures are not exotic in this position (differing existential-deposit/consumer-reference states, `Preservation::Expendable` edge cases, or an intentionally near-empty payout account) and the code explicitly anticipates them (it already has an error-logging branch), showing the failure path is reachable in production, not merely theoretical.

### Recommendation
Do not advance the page-claimed / "next claimable page" state for a page unless every payout within it either succeeds or is deterministically and re-claimably deferred. Either:
- Fail the whole extrinsic (propagate the transfer error) so the page is not marked claimed and the caller can retry, or
- Record unpaid amounts in a dedicated "pending/unclaimed reward" storage item per (era, page, nominator) that can be retried/claimed later, mirroring `PayRewardFromAccount`'s pattern of leaving state changes reversible on `PaymentProcedure` failure.

### Proof of Concept
1. Construct a scenario where a nominator's `payout_account` (from `RewardDestination::Account`) is configured such that `T::Currency::transfer(&staker_rewards_pot, &payout_account, amount, Preservation::Expendable)` returns `Err` (e.g., account has a consumer/provider reference imbalance that makes it unable to accept the incoming balance under the runtime's account-existence rules).
2. Call `payout_stakers`/`payout_stakers_by_page` for that era/validator/page as any unprivileged signed account.
3. Observe: extrinsic returns `Ok`, `PayoutStarted` is emitted, the page's claimed cursor advances, but no `Rewarded` event is emitted for the failing nominator and their balance is unchanged.
4. Attempt to re-claim the same era/page for that nominator — it is rejected because the page is already recorded as claimed, permanently losing that nominator's share of the era reward. [5](#0-4)

### Citations

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L440-478)
```rust
		// Pay validator incentive bonus from the separate incentive pot.
		// Emits `ValidatorIncentivePaid` event inside `transfer_validator_incentive`.
		if let Some(incentive) = Self::calculate_validator_incentive_for_page(
			era,
			&stash,
			page_stake_part,
			&era_reward_points,
		) {
			Self::transfer_validator_incentive(era, &stash, incentive);
		}

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
	}
```

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L491-516)
```rust
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

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L598-616)
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

**File:** substrate/frame/staking/src/pallet/impls.rs (L352-358)
```rust
		Self::deposit_event(Event::<T>::PayoutStarted {
			era_index: era,
			validator_stash: stash.clone(),
			page,
			next: EraInfo::<T>::get_next_claimable_page(era, &stash, &ledger),
		});

```
