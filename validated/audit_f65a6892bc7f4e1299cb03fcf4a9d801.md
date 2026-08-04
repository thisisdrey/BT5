### Title
Oracle price data can remain stale indefinitely because `DataProvider::get()` strips the timestamp and `DefaultCombineData` silently falls back to an unbounded-age previous value - ([File: substrate/frame/honzon/oracle/src/default_combine_data.rs])

### Summary
This is a direct on-chain analog of the VST Oracle issue: a price/data consumer can receive an oracle value with no built-in way to detect that the data is out of date, because the freshness (`updatedAt`) information is discarded before it reaches the caller that would need it to enforce a staleness bound.

### Finding Description
The oracle pallet stores every submitted value with a timestamp (`TimestampedValue`) [1](#0-0) . Aggregation is performed by `DefaultCombineData::combine_data`, which filters out raw values older than `ExpiresIn`, but if the number of still-fresh values is below `MinimumCount`, it returns `prev_value` unchanged: [2](#0-1) 

Critically, `prev_value` itself is never checked against `ExpiresIn` before being returned/re-stored — only the incoming `values` are filtered. So once quorum drops (e.g., operators stop feeding, or all feed simultaneously stale/duplicate data), `Values<T, I>` in storage can retain an arbitrarily old `TimestampedValue` indefinitely, since `do_feed_values` re-inserts whatever `combined()` returns, including the stale `prev_value` [3](#0-2) .

On top of that, the primary consumption interface — `DataProvider::get()` — deliberately strips the timestamp and returns only the raw value: [4](#0-3) 

This is exactly the VST Oracle pattern: `getPriceData` returns `updatedAt`, but `getPrice()` (the convenience wrapper most callers use) never checks it. Here, `DataProviderExtended::get_all_values()` and `Pallet::get_value` do carry the timestamp [5](#0-4) , but any consuming pallet that integrates via the simpler `DataProvider<Key, Value>` trait (the one explicitly documented as the primary integration point for "other pallets to easily consume oracle data" [6](#0-5) ) has no access to the timestamp at all and thus cannot enforce any max-age check, no matter how security-conscious its own code is.

### Impact Explanation
Any runtime that wires a financial pallet (lending, CDP/liquidation engine, DEX pricing, collateral valuation) against `pallet-oracle`'s `DataProvider` interface inherits an unavoidable staleness blind spot: the consumer cannot reject old prices because the interface it is told to use does not expose `timestamp`, and the aggregation logic (`DefaultCombineData`) can perpetuate an old value without limit once quorum is lost. This can lead to mispriced collateral, incorrect liquidations, or acceptance of economically stale data driving fund-affecting decisions — the same "false state acceptance based on unverified freshness" class as the original report, but on-chain and affecting consensus-critical financial logic rather than an off-chain price consumer.

### Likelihood Explanation
No malicious actor, governance abuse, or privileged action is required — it happens naturally whenever the number of active oracle feeders temporarily drops below `MinimumCount` (e.g., network partition, feeder downtime, or an attacker simply not feeding while other conditions coincide). Since `combine_data` only filters incoming values and not `prev_value`, the stale value persists silently with no error, event, or bound on how old it can become.

### Recommendation
- Have `DefaultCombineData::combine_data` also check `prev_value`'s own timestamp against `ExpiresIn` before returning it as a fallback; if `prev_value` is itself expired, return `None` instead of stale data.
- Change the primary `DataProvider::get()` contract (or add a required companion) so pallets consuming oracle prices always receive the `timestamp` alongside the `value`, forcing integrators to make an explicit freshness decision rather than allowing silent staleness.
- Emit an event/error when the oracle falls back to an existing (unrefreshed) value due to insufficient fresh submissions, so downstream monitoring/governance can react.

### Proof of Concept
1. Configure `pallet-oracle` with `MinimumCount = 3`, `ExpiresIn = 600` (10 minutes in block-time units), 4 members.
2. At `t = 0`, 3 members feed a price; `Values` is updated to `(price=P0, timestamp=0)`.
3. From `t = 1` to `t = 10000`, only 1 member (or fewer than `MinimumCount`) continues feeding; `combine_data` always returns `prev_value = (P0, 0)` unchanged because `count < MinimumCount`.
4. Any consumer pallet calling `DataProvider::get(key)` at `t = 10000` receives `Some(P0)` with **no timestamp at all**, and even a consumer using `Pallet::get_value` sees `timestamp = 0` but has no enforced check preventing it from using `P0` as current — since the pallet itself provides no staleness rejection, this is left entirely to (often absent) downstream logic, exactly mirroring the reported `VSTOracle.getPrice()` flaw.

### Citations

**File:** substrate/frame/honzon/oracle/src/lib.rs (L138-159)
```rust
	/// A wrapper for a value with a timestamp.
	#[derive(
		Encode,
		Decode,
		Debug,
		Eq,
		PartialEq,
		Clone,
		Copy,
		Ord,
		PartialOrd,
		TypeInfo,
		MaxEncodedLen,
		Serialize,
		Deserialize,
	)]
	pub struct TimestampedValue<Value, Moment> {
		/// The value.
		pub value: Value,
		/// The timestamp.
		pub timestamp: Moment,
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

**File:** substrate/frame/honzon/oracle/README.md (L37-40)
```markdown
### Data Providers

The pallet implements the `DataProvider` and `DataProviderExtended` traits, allowing other pallets to easily
consume the oracle data.
```
