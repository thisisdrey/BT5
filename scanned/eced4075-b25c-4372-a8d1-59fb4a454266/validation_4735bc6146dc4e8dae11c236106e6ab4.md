### Title
Oracle's `DataProvider::get` returns stale, unexpired aggregated price data with no freshness check at read time - ([File: substrate/frame/honzon/oracle/src/lib.rs])

### Summary
The core Chainlink bug is that a price consumer trusted `latestRoundData()` without checking whether the returned round was actually fresh (non-zero timestamp, no round carry-over). `pallet-oracle` in this repository has the exact same broken invariant: staleness is only checked once, at aggregation time, and never again when the aggregated value is later consumed. Any pallet wired up as a `DataProvider`/`DataProviderExtended` consumer of this oracle (the pallet's own documented purpose, see [1](#0-0) ) can silently keep using an arbitrarily old price if operators stop feeding new data.

### Finding Description
`DefaultCombineData::combine_data` filters out stale *raw* submissions before computing the median, using an `ExpiresIn` window compared to `T::Time::now()`: [2](#0-1) 

This check only runs inside `do_feed_values`, i.e. only when a new `feed_values` extrinsic actually arrives and re-triggers `combined()`: [3](#0-2) 

The last computed aggregate is written to `Values<T, I>` and stays there indefinitely. The public consumption path exposed to every other pallet strips the timestamp entirely and hands back only the raw value, with no re-validation against current time: [4](#0-3) 

`Pallet::get` (used internally and exported) likewise just returns the stored `TimestampedValue` without any caller-side enforcement that the timestamp is still within `ExpiresIn`: [5](#0-4) 

So the invariant "oracle data consumers only ever see data that is provably fresh" is broken exactly the way the Chainlink report describes: the freshness gate exists only at the write/aggregation boundary, not at the read/consumption boundary. If oracle operators stop submitting (network partition, censorship, removal via `SortedMembers`, or simply a quiet market with no need to re-feed), `Values::get(key)` — and therefore `DataProvider::get` — continues to return the old value forever. Nothing forces the value to be re-checked against `ExpiresIn` outside of a new `feed_values` call.

### Impact Explanation
Any downstream pallet that treats `pallet_oracle::Pallet::get` / `DataProvider::get` as ground truth (e.g. for collateral pricing, liquidation thresholds, mint/burn ratios, or fee calculations, which is the stated purpose of this pallet per its own docs) inherits unbounded staleness risk. An attacker or a passive failure (oracle members going offline, being slashed/removed, or simply not needing to re-feed identical values) can freeze the effective price at a stale value while real market conditions move, enabling under-collateralized minting, blocked liquidations, or mispriced withdrawals in any runtime that plugs this pallet in as its price source — directly matching the "runtime bugs that compromise intended behavior" and "theft or unbacked mint" impact classes in scope.

### Likelihood Explanation
No privileged actor, governance action, or malicious relayer is required. The pallet's aggregation code already contains the freshness concept (`ExpiresIn`), showing the intended design was for consumers to only ever see fresh data — but the enforcement gap is purely a missing check on the read/consumption side, which is trivially reached simply by any oracle operators pausing submissions (a routine, non-malicious event) or being removed via membership changes (`change_members_sorted`), which does not force `Values` to be cleared or re-validated.

### Recommendation
Mirror the Chainlink-style fix at the consumption boundary: have `DataProvider::get` (and `Pallet::get`) validate `timestamp.saturating_add(ExpiresIn::get()) > T::Time::now()` before returning a value, returning `None` otherwise, so stale aggregates can never silently flow into dependent runtime logic. Alternatively, expose the timestamp through `DataProvider` (or extend the trait) so every consumer is forced to perform its own explicit staleness check, analogous to requiring `timeStamp != 0` and `answeredInRound >= roundID` before trusting `latestRoundData()`.

### Proof of Concept
1. Configure a runtime with `pallet-oracle` (`DefaultCombineData<T, MinimumCount, ExpiresIn>`) as the price source for some consuming pallet (per the documented `DataProvider` integration pattern).
2. Operators feed enough values to populate `Values::<T, I>` for a key (see `values_are_updated_on_feed` test at [6](#0-5) ).
3. Stop feeding new data for longer than `ExpiresIn` (or remove all members via `ChangeMembers`).
4. Call `DataProvider::get(key)` — it still returns the old value from step 2 because `Values` is never re-checked against `now()`; only newly-fed raw values are filtered inside `combine_data`. Any consuming pallet computing collateral ratios, mint amounts, or liquidations off this call now operates on unbounded-age data.

### Citations

**File:** substrate/frame/honzon/oracle/README.md (L37-40)
```markdown
### Data Providers

The pallet implements the `DataProvider` and `DataProviderExtended` traits, allowing other pallets to easily
consume the oracle data.
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

**File:** substrate/frame/honzon/oracle/src/lib.rs (L395-398)
```rust
	/// Returns the aggregated and timestamped value for a given key.
	pub fn get(key: &T::OracleKey) -> Option<TimestampedValueOf<T, I>> {
		Self::values(key)
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

**File:** substrate/frame/honzon/oracle/src/tests.rs (L316-341)
```rust
#[test]
fn values_are_updated_on_feed() {
	new_test_ext().execute_with(|| {
		assert_ok!(ModuleOracle::feed_values(
			RuntimeOrigin::signed(1),
			vec![(50, 900)].try_into().unwrap()
		));
		assert_ok!(ModuleOracle::feed_values(
			RuntimeOrigin::signed(2),
			vec![(50, 1000)].try_into().unwrap()
		));

		assert_eq!(ModuleOracle::values(50), None);

		// Upon the third price feed, the value is updated immediately after `combine`
		// can produce valid result.
		assert_ok!(ModuleOracle::feed_values(
			RuntimeOrigin::signed(3),
			vec![(50, 1100)].try_into().unwrap()
		));
		assert_eq!(
			ModuleOracle::values(50),
			Some(TimestampedValue { value: 1000, timestamp: 12345 })
		);
	});
}
```
