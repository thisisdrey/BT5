### Title
`pallet-oracle`'s aggregated price/value storage never expires and `Pallet::get`/`DataProvider::get` return it with no staleness check - (File: `substrate/frame/honzon/oracle/src/lib.rs`, `substrate/frame/honzon/oracle/src/default_combine_data.rs`)

### Summary
The Zaros bug is that `ChainlinkUtil::getPrice` reads `latestRoundData()` and hands the price to consumers without validating `updatedAt` against a heartbeat, so a stalled feed silently returns old data as if it were current. The equivalent local pattern exists in the newly introduced `pallet-oracle` (`substrate/frame/honzon/oracle`, added for the Polkadot Stablecoin on AssetHub effort, see `prdoc/stable2512/pr_9815.prdoc`): the pallet only filters staleness on the *raw* per-operator submissions before aggregation, but the *aggregated* `Values` entry that consumers actually read is never invalidated or expired, and the public read APIs (`Pallet::get`, `DataProvider::get`) return it unconditionally.

### Finding Description
`DefaultCombineData::combine_data` filters raw values by `ExpiresIn` before combining: [1](#0-0) 

If the number of still-fresh raw values falls below `MinimumCount` (e.g. because oracle operators stop feeding, go offline, or are slow), `combine_data` returns `prev_value` unchanged — i.e., the previously aggregated (and now potentially long-expired) value: [2](#0-1) 

In `do_feed_values`, the `Values` storage is only updated when `combined()` yields `Some(...)`. Because `combine_data` keeps returning the same `prev_value` when quorum is lost, `Values<T, I>` is simply left untouched — it is never cleared, marked stale, or removed: [3](#0-2) 

The public read path that any downstream pallet uses to obtain a price performs no freshness check at all — it just returns whatever is in `Values` storage, regardless of how old its embedded `timestamp` is: [4](#0-3) [5](#0-4) 

The pallet's own documentation acknowledges the timestamp exists only so "consumers of the data... know how fresh it is" — i.e., staleness enforcement is explicitly punted to the consumer, exactly mirroring the root cause in the Zaros report (the Chainlink aggregator itself doesn't enforce a heartbeat check either; the wrapper that reads it must): [6](#0-5) 

The `create_median_value_data_provider!` macro used to build a `DataProvider` from multiple sources exhibits the identical gap — it takes the median of whatever `get()` returns from each source, again with no timestamp check: [7](#0-6) 

### Impact Explanation
This pallet is explicitly being introduced as price-feed infrastructure for a stablecoin on AssetHub (per `prdoc/stable2512/pr_9815.prdoc`). Any consuming pallet (e.g. a future collateral/liquidation/minting module) that trusts `Pallet::get`/`DataProvider::get` as an up-to-date price without independently re-checking the embedded `timestamp` against a maximum age will make solvency-critical decisions (collateral valuation, minting limits, liquidation triggers) using an arbitrarily stale price. If operators go offline or fail to refresh a feed, the last aggregated value persists indefinitely in storage and is returned as if current, enabling under/over-collateralized minting or unfair liquidations — the same "theft/unbacked mint" class of impact called out in the report.

### Likelihood Explanation
No adversarial or privileged action is required. A benign operator outage, network partition, or the operator set simply dropping below `MinimumCount` for a given key is sufficient to freeze `Values` at its last value forever, with the pallet giving no signal (error, `None`, or expired flag) to callers. Since the pallet exposes no built-in maximum-age enforcement on read, every consumer must reimplement the check correctly and independently — a design that has repeatedly caused exactly this bug class in real financial systems (per the referenced Chainlink oracle report).

### Recommendation
Enforce staleness at the read boundary inside `pallet-oracle` itself rather than relying purely on consumer discipline:
- Add a configurable maximum age (heartbeat) per `OracleKey` (or a pallet-wide `Config::MaxAge`), and have `Pallet::get`/`DataProvider::get` return `None` (or a distinguishable stale variant) when `now - value.timestamp > MaxAge`.
- Alternatively, when `combine_data` fails to produce a fresh aggregate (quorum lost), actively clear/remove the stale `Values` entry instead of leaving the old value in storage indefinitely.
- Document and enforce that `DataProviderExtended::get_all_values` and the `create_median_value_data_provider!` macro also apply the same staleness gate before returning data to any consumer.

### Proof of Concept
1. Configure `pallet-oracle` with `MinimumCount = 3` and `ExpiresIn = N` blocks/moments.
2. Three operators feed a price for `key`; `combine_data` succeeds and `Values::<T, I>::insert(key, combined)` stores `TimestampedValue { value: P, timestamp: T0 }` (`substrate/frame/honzon/oracle/src/lib.rs:415-429`).
3. All three operators stop submitting (or are removed via governance and not replaced immediately). Time advances far beyond `ExpiresIn`.
4. Any consumer calls `Oracle::get(&key)` or `<Oracle as DataProvider<_,_>>::get(&key)`. Because `Values` was never touched after the initial insert, the pallet returns `TimestampedValue { value: P, timestamp: T0 }` unchanged (`lib.rs:396-403,449-453`) — a price that may be arbitrarily old — with `pays_fee`/dispatch logic giving no indication of staleness. A consumer that does not independently check `timestamp` (mirroring `ChainlinkUtil::getPrice`) will treat `P` as the current price indefinitely.

### Citations

**File:** substrate/frame/honzon/oracle/src/default_combine_data.rs (L42-52)
```rust
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

**File:** substrate/frame/honzon/oracle/src/lib.rs (L49-49)
```rust
//! * **Timestamped Data**: All submitted data includes timestamps for freshness tracking.
```

**File:** substrate/frame/honzon/oracle/src/lib.rs (L395-403)
```rust
	/// Returns the aggregated and timestamped value for a given key.
	pub fn get(key: &T::OracleKey) -> Option<TimestampedValueOf<T, I>> {
		Self::values(key)
	}

	fn combined(key: &T::OracleKey) -> Option<TimestampedValueOf<T, I>> {
		let values = Self::read_raw_values(key);
		T::CombineData::combine_data(key, values, Self::values(key))
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

**File:** substrate/frame/honzon/oracle/src/traits.rs (L55-69)
```rust
#[macro_export]
macro_rules! create_median_value_data_provider {
	($name:ident, $key:ty, $value:ty, $timestamped_value:ty, [$( $provider:ty ),*]) => {
		pub struct $name;
		impl $crate::DataProvider<$key, $value> for $name {
			fn get(key: &$key) -> Option<$value> {
				let mut values = vec![];
				$(
					if let Some(v) = <$provider as $crate::DataProvider<$key, $value>>::get(&key) {
						values.push(v);
					}
				)*
				$crate::traits::median(values)
			}
		}
```
