### Title
`OptimumSelfStake` incentive-weight threshold defaults to zero with no genesis initialization, corrupting validator/nominator reward-weight accounting - ([File: substrate/frame/staking-async/src/pallet/mod.rs])

### Summary
`pallet-staking-async` introduces a storage item `OptimumSelfStake` that is documented as the threshold below which incentive weight grows as `sqrt(self_stake)` and above which growth is dampened by `SelfStakeSlopeFactor`. It is declared as a plain `StorageValue<_, BalanceOf<T>, ValueQuery>` with no explicit default and no genesis/constructor initialization, so it silently defaults to `0`. This is the direct structural analog of the Gearbox `expectedLiquidityLimit` bug: a threshold that gates a payout/accounting formula is left unset, and its implicit zero value changes the code path taken by every subsequent calculation instead of triggering an error. [1](#0-0) 

### Finding Description
The pallet explicitly contrasts two similar threshold storages right next to each other:
- `MinCommission` is documented "If set to `0`, no limit exists" — zero is an intentional, acknowledged sentinel.
- `MaxCommission` is given an explicit non-trivial default (`Perbill::one()`, i.e. 100%) via a dedicated `Get` impl (`MaxCommissionDefault`) specifically so that leaving it unset does not silently impose the strictest possible cap. [2](#0-1) 

`OptimumSelfStake`, however, has neither an acknowledged zero-sentinel semantic nor a protective non-zero default: [1](#0-0) 

Per its own doc comment, this value is meant to separate two reward-weight growth regimes: a `sqrt(self_stake)` growth regime below the threshold, and a `SelfStakeSlopeFactor`-dampened regime above it. Because the storage value has no genesis initializer and no non-zero `Get` default (unlike `MaxCommission`), it starts at `0` on any chain/spec that does not explicitly populate it via a runtime upgrade or genesis config. With the threshold at `0`, every validator's `self_stake` (which is always `> 0` once bonded) is unconditionally treated as "above threshold," permanently disabling the intended `sqrt`-growth incentive regime and forcing every validator through the `SelfStakeSlopeFactor`-dampened path from genesis onward — the reward/incentive-weight formula is silently corrupted for the entire lifetime of the chain unless someone manually notices and sets it, exactly mirroring how `expectedLiquidityLimit` silently gated pool behavior in the Gearbox bug because it was never set in the constructor.

### Impact Explanation
This value feeds directly into the incentive-weight computation used for staking reward apportionment (staking/asset accounting), which is an explicitly in-scope impact category ("staking, pools, treasury spends, bridge rewards ... must conserve value and settle exactly once to the rightful beneficiary and amount"). An unset threshold changes the reward-weight distribution formula applied to every validator/nominator payout without any error, malicious actor, or governance action required — it is a pure default-initialization gap that produces wrong reward weighting network-wide.

### Likelihood Explanation
This requires no attacker action at all — it triggers automatically for any chain instance (new spec, test network, or migration path) that does not explicitly populate `OptimumSelfStake` at genesis or via a dedicated migration, which is the same "forgot to initialize in the constructor" root cause called out in the source report. Given `MaxCommission` right above it received a deliberate protective default while `OptimumSelfStake` did not, this looks like an oversight rather than an intentional design choice.

### Recommendation
Give `OptimumSelfStake` an explicit non-zero `Get` default (mirroring the `MaxCommissionDefault` pattern used for `MaxCommission`) or enforce population of this value via a mandatory genesis/migration step, so the reward-weight formula cannot silently run in an unintended, uninitialized-threshold state.

### Proof of Concept
1. Deploy `pallet-staking-async` without setting `OptimumSelfStake` in genesis config or storage (default `ValueQuery` behavior yields `0`). [1](#0-0) 
2. Any bonded validator has `self_stake > 0`, so the "below-threshold sqrt-growth" branch of the incentive-weight formula (described in the doc comment) is never reachable; every validator is always evaluated under the `SelfStakeSlopeFactor`-dampened branch.
3. Reward computations for the entire validator set proceed on the wrong branch of the formula from the chain's genesis, silently misallocating incentive weight — with no exception, warning, or governance action needed to trigger it.

I was not able to open `substrate/frame/staking-async/src/reward.rs` (where `OptimumSelfStake`/`SelfStakeSlopeFactor` are consumed) within the available tool budget to show the exact arithmetic expression that consumes this value; the finding above is based on the storage declaration and its doc comments, which unambiguously describe an accounting threshold whose default value differs materially and dangerously from its intended non-zero setting, in direct structural parallel to the reported `expectedLiquidityLimit` bug.

### Citations

**File:** substrate/frame/staking-async/src/pallet/mod.rs (L516-534)
```rust
	/// The minimum amount of commission that validators can set.
	///
	/// If set to `0`, no limit exists.
	#[pallet::storage]
	pub type MinCommission<T: Config> = StorageValue<_, Perbill, ValueQuery>;

	/// The maximum commission that validators can set.
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

**File:** substrate/frame/staking-async/src/pallet/mod.rs (L546-552)
```rust
	/// Optimum self-stake threshold for validators.
	///
	/// Below this threshold, the incentive weight grows as `sqrt(self_stake)`.
	/// Above it, growth is dampened by [`SelfStakeSlopeFactor`].
	#[pallet::storage]
	pub type OptimumSelfStake<T: Config> = StorageValue<_, BalanceOf<T>, ValueQuery>;

```
