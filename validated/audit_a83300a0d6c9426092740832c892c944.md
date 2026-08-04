### Title
Oracle price aggregation silently returns unboundedly stale data with a single fixed expiry for all assets - (File: `substrate/frame/honzon/oracle/src/default_combine_data.rs`)

### Summary
`pallet-oracle`'s default aggregation logic (`DefaultCombineData`) uses one hard-coded `ExpiresIn` staleness window and one hard-coded `MinimumCount` quorum for *every* `OracleKey` (every asset/currency pair) in a runtime, and when live submissions fall below quorum it falls back to the last aggregated `prev_value` with **no check on how old that `prev_value` itself is**. This is the exact analog of the Vader TWAP finding: a single, asset-agnostic, non-adjustable time window governs price freshness, and downstream consumers (`Pallet::get`, `DataProvider::get`) have no way to detect or reject arbitrarily stale prices.

### Finding Description
`DefaultCombineData::combine_data` first filters out raw submissions older than a fixed `ExpiresIn` moment, then requires at least `MinimumCount` fresh submissions to compute a new median; if that quorum isn't met, it unconditionally returns `prev_value` regardless of that stored value's own age: [1](#0-0) 

Both `ExpiresIn` and `MinimumCount` are compile-time `Get<...>` associated types fixed once in the runtime's `Config`, applied uniformly across all `OracleKey`s regardless of the underlying asset's volatility — mirroring the Vader bug where `_updatePeriod` was a single immutable value applied to every TWAP oracle instance: [2](#0-1) 

Consumers of the oracle (`Pallet::get`, `DataProviderExtended::get_all_values`, and the `DataProvider` trait implementation) return only the `TimestampedValue` without any freshness enforcement at the read boundary: [3](#0-2) [4](#0-3) 

So if operators stop feeding (or are prevented/incentivized not to feed, e.g. during a volatile market event) for an asset, quorum is never reached again, and `combine_data` keeps returning the same stale `prev_value` indefinitely — there is no maximum age check on the aggregated value itself, only on the raw per-operator submissions used to *compute a new* aggregate.

### Impact Explanation
Any pallet built on top of `pallet-oracle`'s `DataProvider`/`DataProviderExtended` trait for pricing (the pallet was introduced specifically to support "Polkadot Stablecoin on AssetHub", per its PRDoc) will treat an unboundedly stale price as current truth whenever quorum submissions lapse, since neither the pallet's storage nor its read API expose or enforce a staleness bound on `Values`. A consuming pallet that mints, redeems, or liquidates based on this price would settle transactions at a wrong, stale valuation — a runtime bug that compromises intended pricing behavior and can lead to wrong-amount settlement for any downstream asset-accounting logic, exactly the risk class flagged in the source report (asset-specific volatility not reflected in a single fixed staleness/period parameter, and no per-asset override).

### Likelihood Explanation
Reaching this state requires no privileged actor: it only requires the population of independent `Members` submitting fresh feeds for a given `OracleKey` to drop below `MinimumCount` within one `ExpiresIn` window — a routine, unprivileged (non-malicious) liveness condition (e.g., an asset losing relevance, an operator outage, or a temporarily illiquid/volatile pair where operators pause quoting). No attacker action or governance/admin misuse is needed; the flaw is structural in `combine_data`'s fallback logic and the single global `Get` parameters.

### Recommendation
- Track and expose the timestamp of the currently-stored aggregate (`Values`) so consumers can reject data older than an acceptable bound, rather than only filtering raw per-operator inputs.
- Do not unconditionally fall back to `prev_value` in `combine_data`; return `None` (or otherwise signal staleness) once `prev_value.timestamp.saturating_add(expires_in) <= now`.
- Allow `ExpiresIn`/`MinimumCount` (or equivalent freshness policy) to be configured per `OracleKey` rather than as a single runtime-wide constant, so volatile vs. stable assets can have different staleness tolerances, matching the report's second recommendation for per-asset configurability.

### Proof of Concept
1. Configure `pallet-oracle` with `DefaultCombineData<Runtime, ConstU32<5>, ConstU64<3600>>` as in `substrate/bin/node/runtime/src/lib.rs:3154`, and register 5 `Members`.
2. All 5 members feed a price for `key = X` at time `t0`; `combined()` succeeds and `Values::<T>::insert(X, ...)` stores `TimestampedValue { value: p0, timestamp: t0 }`.
3. After `t0 + 3600`, only 2 members (< `MinimumCount = 5`) continue feeding `X`; the other members stop (no malicious action needed — simple inactivity/outage).
4. On every subsequent `feed_values` call for `X`, `combine_data` filters out the now-expired raw entries, count drops below `MinimumCount`, and the function returns `prev_value` — the `t0` price — forever, with `Values::<T>::get(X)` never advancing and never being flagged stale to any caller of `Pallet::get(&X)` or `DataProvider::get(&X)`.

### Citations

**File:** substrate/frame/honzon/oracle/src/default_combine_data.rs (L38-59)
```rust
	fn combine_data(
		_key: &<T as Config<I>>::OracleKey,
		mut values: Vec<TimestampedValueOf<T, I>>,
		prev_value: Option<TimestampedValueOf<T, I>>,
	) -> Option<TimestampedValueOf<T, I>> {
		let expires_in = ExpiresIn::get();
		let now = T::Time::now();

		values.retain(|x| x.timestamp.saturating_add(expires_in) > now);

		let count = values.len() as u32;
		let minimum_count = MinimumCount::get();
		if count < minimum_count || count == 0 {
			return prev_value;
		}

		let mid_index = count / 2;
		// Won't panic as `values` ensured not empty.
		let (_, value, _) =
			values.select_nth_unstable_by(mid_index as usize, |a, b| a.value.cmp(&b.value));
		Some(value.clone())
	}
```

**File:** substrate/bin/node/runtime/src/lib.rs (L3152-3165)
```rust
impl pallet_oracle::Config for Runtime {
	type OnNewData = ();
	type CombineData = pallet_oracle::DefaultCombineData<Self, ConstU32<5>, ConstU64<3600>>;
	type Time = Timestamp;
	type OracleKey = u32;
	type OracleValue = u128;
	type PalletId = OraclePalletId;
	type Members = TechnicalMembership;
	type WeightInfo = ();
	type MaxHasDispatchedSize = OracleMaxHasDispatchedSize;
	type MaxFeedValues = OracleMaxFeedValues;
	#[cfg(feature = "runtime-benchmarks")]
	type BenchmarkHelper = OracleBenchmarkingHelper;
}
```

**File:** substrate/frame/honzon/oracle/src/lib.rs (L395-398)
```rust
	/// Returns the aggregated and timestamped value for a given key.
	pub fn get(key: &T::OracleKey) -> Option<TimestampedValueOf<T, I>> {
		Self::values(key)
	}
```

**File:** substrate/frame/honzon/oracle/src/lib.rs (L449-460)
```rust
impl<T: Config<I>, I: 'static> DataProvider<T::OracleKey, T::OracleValue> for Pallet<T, I> {
	fn get(key: &T::OracleKey) -> Option<T::OracleValue> {
		Self::get(key).map(|timestamped_value| timestamped_value.value)
	}
}
impl<T: Config<I>, I: 'static> DataProviderExtended<T::OracleKey, TimestampedValueOf<T, I>>
	for Pallet<T, I>
{
	fn get_all_values() -> impl Iterator<Item = (T::OracleKey, Option<TimestampedValueOf<T, I>>)> {
		<Values<T, I>>::iter().map(|(k, v)| (k, Some(v)))
	}
}
```
