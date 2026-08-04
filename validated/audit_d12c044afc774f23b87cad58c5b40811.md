## Title
`Perbill::from_rational` silently returns 100% on zero-stake denominator, allowing duplicate/overpaid nominator rewards in `payout_from_provider`/`payout_legacy_mint` - (File: `substrate/frame/staking-async/src/pallet/impls.rs`)

### Summary
The external report's core invariant break is: a division-by-zero path that a formal spec says "MUST NOT" corrupt/revert behavior, because the numerator/denominator relationship (`totalAssets`/`totalDebt`) can legitimately collapse to `0/0`. The Substrate analog is `Perbill::from_rational`, whose `PerThing::from_rational` implementation does **not** propagate the division-by-zero error but silently defaults to `Self::one()` (100%) when the denominator is zero: [1](#0-0) 

This is used unguarded to compute each nominator's share of an era's reward pot in `payout_from_provider` and `payout_legacy_mint`: [2](#0-1) [3](#0-2) 

### Finding Description
`total_nominator_stake = exposure.total().saturating_sub(overview_own)` is used as the denominator for `Perbill::from_rational(nominator.value, total_nominator_stake)` for **every** nominator entry in `exposure.others()`. If `total_nominator_stake` is `0` (e.g. `overview_own` — the validator's full self-stake from `ErasStakersOverview` — happens to equal or exceed `exposure.total()` for the exposure snapshot used at payout time, which can occur due to page/overview desynchronization since `overview_own` is read from the overview independently of the specific page's `exposure.total()`), `PerThing::from_rational` does not error out; per its documented contract it silently returns `Self::one()`, i.e. **100%**, for `nominator.value / 0`: [4](#0-3) 

Because this happens inside a loop over `exposure.others()`, **every nominator entry** in that page would compute `nominator_exposure_part = 100%`, and thus each would receive `nominator_exposure_part.mul_floor(total_nominator_payout)` = the *entire* `total_nominator_payout`, not a proportional share. This is a duplicate/over-settlement of payout to multiple parties from a single reward budget — the exact class of "duplicate settlement or payout" flagged as in-scope in the impact gate.

This contrasts with how the legacy `pallet-staking` computes the analogous `validator_exposure_part`/`page_stake_part`, and with the nomination-pools pallet's `point_to_balance`, which **explicitly guards** the zero case before doing any ratio math: [5](#0-4) 

No equivalent explicit `is_zero()` guard exists before the `Perbill::from_rational(nominator.value, total_nominator_stake)` call in `staking-async`'s payout path, unlike the `calculate_validator_incentive_for_page` function elsewhere in the same file which does defensively check `sum_weighted_points.is_zero()` / `total_weight.is_zero()` before calling `Perbill::from_rational`: [6](#0-5) 

### Impact Explanation
If `total_nominator_stake` reaches zero while `exposure.others()` is non-empty, the reward-splitting logic silently mis-assigns 100% of the nominator reward pot to each individual nominator in the page instead of a proportional fraction. Depending on how the reward pot is funded (`payout_from_provider` transfers from an era pot; `payout_legacy_mint` mints new currency into the imbalance), this manifests either as an unbacked mint of tokens (legacy path, directly violating token-supply conservation) or as the era reward pot being drained beyond its intended budget / running out mid-loop for later nominators in the page (transfer path). Both outcomes correspond directly to "theft or unbacked mint" and "duplicate settlement or payout" in the accepted impact list.

### Likelihood Explanation
This requires the specific state where `overview_own >= exposure.total()` for the page-exposure snapshot fetched at payout time, which is not the everyday case, but the code contains no defensive check against it despite the surrounding function (`calculate_validator_incentive_for_page`) demonstrating the authors are aware such zero-denominator states can occur from storage/overview mismatches and explicitly guard against them there. The absence of the same guard in `payout_from_provider`/`payout_legacy_mint` is an inconsistency in the codebase's own defensive-programming pattern, and the underlying primitive (`PerThing::from_rational`) is documented to swallow the zero-division case as "100%" rather than erroring, which is a silent-failure trap that is easy to overlook exactly as it was overlooked in the original ERC-4626 report.

### Recommendation
Add an explicit `total_nominator_stake.is_zero()` guard before computing `Perbill::from_rational(nominator.value, total_nominator_stake)` in both `payout_from_provider` and `payout_legacy_mint`, mirroring the pattern already used in `calculate_validator_incentive_for_page` (emit an `Unexpected`/defensive event and skip payout, or use `checked_from_rational`-style APIs that surface the error) rather than relying on `Perbill::from_rational`'s implicit "returns 100% instead of erroring on zero denominator" behavior.

### Proof of Concept
Conceptual reproduction (exact storage manipulation would need to be verified against `ErasStakersOverview`/paged-exposure construction invariants in a live testnet/Devin session):
1. Get a validator elected with a paged exposure (`MaxExposurePageSize` > 1 nominator, multiple pages).
2. Manufacture or exploit a storage-consistency window where `ErasStakersOverview::<T>::get(era, &stash).own` (`overview_own`) is recorded as equal to (or greater than) `exposure.total()` returned by `Eras::<T>::get_paged_exposure(era, &stash, page)` for a specific page (e.g. via era transition timing, page recomputation, or an overview update that races with paged-exposure snapshotting).
3. Call `payout_stakers`/`payout_stakers_by_page` for that `(era, stash, page)`.
4. Observe `total_nominator_stake = exposure.total().saturating_sub(overview_own) == 0`.
5. For each nominator in `exposure.others()`, `Perbill::from_rational(nominator.value, 0)` returns `Perbill::one()` (per `substrate/primitives/arithmetic/src/per_things.rs` lines 384-391), so each nominator's computed reward equals the full `total_nominator_payout` rather than a fraction — resulting in over-issuance across the page's nominators.

Note: I was not able to fully trace, within the available indexed code, the precise sequence of storage writes that produces `overview_own >= exposure.total()` for a given page (this requires deeper cross-referencing of `ErasStakersOverview` population vs. `get_paged_exposure` construction across era-transition code, which the index did not fully surface). This should be validated with a live Devin session with full repository and test-harness access before treating this as a confirmed exploitable path rather than a design gap around an unguarded zero-denominator `Perbill::from_rational` call.

### Citations

**File:** substrate/primitives/arithmetic/src/per_things.rs (L384-391)
```rust
	#[must_use]
	fn from_rational<N>(p: N, q: N) -> Self
	where
		N: RationalArg + TryInto<Self::Inner> + TryInto<Self::Upper>,
		Self::Inner: Into<N>,
	{
		Self::from_rational_with_rounding(p, q, Rounding::Down).unwrap_or_else(|_| Self::one())
	}
```

**File:** substrate/primitives/arithmetic/src/per_things.rs (L393-399)
```rust
	/// Approximate the fraction `p/q` into a per-thing fraction.
	///
	/// The computation of this approximation is performed in the generic type `N`. Given
	/// `M` as the data type that can hold the maximum value of this per-thing (e.g. `u32` for
	/// `Perbill`), this can only work if `N == M` or `N: From<M> + TryInto<M>`.
	///
	/// In the case of an overflow (or divide by zero), an `Err` is returned.
```

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L496-516)
```rust
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

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L539-561)
```rust
		let total_nominator_stake = exposure.total().saturating_sub(overview_own);
		for nominator in exposure.others().iter() {
			let nominator_exposure_part =
				Perbill::from_rational(nominator.value, total_nominator_stake);
			let nominator_reward: BalanceOf<T> =
				nominator_exposure_part.mul_floor(total_nominator_payout);

			if let Some((imbalance, dest)) =
				Self::make_payout_legacy(era, &nominator.who, nominator_reward)
			{
				nominator_payout_count.saturating_inc();
				Self::deposit_event(Event::<T>::Rewarded {
					stash: nominator.who.clone(),
					dest,
					amount: imbalance.peek(),
				});
				total_imbalance.subsume(imbalance);
			}
		}

		T::Reward::on_unbalanced(total_imbalance);
		nominator_payout_count
	}
