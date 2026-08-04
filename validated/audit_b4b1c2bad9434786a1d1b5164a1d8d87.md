Based on my research, I found a strong local analog to the Switchboard staleness bug in `pallet-oracle`'s `DataProvider` implementation, which is actually wired into the shipped node runtime.

### Title
Oracle `DataProvider::get` discards price timestamp, allowing stale aggregated prices to be consumed indefinitely - ([File: substrate/frame/honzon/oracle/src/lib.rs])

### Summary
`pallet-oracle` timestamps every raw price submission and uses `DefaultCombineData` to aggregate them, dropping expired raw feeds via an `ExpiresIn` window. However, once an aggregated `TimestampedValue` is stored in `Values<T, I>`, it is returned as `prev_value` forever whenever fresh submissions drop below `MinimumCount`, and the `DataProvider::get` trait implementation strips the timestamp entirely before handing the price to consumers. This is structurally identical to `refresh_oracle_price::load_switchboard`, which extracts `feed.value()` and ignores the feed's timestamp.

### Finding Description
`DefaultCombineData::combine_data` filters only the freshly-submitted raw values by `ExpiresIn`, then falls back to the last known aggregate if too few fresh values remain: [1](#0-0) 

Crucially, `prev_value` (the previously stored aggregate) is returned unconditionally when `count < minimum_count`, with no check on `prev_value.timestamp` itself. This means a once-aggregated `TimestampedValue` in `Values<T,I>` can be handed out for an unbounded number of blocks after all oracle operators stop feeding data (e.g., go offline, get removed and not replaced quickly, or simply skip a currency pair).

Compounding this, the `DataProvider` trait implementation - the primary interface most consuming pallets are expected to use per the pallet's own docs ("other pallets can use the `DataProvider` trait to read the aggregated data") - discards the timestamp entirely: [2](#0-1) 

Any pallet wired to `T::PriceSource = pallet_oracle::Pallet<T>` via `DataProvider::get` therefore receives only `T::OracleValue` with zero indication of when it was produced - functionally identical to `load_switchboard` calling `feed.value()` and discarding the feed timestamp. The only freshness-aware accessor is `get_value`/`get` (returning `TimestampedValueOf`) and `DataProviderExtended::get_all_values`, but nothing in the pallet itself enforces that consumers check these timestamps, and the most convenient/canonical trait (`DataProvider`) offers no way to do so at all.

### Impact Explanation
Any downstream pallet in a runtime that uses `pallet-oracle` as a `DataProvider` (e.g., a lending/CDP/liquidation module using price data to value collateral, trigger liquidations, or gate swaps) inherits this staleness blind spot. If oracle operators go offline or are slow to re-feed a key, the last aggregated price keeps being served as if current, exactly the "outdated prices accepted" primitive from the external report. This can misprice collateral/debt, enabling under-collateralized borrowing, incorrect liquidations, or arbitrage against a stale peg — a real accounting/fund-safety impact rather than a cosmetic one.

### Likelihood Explanation
This is a structural design gap, not a rare edge case: it triggers naturally whenever the number of live/fresh feeders for a key drops below `MinimumCount` (operator churn, network issues, a currency pair simply going quiet) — no malicious actor, governance action, or privileged party is required. The `DataProvider::get` API being the pallet's advertised integration point makes it the likely path any consuming pallet would use, and it offers zero recency signal.

### Recommendation
1. In `DefaultCombineData::combine_data`, also validate that `prev_value.timestamp.saturating_add(expires_in) > now` before returning it as a fallback; if it too has expired, return `None` instead of a stale price.
2. Change `DataProvider::get` (or add a distinct safe accessor) to return `None`/error when the aggregated value's timestamp is older than a configurable staleness bound, rather than silently returning a valueless, timestamp-less price to consumers.
3. Document/require that any pallet consuming oracle data must check `TimestampedValue::timestamp` against a max age before use, and prefer exposing only the timestamped API by default.

### Proof of Concept
1. Configure `MinimumCount = 3`, `ExpiresIn = 100` (moments) for `pallet-oracle`.
2. Three operators feed a price for key `K` at `t=0`; `Values::<T>::get(K)` becomes `Some(TimestampedValue { value: P, timestamp: 0 })` (see aggregation logic at [3](#0-2) ).
3. All operators stop feeding (or are removed via `change_members_sorted`, which clears `RawValues` at [4](#0-3) ).
4. Time advances far beyond `ExpiresIn` (e.g., `t=100000`). Any subsequent single feed (or even a query that triggers `combined`) recomputes `combine_data`: fresh-value count is `0 < MinimumCount`, so it returns `prev_value` — the `t=0` price — verbatim, with no error.
5. A consumer calling `Pallet::<T>::get(K)` sees a `TimestampedValue` with an ancient timestamp (if it bothers to check), but any consumer using `DataProvider::get(K)` receives only `P` with no timestamp at all, and cannot distinguish this from a fresh price.

### Citations

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

**File:** substrate/frame/honzon/oracle/src/lib.rs (L432-443)
```rust
impl<T: Config<I>, I: 'static> ChangeMembers<T::AccountId> for Pallet<T, I> {
	fn change_members_sorted(
		_incoming: &[T::AccountId],
		outgoing: &[T::AccountId],
		_new: &[T::AccountId],
	) {
		// remove values
		for removed in outgoing {
			let _ = RawValues::<T, I>::clear_prefix(removed, u32::MAX, None);
		}
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
