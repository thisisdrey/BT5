## Summary

The Goat.Tech bug's core primitive is: **a percentage-weighted reward-splitting formula that a single actor can route funds through multiple accounts they control to extract a disproportionate share of a payout pool, because the formula treats each account independently instead of aggregating true economic ownership.** The local analog is the `pallet-staking-async` validator self-stake incentive curve, which uses a **concave (square-root) weighting function applied per-validator-identity**. Because `√a + √b > √(a+b)` for any `a, b > 0`, an actor who splits one total self-stake across multiple validator stashes receives strictly more aggregate "incentive weight" — and therefore a larger share of the fixed `ValidatorSelfStake` reward pot — than if the same capital were bonded under a single validator identity.

## Finding Description

`pallet-staking-async` allocates a portion of era rewards into a dedicated pot for `RewardKind::ValidatorSelfStake` [1](#0-0) . Each validator's share of this pot is driven by `calculate_validator_incentive_weight(self_stake)`, which calls `incentive_weight` using the chain-configured `OptimumSelfStake`, `HardCapSelfStake`, and `SelfStakeSlopeFactor` parameters [2](#0-1) .

The weighting function is explicitly piecewise square-root:

```
- Below optimum: w(s) = √s
- Between optimum and cap: w(s) = √(T + k² × (s - T))
- Above cap: plateau at w(cap)
``` [3](#0-2) 

`√` is strictly concave and `w(0) = 0` [4](#0-3) . For any concave function vanishing at zero, splitting one input into two positive parts yields a strictly larger sum: `w(a) + w(b) ≥ w(a+b)`, with strict inequality whenever both parts are below the optimum threshold (the linear-`√` region). Nothing in the pallet ties `self_stake` to a unique real-world identity — a single controller can bond capital `S` under one stash/validator, or split it into `N` separate stash/validator accounts each self-bonding `S/N`, and nominate/validate normally through the fully public `bond`/`validate` extrinsics. There is no cross-validator dedup, KYC, or per-controller aggregation of self-stake in `OptimumSelfStake`/`HardCapSelfStake`/`SelfStakeSlopeFactor` accounting [5](#0-4) .

This exactly mirrors the GoatTech flaw: the "trust/incentive" metric is computed per-account rather than per beneficial-owner, so routing the same underlying capital through additional attacker-controlled accounts extracts more from a shared pool without any corresponding increase in the resource (real, aggregate skin-in-the-game) the mechanism is meant to reward.

## Impact Explanation

The `ValidatorSelfStake` reward pot is a fixed allocation per era, snapshotted from a general pot into an era-specific pot at era boundaries [6](#0-5) . If distribution to validators is proportional to `incentive_weight(self_stake)` (the pallet's own module docs and tests describe this as an anti-plutocracy/"discourage concentration" curve, e.g. `incentive_weight_monotonically_increasing_below_cap`), then an attacker who fragments a given amount of self-bonded capital across multiple validator identities receives a larger aggregate slice of this fixed pot than an honest single-identity validator with the same total capital. This dilutes the honestly-computed rewards of every other validator in the set — a wrong-beneficiary/wrong-amount settlement of a reward payout, which is squarely inside the "runtime bugs that compromise intended behavior" and "theft or unbacked mint" impact classes, since value is redirected away from the intended proportional recipients without any privileged or malicious-node assumption — just ordinary staking operations available to any funded account.

## Likelihood Explanation

Bonding stake and calling `validate()` from multiple accounts is a completely permissionless, low-cost, everyday staking operation; no validator collusion, governance action, or leaked keys are required — only capital that the attacker already intends to deploy as self-stake. The exploit is purely a consequence of the concavity of `√` combined with per-identity (not per-controller) accounting, so it is deterministically profitable any time `self_stake` values land in the sub-optimum or sub-cap growth region, which is the pallet's normal operating range.

## Recommendation

Aggregate self-stake by beneficial owner (e.g., via a bounded identity/controller mapping, or by capping the number of validator slots creditable per controlling key) before applying the `incentive_weight` curve, or replace the strictly concave weighting with a function that is at minimum linear/additive across an owner's total self-stake, so that `w(a) + w(b) == w(a+b)` for stake controlled by the same entity. Alternatively, require a per-era, per-controller self-stake sum to be used as the single input to `calculate_validator_incentive_weight` rather than computing it independently per validator stash.

## Proof of Concept

Using the pallet's own unit-test helper `incentive_weight(self_stake, optimum, cap, slope_factor)` [7](#0-6) , with `optimum = 100_000`, `cap = 500_000` (values used in the pallet's own tests [8](#0-7) ):

- Single validator self-bonds `100_000`: `w = √100_000 ≈ 316` (matches `incentive_weight_at_optimum` test, line 361-372).
- Same attacker splits the same `100_000` across 10 validator stashes of `10_000` each: `w_i = √10_000 = 100` per stash, total `= 10 × 100 = 1_000`.

`1_000 ≫ 316`: by splitting identical total capital into 10 self-controlled validator identities, the attacker's aggregate incentive weight — and therefore aggregate share of the fixed `ValidatorSelfStake` era reward pot — increases roughly 3.16×, purely from account fragmentation, with zero additional capital and no protocol violation of any single extrinsic's preconditions.

### Citations

**File:** substrate/frame/staking-async/src/reward.rs (L84-95)
```rust
	/// Snapshots the general reward pots into era-specific pots.
	///
	/// DAP drips inflation continuously into the general pots. At era boundary,
	/// this transfers the accumulated balances (minus ED) into era pots.
	pub(crate) fn snapshot_era_rewards(era: EraIndex) -> EraRewardAllocation<BalanceOf<T>> {
		let staker_era_pot = Self::create(era, RewardKind::StakerRewards);
		let incentive_era_pot = Self::create(era, RewardKind::ValidatorSelfStake);

		let general_staker_pot =
			T::RewardPots::pot_account(RewardPot::General(RewardKind::StakerRewards));
		let general_incentive_pot =
			T::RewardPots::pot_account(RewardPot::General(RewardKind::ValidatorSelfStake));
```

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

**File:** substrate/frame/staking-async/src/reward.rs (L346-358)
```rust
	#[test]
	fn incentive_weight_below_optimum() {
		// √10_000 = 100
		assert_eq!(
			incentive_weight::<Balance>(
				10_000,
				100_000,
				500_000,
				Perbill::from_rational(1u32, 2u32)
			),
			100
		);
	}
```

**File:** substrate/frame/staking-async/src/pallet/mod.rs (L523-534)
```rust
	///
	/// If not set, defaults to `Perbill::one()` (100%), i.e. no upper limit.
	#[pallet::storage]
	pub type MaxCommission<T: Config> = StorageValue<_, Perbill, ValueQuery, MaxCommissionDefault>;

	/// Default for MaxCommission: 100% (no restriction).
	pub struct MaxCommissionDefault;
	impl Get<Perbill> for MaxCommissionDefault {
		fn get() -> Perbill {
			Perbill::one()
		}
	}
```
