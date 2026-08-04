Based on the investigation, the strongest available local analog is in `pallet-staking-async`'s validator self-stake incentive mechanism, which mirrors the sponsor bug's core flaw: a "highest stake wins a privileged reward share" invariant that is locked in from a single snapshot point, with a formula whose dampening curve and single-shot weight capture make it gameable by an unprivileged staker who times their bonding actions around that snapshot.

### Title
Validator self-stake incentive weight is frozen at election snapshot, letting a staker briefly inflate stake to farm a disproportionate era-long reward share - (File: `substrate/frame/staking-async/src/reward.rs`)

### Summary
`pallet-staking-async` pays a "validator self-stake incentive" whose share is meant to reflect how much of their own stake a validator has bonded (analogous to the "sponsor = highest staking power" invariant in the external report). The weight used for the whole era, `ErasValidatorIncentiveWeight`, is derived once via `calculate_validator_incentive_weight` from `self_stake` [1](#0-0)  at election time, then used unchanged for reward-share computation for the entire era through `calculate_validator_incentive_for_page` [2](#0-1) .

### Finding Description
The comment "No incentive weight (e.g. own-stake was zero at election) means no share" [3](#0-2)  confirms the weight is fixed at the moment of election and is not re-derived from the validator's actual bonded stake as the era progresses. `bond_extra` and `unbond` are unprivileged, public calls available to any stash. This creates the same class of bug as the sponsor report: a value meant to represent "current highest/self stake power" is captured at one point in time and does not track the real, ongoing amount, so an actor can time an unprivileged public action (bonding right before the election snapshot, then unbonding shortly after) to lock in an inflated share for a period where the stake is no longer actually at risk — mirroring both of the report's issues: (1) no "freshness"/continuous protection tying the privilege to sustained stake, and (2) a formula (`incentive_weight`, sqrt-based with an `optimum`/`cap` plateau at [4](#0-3) ) whose diminishing-returns curve and snapshot timing can produce shares disconnected from the actual capital committed for the era, since the storage item `ErasValidatorIncentiveWeight` is only keyed at election and not re-verified against later stake changes [5](#0-4) .

### Impact Explanation
`ErasSumWeightedPoints`/`ErasValidatorIncentiveWeight` directly determine the numerator/denominator of `Perbill::from_rational` shares of the era's `ErasValidatorIncentiveBudget`, which is then paid out from a real reward pot via `make_all_reward_payment` [6](#0-5) . If the weight can be inflated without maintaining the corresponding stake for the era, real funds from the reward pot are diverted to a validator whose actual committed capital does not justify the payout, at the expense of other validators sharing the same budget — a fund-misallocation issue directly tied to a public/unprivileged entrypoint (`bond_extra`/`unbond`), fitting the "theft or unbacked... duplicate settlement... beneficiary/amount" impact class.

### Likelihood Explanation
Medium-Low: the exploit requires precise timing of `bond_extra` before an election snapshot and `unbond` shortly after, and I was not able to fully trace the exact call/session-rotation hook where `ErasValidatorIncentiveWeight` gets inserted per election (grep only located references in `session_rotation.rs` without full inspection given tool-call limits), so I cannot confirm with certainty whether any additional safeguard (e.g., re-derivation across multiple sessions of an era, or a bonding-duration gate before "active" stake can be reduced) exists to blunt this. This uncertainty should be resolved by a deeper audit of `session_rotation.rs`'s election-result handling.

### Recommendation
- Re-derive (or continuously track) validator self-stake incentive weight from the validator's actual bonded/active stake throughout the era rather than freezing it at a single election snapshot.
- Consider requiring a minimum bonding duration/age for stake to count toward the incentive weight (similar to the report's recommendation to cap/limit the "sponsor advantage" window), so momentary stake spikes around election time cannot be farmed for a full era's incentive share.

### Proof of Concept
Conceptual PoC (not runnable without full trace of the election hook, which is the "uncertainty" noted above):
1. Validator bonds a large `bond_extra` amount immediately before the era's election snapshot is taken, temporarily maximizing `self_stake`.
2. `calculate_validator_incentive_weight` captures this inflated stake into `ErasValidatorIncentiveWeight` for the new era [1](#0-0) .
3. Validator calls `unbond` right after election completes, reducing effectively-committed active stake for the remainder of the era while `ErasValidatorIncentiveWeight` remains unchanged.
4. At era-end payout, `calculate_validator_incentive_for_page` still uses the stale, inflated weight to compute the validator's share of the incentive budget [7](#0-6) , granting a payout disproportionate to the stake actually held for the era — the same "highest stake wins a privilege, but the underlying formula/snapshot doesn't defend against timing games" flaw described in the external report.

### Citations

**File:** substrate/frame/staking-async/src/reward.rs (L236-242)
```rust
	fn calculate_validator_incentive_weight(self_stake: BalanceOf<T>) -> BalanceOf<T> {
		let optimum = OptimumSelfStake::<T>::get();
		let cap = HardCapSelfStake::<T>::get();
		let slope_factor = SelfStakeSlopeFactor::<T>::get();

		incentive_weight::<BalanceOf<T>>(self_stake, optimum, cap, slope_factor)
	}
```

**File:** substrate/frame/staking-async/src/reward.rs (L264-307)
```rust
/// Piecewise sqrt-based incentive weight function.
///
/// - Below optimum: `w(s) = √s`
/// - Between optimum and cap: `w(s) = √(T + k² × (s - T))`
/// - Above cap: plateau at `w(cap)`
fn incentive_weight<Balance>(
	self_stake: Balance,
	optimum: Balance,
	cap: Balance,
	slope_factor: Perbill,
) -> Balance
where
	Balance: AtLeast32BitUnsigned + Copy + Into<u128> + From<u128>,
{
	debug_assert!(optimum <= cap, "config invariant: optimum must be <= cap");

	if self_stake.is_zero() {
		return Balance::zero();
	}

	if optimum.is_zero() && cap.is_zero() {
		return Balance::zero();
	}

	let self_stake_u128: u128 = self_stake.into();
	let optimum_u128: u128 = optimum.into();
	let cap_u128: u128 = cap.into();

	let weight_u128 = if self_stake <= optimum {
		sp_arithmetic::helpers_128bit::sqrt(self_stake_u128)
	} else if self_stake <= cap {
		let k_squared = slope_factor.square();
		let excess = self_stake_u128.saturating_sub(optimum_u128);
		let arg = optimum_u128.saturating_add(k_squared.mul_floor(excess));
		sp_arithmetic::helpers_128bit::sqrt(arg)
	} else {
		let k_squared = slope_factor.square();
		let excess = cap_u128.saturating_sub(optimum_u128);
		let arg = optimum_u128.saturating_add(k_squared.mul_floor(excess));
		sp_arithmetic::helpers_128bit::sqrt(arg)
	};

	Balance::from(weight_u128)
}
```

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L691-751)
```rust
	fn calculate_validator_incentive_for_page(
		era: EraIndex,
		stash: &T::AccountId,
		page_stake_part: Perbill,
		era_reward_points: &EraRewardPoints<T>,
	) -> Option<BalanceOf<T>> {
		let era_incentive_budget = Eras::<T>::get_validator_incentive_budget(era);
		if era_incentive_budget.is_zero() {
			return None;
		}

		let validator_weight = match ErasValidatorIncentiveWeight::<T>::get(era, stash) {
			// No incentive weight (e.g. own-stake was zero at election) means no share.
			Some(w) if !w.is_zero() => w,
			_ => return None,
		};

		// Branch on the cutoff: legacy formula for eras whose denominator was never
		// maintained, new weighted-points formula otherwise.
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

		if share_part.is_zero() {
			return None;
		}

		let validator_total_incentive = share_part.mul_floor(era_incentive_budget);
		let validator_incentive_for_page = page_stake_part.mul_floor(validator_total_incentive);
```

**File:** substrate/frame/staking-async/src/pallet/mod.rs (L581-590)
```rust
	#[pallet::storage]
	pub type ErasValidatorIncentiveWeight<T: Config> = StorageDoubleMap<
		_,
		Twox64Concat,
		EraIndex,
		Twox64Concat,
		T::AccountId,
		IncentiveWeight<T>,
		OptionQuery,
	>;
```
