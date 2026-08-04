### Title
`pallet-oracle` returns aggregated price data with no staleness check against current time - ([File: substrate/frame/honzon/oracle/src/lib.rs])

### Summary
The oracle pallet's public read path (`Pallet::get`, `DataProvider::get`, `DataProviderExtended::get_all_values`, and the `get_value`/`all_values` view functions) returns whatever is stored in the `Values` map without ever re-validating that the stored value is still fresh relative to the *current* block time. Freshness (`expires_in`) is only enforced transiently, inside `DefaultCombineData::combine_data`, when filtering the *raw* per-operator submissions used to compute a *new* aggregate. Once an aggregate is written to `Values`, it is never expired or re-checked — it is served forever to any consumer until a new successful aggregation overwrites it. This mirrors the Chainlink "stale price" bug class: a consumer trusts a stored answer without checking its `updatedAt`/round freshness before use.

### Finding Description
`DefaultCombineData::combine_data` filters incoming raw values by `x.timestamp.saturating_add(expires_in) > now` before computing a new median, but if too few live values remain (`count < minimum_count`) it explicitly falls back to `prev_value` (the previously stored, now-possibly-stale aggregate): [1](#0-0) 

This means: if oracle operators stop feeding (e.g., they're removed from `Members`, the off-chain feeder crashes, or there is any accidental/censorship-induced slowdown), the `Values<T,I>` storage entry — including its `timestamp` — simply stops being updated and is retained indefinitely at its last value.

Crucially, `Pallet::get()` and the `DataProvider`/`DataProviderExtended` implementations do not check the stored timestamp against "now" at read time at all — they just return the storage entry as-is: [2](#0-1) [3](#0-2) 

The pallet-level view functions exposed for external/runtime consumption have the identical gap: [4](#0-3) 

The `TimestampedValue` struct does carry a `timestamp` field, so any downstream consumer *could* check freshness itself, but the pallet's own `DataProvider` trait (the interface most consuming pallets are expected to use, per `substrate/frame/honzon/oracle/README.md`) strips the timestamp entirely and returns only the raw value — making it structurally impossible for a consumer using that trait to detect staleness: [5](#0-4) 

This is configured directly in the node runtime with a 3600-unit `ExpiresIn`, i.e., the design explicitly assumes a freshness SLA of 1 hour but never enforces it on the read path: [6](#0-5) 

### Impact Explanation
Any runtime pallet that consumes oracle prices through `DataProvider::get` (the documented, recommended integration point) receives a bare value with no way to detect that the underlying data is arbitrarily stale. For price-dependent logic — e.g., collateral valuation, PSM conversion rates, liquidation triggers, or any economic accounting keyed off this oracle — an attacker or even benign network conditions that stop feed submissions (loss of quorum among `Members`, a stalled off-chain worker, or governance-driven operator churn) can cause the chain to continue operating on a frozen, arbitrarily old price indefinitely. This can lead to mispriced liquidations, incorrect collateral/backing calculations, or unbacked mint/redemption in any dependent economic pallet — directly matching the "runtime bugs that compromise intended behavior" / "theft or unbacked mint" impact classes in scope.

### Likelihood Explanation
No attacker action or privileged/malicious actor is required. This is a design gap in a public read API (`DataProvider::get`, `Pallet::get`, view functions) reachable by any consuming pallet or runtime API caller. The staleness condition can be triggered by wholly benign events (removal of a member via `ChangeMembers`, network partition of feeders, insufficient quorum after `MinimumCount` requirement is no longer met) — no adversarial coordination is needed for the stale state to persist and be consumed.

### Recommendation
- Store and check the aggregate's `timestamp` at every read: have `DataProvider::get`/`get_all_values`/`Pallet::get` compare `T::Time::now()` against the stored `TimestampedValue::timestamp` and return `None` (or a distinct stale-marker) once `timestamp + expires_in <= now`.
- Change the `DataProvider` trait (or add a companion trait) to always expose the timestamp so that consuming pallets are forced to make (and can make) a freshness decision, rather than being handed a de-timestamped value.
- Alternatively/additionally, clear (`kill`) the `Values` entry for a key once `combine_data` cannot produce a fresh aggregate (returns `None` due to insufficient live values), instead of silently preserving `prev_value` forever via the `Values` map remaining untouched.

### Proof of Concept
1. Configure `pallet_oracle::Config` with `DefaultCombineData<T, MinimumCount=3, ExpiresIn=3600>` as in `substrate/bin/node/runtime/src/lib.rs:3152-3165`.
2. Three oracle members feed a price for key `K` at time `t0`; `combine_data` succeeds (`count == minimum_count`), so `Values::<T>::insert(K, {value: P, timestamp: t0})`.
3. Governance/`ChangeMembers` removes 2 of the 3 oracle members (a normal administrative action, not privileged abuse of this pallet's own logic), or the off-chain feeders simply stop submitting.
4. Time advances past `t0 + expires_in` (e.g., to `t0 + 100000`).
5. Any pallet calls `<Oracle as DataProvider<Key, Value>>::get(&K)` (or the runtime API `get_value`) — it still returns `Some(P)`, the same value fed at `t0`, with no indication that it is now `> 27` hours stale relative to the configured 1-hour SLA. A consuming pallet computing e.g. collateral value or liquidation thresholds off this price operates on obsolete data indefinitely, with no built-in mechanism to detect or reject it.

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

**File:** substrate/frame/honzon/oracle/src/lib.rs (L313-324)
```rust
	#[pallet::view_functions]
	impl<T: Config<I>, I: 'static> Pallet<T, I> {
		/// Retrieve the aggregated oracle value for a specific key, including its timestamp.
		pub fn get_value(key: T::OracleKey) -> Option<TimestampedValueOf<T, I>> {
			Self::get(&key)
		}

		/// Retrieve every aggregated oracle value tracked by the pallet.
		pub fn all_values() -> Vec<(T::OracleKey, TimestampedValueOf<T, I>)> {
			<Values<T, I>>::iter().collect()
		}
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

**File:** substrate/frame/honzon/oracle/src/traits.rs (L29-33)
```rust
/// A simple trait for providing data.
pub trait DataProvider<Key, Value> {
	/// Returns the data for a given key.
	fn get(key: &Key) -> Option<Value>;
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
