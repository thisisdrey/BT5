### Title
Oracle Pallet Silently Serves Stale, Unbounded-Age Price Data via `DataProvider::get()` — ([File: substrate/frame/honzon/oracle/src/lib.rs])

### Summary
The `honzon` Oracle pallet is documented as providing "Timestamped Data: All submitted data includes timestamps for freshness tracking" [1](#0-0) , but the public `DataProvider` trait implementation that downstream pallets are expected to use for pricing/valuation strips the timestamp entirely and can return a value that is arbitrarily old, with no staleness bound enforced at read time. This is the exact bug class from the external report: price data is consumed without validating that it is up to date.

### Finding Description
Raw oracle submissions are timestamped and aggregated through `CombineData`. The default implementation, `DefaultCombineData`, filters out individual raw votes older than `ExpiresIn` before computing the median: [2](#0-1) 

Critically, when the number of still-fresh raw values drops below `MinimumCount` (e.g. all oracle operators stop feeding, `count == 0`), the function does not invalidate the previous aggregate — it returns `prev_value` unconditionally: [3](#0-2) 

That stale `prev_value` is then written straight back into the `Values` storage map every time any single member feeds any key, via `do_feed_values`: [4](#0-3) 

There is no `on_finalize`/expiry hook that clears or invalidates `Values` when it goes stale — the only cleanup hook that exists resets `HasDispatched`, not `Values`: [5](#0-4) 

Finally, and most importantly, the standard `DataProvider` trait — the interface downstream consumer pallets are expected to call for pricing — discards the timestamp entirely: [6](#0-5) 

So a caller using `DataProvider::get(key)` (the generic, most commonly implemented consumption path per the trait definition in `traits.rs`) [7](#0-6)  has no way to know the returned value might be minutes, hours, or indefinitely old. Only the separate `get_value`/`DataProviderExtended` path exposes the timestamp [8](#0-7) , but nothing forces a consumer to use it, and the pallet's own `feed_values` extrinsic keeps re-committing the stale value to storage without a chain-enforced upper bound on age.

This mirrors the Chainlink report precisely: the on-chain price store never enforces "is this data still fresh" at the point of consumption — freshness filtering exists only transiently during aggregation, and a stale value, once cached in `Values`, persists and is served forever via the primary `DataProvider` interface.

### Impact Explanation
The Oracle pallet is shipped as live code in this polkadot-sdk snapshot (referenced from `umbrella/Cargo.toml`, `umbrella/src/lib.rs`, and `substrate/bin/node/runtime/src/lib.rs`), and its explicit design goal is to feed "critical operations" needing fresh pricing/valuation data for consumer runtime modules (e.g., collateral valuation, liquidation triggers in Acala-style CDP/lending logic that this oracle was built to serve). Any runtime that wires a consumer pallet through the plain `DataProvider` trait (the interface this crate provides for that exact purpose) will silently operate on unboundedly stale prices whenever oracle-operator liveness degrades (network partition, operator downtime, griefing by simply not submitting) — this requires no malicious peer, validator, or privileged actor, only ordinary liveness gaps. Consequences for a consuming runtime include mispriced collateral, wrongful liquidations/non-liquidations, and incorrect asset accounting — directly hitting the "asset accounting" and "runtime bugs that compromise intended behavior" impact categories.

### Likelihood Explanation
High: the condition is a straightforward liveness edge case, not an adversarial exploit requiring privileged access. As soon as fresh submissions fall below `MinimumCount` (a routine scenario — e.g., a subset of operators go offline), `combine_data` returns `prev_value`, which gets persisted again on the next `feed_values` call and is served indefinitely via `DataProvider::get`. No governance action, forged signature, or malicious peer is needed.

### Recommendation
- Bound the age of `Values<T, I>` entries: when `combine_data` cannot produce a fresh aggregate (`count < minimum_count`), clear the `Values` entry instead of re-persisting `prev_value`, or explicitly mark it stale.
- Extend the `DataProvider` trait (or provide a staleness-aware default) so that `get()` returns `None`/an error once the underlying timestamp exceeds `ExpiresIn`, rather than only exposing this via the separate `get_value`/`DataProviderExtended` path.
- Document and enforce that any consumer pallet using price data for critical operations (liquidation, collateral valuation) must use the timestamp-aware `get_value` and validate `now - timestamp <= ExpiresIn` before trusting the value.

### Proof of Concept
1. Configure `MinimumCount = 3`, `ExpiresIn = N` (moments).
2. Three operators feed `key = 50` with value `1000` at time `t0`; `combine_data` succeeds and `Values[50] = {1000, t0}`.
3. All three operators stop submitting for longer than `ExpiresIn`.
4. A 4th unrelated key feed (`feed_values` for a different key by any single operator) triggers `do_feed_values`, which calls `Self::combined(&50)` — `read_raw_values` still returns the now-`ExpiresIn`-filtered-out entries, so `values` for key 50 becomes empty (`count == 0`), and `combine_data` returns `prev_value = {1000, t0}` unchanged: [9](#0-8) .
5. `Values[50]` is written back with the original stale timestamp `t0` [10](#0-9) .
6. Any consumer pallet calling `<Oracle as DataProvider<Key,Value>>::get(&50)` receives `1000` with zero indication it is stale, indefinitely, until fresh data eventually arrives [6](#0-5) .

Note: this repo snapshot contains the isolated Oracle pallet but not an integrated CDP/liquidation consumer pallet, so the downstream fund-loss scenario (e.g., mispriced liquidation) could not be traced end-to-end within this codebase; the vulnerable pattern (timestamp-discarding `DataProvider::get` plus stale-value persistence) is nonetheless directly verifiable in the cited pallet code.

### Citations

**File:** substrate/frame/honzon/oracle/src/lib.rs (L49-49)
```rust
//! * **Timestamped Data**: All submitted data includes timestamps for freshness tracking.
```

**File:** substrate/frame/honzon/oracle/src/lib.rs (L300-311)
```rust
	#[pallet::hooks]
	impl<T: Config<I>, I: 'static> Hooks<BlockNumberFor<T>> for Pallet<T, I> {
		/// `on_initialize` to return the weight used in `on_finalize`.
		fn on_initialize(_n: BlockNumberFor<T>) -> Weight {
			T::WeightInfo::on_finalize()
		}

		fn on_finalize(_n: BlockNumberFor<T>) {
			// cleanup for next block
			<HasDispatched<T, I>>::kill();
		}
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

**File:** substrate/frame/honzon/oracle/src/lib.rs (L415-429)
```rust
	fn do_feed_values(who: T::AccountId, values: Vec<(T::OracleKey, T::OracleValue)>) {
		let now = T::Time::now();
		for (key, value) in &values {
			let timestamped = TimestampedValue { value: value.clone(), timestamp: now };
			RawValues::<T, I>::insert(&who, key, timestamped);

			// Update `Values` storage if `combined` yielded result.
			if let Some(combined) = Self::combined(key) {
				<Values<T, I>>::insert(key, combined);
			}

			T::OnNewData::on_new_data(&who, key, value);
		}
		Self::deposit_event(Event::NewFeedData { sender: who, values });
	}
```

**File:** substrate/frame/honzon/oracle/src/lib.rs (L449-453)
```rust
impl<T: Config<I>, I: 'static> DataProvider<T::OracleKey, T::OracleValue> for Pallet<T, I> {
	fn get(key: &T::OracleKey) -> Option<T::OracleValue> {
		Self::get(key).map(|timestamped_value| timestamped_value.value)
	}
}
```

**File:** substrate/frame/honzon/oracle/src/default_combine_data.rs (L38-52)
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
```

**File:** substrate/frame/honzon/oracle/src/traits.rs (L29-33)
```rust
/// A simple trait for providing data.
pub trait DataProvider<Key, Value> {
	/// Returns the data for a given key.
	fn get(key: &Key) -> Option<Value>;
}
```
