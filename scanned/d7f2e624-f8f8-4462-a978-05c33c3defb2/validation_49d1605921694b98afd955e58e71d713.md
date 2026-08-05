## Analysis

The external report's core broken invariant: **a price/data value is consumed by protocol logic without verifying that the underlying data source is still live/fresh**, causing stale data to be treated as valid current state (in the Chainlink case, a dead L2 sequencer's frozen `latestRoundData()`).

The direct local analog is in `substrate/frame/honzon/oracle`, the pallet that other pallets are meant to consume as a `DataProvider`/`DataFeeder` for pricing.

### Title
Unbounded stale price fallback in `pallet-oracle`'s `DefaultCombineData::combine_data` allows indefinitely aging price data to be served as current - (`substrate/frame/honzon/oracle/src/default_combine_data.rs`)

### Summary
`DefaultCombineData::combine_data` filters out expired raw submissions using `ExpiresIn`, but when too few fresh values remain, it falls back to returning `prev_value` unconditionally — with no check on how old `prev_value` itself is. `Values::<T, I>` then continues to be updated with (or simply retains) this stale entry, and it is served as-is via `Pallet::get`, `get_value`, and the `DataProvider`/`DataProviderExtended` implementations to any consuming pallet, with no expiry re-check at read time.

### Finding Description [1](#0-0) 

`combine_data` computes `expires_in`/`now`, retains only fresh raw values, and if `count < minimum_count` (including `count == 0`, i.e., every oracle operator's submission has expired), it returns `prev_value` — the previously aggregated `TimestampedValue`, whose own `timestamp` field could be arbitrarily old — as the new aggregated value: [2](#0-1) 

This `prev_value` is written straight into `Values` storage inside `do_feed_values`: [3](#0-2) 

Consumers read this value through `Pallet::get`, the `#[pallet::view_functions]` `get_value`/`all_values`, and the `DataProvider`/`DataProviderExtended` trait implementations — none of which re-check the embedded `timestamp` against the current time before returning the value to the caller: [4](#0-3) [5](#0-4) 

This is architecturally identical to the sequencer-uptime bug: a downstream consumer that only checks "did I get *a* value" (analogous to `latestRoundData()` succeeding) has no way to know, without inspecting and independently validating the `timestamp` field itself, that the value is stale — the pallet's own aggregation logic silently perpetuates old data once the operator quorum stops refreshing it (analogous to the sequencer going down and freezing the feed).

### Impact Explanation
Any runtime pallet that wires `pallet-oracle` as its `T::Source: DataProvider`/`DataProviderExtended` (e.g., a CDP/lending/liquidation engine, as is standard for this pallet's intended Acala-style Honzon usage) inherits this staleness gap. If oracle operators stop submitting fresh values for a key (going offline, being slashed/removed, network partition, or simply not being incentivized once `minimum_count` can't be met), the aggregated price silently freezes at its last known value indefinitely instead of becoming unavailable (`None`) or being flagged stale. Downstream logic that trusts `get()`/`get_value()` returning `Some(..)` as "valid current price" can then execute liquidations, collateral valuations, or fee/reward calculations against a frozen price, causing mispriced settlement, unfair liquidations, or the freezing/loss of user funds when real market price has since diverged — the same class of impact called out in the report (incorrect pricing due to unrecognized data-source downtime).

### Likelihood Explanation
No privileged or malicious actor is required: an unprivileged, non-adversarial *absence* of activity (operators simply not feeding data, which can happen for many benign reasons: node downtime, connectivity issues, insufficient incentive) is enough to trigger the fallback path. The condition `count < minimum_count` is easy to reach whenever operator participation dips, and once triggered, the stale value persists across arbitrarily many blocks with no self-correcting expiry check — the pallet provides an `ExpiresIn` config specifically to prevent stale data, but that same protection is bypassed exactly when it's needed most (universal staleness).

### Recommendation
In `combine_data`, do not blindly forward `prev_value` when the fresh-value quorum is not met. Either:
- Also check `prev_value`'s own timestamp against `ExpiresIn` before returning it (returning `None` if it too has expired), or
- Track feed liveness/staleness explicitly and have consumers of `DataProvider`/`DataProviderExtended` be required to check the returned timestamp against an acceptable freshness bound before treating the value as valid, analogous to a sequencer-liveness check.

### Proof of Concept
1. Configure `pallet-oracle` with `DefaultCombineData<T, MinimumCount, ExpiresIn>` where `MinimumCount = 3`.
2. Three operators feed a price for key `K` at block time `t0`; `combine_data` succeeds and `Values[K] = { value: P, timestamp: t0 }`.
3. All operators stop submitting new values for `K` (no malicious action needed — simple inactivity).
4. Time advances past `t0 + ExpiresIn`. Any subsequent call path that re-invokes `combine_data` for `K` (e.g., a single new unrelated feed causing recomputation, or any consumer relying on stored `Values[K]`) still returns `prev_value = { value: P, timestamp: t0 }` — a price that is now arbitrarily stale — because the `count < minimum_count` branch returns `prev_value` unconditionally without an expiry check.
5. A downstream pallet configured with `type PriceSource = pallet_oracle::Pallet<T>;` calls `PriceSource::get(&K)`, receives `Some(P)`, and treats it as the current market price, using it for liquidation/collateral decisions against a value that no longer reflects reality.

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

**File:** substrate/frame/honzon/oracle/src/lib.rs (L395-398)
```rust
	/// Returns the aggregated and timestamped value for a given key.
	pub fn get(key: &T::OracleKey) -> Option<TimestampedValueOf<T, I>> {
		Self::values(key)
	}
```

**File:** substrate/frame/honzon/oracle/src/lib.rs (L415-424)
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