```

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L710-744)
```rust
		let share_part = if Eras::<T>::uses_weighted_points(era) {
			// This validator has non-zero weight (checked above) and reached this point only
			// with non-zero reward points (gated by the caller), so it must have contributed
			// to the denominator. A zero denominator with a live budget is therefore a storage
			// inconsistency and is surfaced rather than silently paying nothing.
			let sum_weighted_points = ErasSumWeightedPoints::<T>::get(era);
			if sum_weighted_points.is_zero() {
				log!(warn, "Sum of weighted points is zero but budget exists for era {}", era);
				Self::deposit_event(Event::<T>::Unexpected(
					UnexpectedKind::ValidatorIncentiveWeightMismatch { era },
				));
				return None;
			}
			let validator_points: RewardPoint =
				era_reward_points.individual.get(stash).copied().unwrap_or(0);
			let numerator = validator_weight.saturating_mul(BalanceOf::<T>::from(validator_points));
			Perbill::from_rational(numerator, sum_weighted_points)
		} else {
			// Legacy stake-only share, denominated by the total incentive weight across all
			// elected validators. A zero denominator with a non-zero budget is a storage
			// inconsistency, so it is surfaced rather than silently paying nothing.
			let total_weight = ErasSumValidatorIncentiveWeight::<T>::get(era);
			if total_weight.is_zero() {
				log!(
					warn,
					"Total validator incentive weight is zero but budget exists for era {}",
					era
				);
				Self::deposit_event(Event::<T>::Unexpected(
					UnexpectedKind::ValidatorIncentiveWeightMismatch { era },
				));
				return None;
			}
			Perbill::from_rational(validator_weight, total_weight)
		};
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L3503-3522)
```rust
	fn point_to_balance(
		current_balance: BalanceOf<T>,
		current_points: BalanceOf<T>,
		points: BalanceOf<T>,
	) -> BalanceOf<T> {
		let u256 = T::BalanceToU256::convert;
		let balance = T::U256ToBalance::convert;
		if current_balance.is_zero() || current_points.is_zero() || points.is_zero() {
			// There is nothing to unbond
			return Zero::zero();
		}

		// Equivalent of (current_balance / current_points) * points
		balance(
			u256(current_balance)
				.saturating_mul(u256(points))
				// We check for zero above
				.div(u256(current_points)),
		)
	}
```
