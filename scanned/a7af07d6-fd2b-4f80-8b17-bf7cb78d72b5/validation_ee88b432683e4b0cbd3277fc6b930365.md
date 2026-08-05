pallet-oracle is instantiated in `substrate/bin/node/runtime/src/lib.rs` (via `pallet_oracle`/`ModuleOracle` config, 9 matches), so it is a live, wired-in runtime module, not just test code.

### Title
`DefaultCombineData::combine_data` can perpetually return an unbounded-stale price when fresh submissions drop below `MinimumCount` - (File: `substrate/frame/honzon/oracle/src/default_combine_data.rs`)

### Summary
The report's core invariant is: a price/data feed's freshness (`blockTimestamp`) must be validated before being treated as authoritative; otherwise stale prices are served indefinitely. `pallet-oracle`'s only shipped aggregation strategy, `DefaultCombineData`, filters out individually expired raw submissions, but when too few fresh submissions remain it falls back to `prev_value` — the previously aggregated value already in `Values` storage — with **no check that `prev_value` itself is still within any freshness window**. This mirrors exactly the reported class of bug: `latestRoundData()`-equivalent (`Pallet::get` / `DataProvider::get`) can return an arbitrarily old value because the underlying combine logic has no analog to `_assertMinIntervalBetweenUpdatesPassed`/max-staleness enforcement on the *returned* value.

### Finding Description
`combine_data` is defined as: [1](#0-0) 

The logic:
1. Retains only raw values whose `timestamp + expires_in > now`.
2. If the count of *fresh* values is below `MinimumCount` (including zero), it returns `prev_value` unchanged — the value already stored in `Values<T, I>` from a previous, possibly very old, combination.
3. `prev_value` is not itself timestamp-checked against `now` before being re-returned.

This `prev_value` (unchanged) is written back into `Values<T,I>` storage in `do_feed_values`: [2](#0-1) 

and is the value returned to any consuming pallet via the `DataProvider` trait: [3](#0-2) 

`DataProvider::get` strips the timestamp entirely, so a downstream consumer using this interface has no way to know the value is stale even if it wanted to check. Even consumers using `DataProviderExtended::get_all_values` (which does expose the `TimestampedValue`) are not protected by the pallet itself — the pallet's own aggregation intentionally recycles a stale value without bound as long as fewer than `MinimumCount` operators keep submitting fresh data (e.g., due to an outage, network partition, or operators simply going idle — no malicious actor required).

This is structurally identical to the reported Redstone issue: the "recentness" of the returned round/price is never enforced at the point the value is served to consumers; only individual raw submissions are timestamp-filtered, not the final served value.

### Impact Explanation
`pallet-oracle` is wired into a live runtime (`substrate/bin/node/runtime/src/lib.rs`) as the canonical price/data-feed source for any dependent pallet's economic logic (e.g., collateral pricing, liquidation thresholds, PSM-style conversions). If oracle operators stop actively feeding fresh values (which requires no compromise — simple inactivity, infra downtime, or a majority temporarily going offline), any consumer relying on `Oracle::get`/`DataProvider::get` will silently keep receiving the last aggregated value indefinitely with no expiry signal. This can lead to mis-priced collateral, incorrect liquidations, or incorrect settlement amounts in any pallet that trusts this price without independently re-validating the timestamp via `DataProviderExtended`.

### Likelihood Explanation
High: the condition (fewer than `MinimumCount` fresh submissions) is an ordinary operational failure mode, not an attack requiring a malicious peer, validator, or governance actor. It can be triggered passively by all-but-one submitter going offline, and it persists automatically without any special transaction being required — the stale value simply continues to be returned by design.

### Recommendation
Add an explicit staleness check on `prev_value` itself before returning it in `combine_data` (e.g., only return `prev_value` if `prev_value.timestamp.saturating_add(expires_in) > now`, otherwise return `None`), analogous to `_assertMinIntervalBetweenUpdatesPassed`. Additionally, consider exposing a `is_stale`/expiry check as part of the `DataProvider` trait itself so pallets consuming `Oracle::get()` (which discards the timestamp) cannot inadvertently consume unbounded-stale data.

### Proof of Concept
1. Configure `MinimumCount = 3`, `ExpiresIn = N` blocks/moments, with 3 oracle members.
2. All 3 members submit a price for key `K` at time `T0`; `combine_data` succeeds, `Values[K] = {value: P, timestamp: T0}`.
3. Advance time beyond `T0 + ExpiresIn` (submissions now stale) without any new `feed_values` calls (operators offline/idle — no attacker needed).
4. Any further `feed_values` call from a single lingering operator (or even zero further calls) leaves `Values[K]` unchanged at `{P, T0}`.
5. Any dependent pallet calling `Oracle::get(&K)` at time `T0 + 10*ExpiresIn` still receives `P` with no staleness indication, exactly reproducing "old prices returned" from the external report. This can be exercised directly against the existing test harness in `substrate/frame/honzon/oracle/src/tests.rs` (e.g. `should_combined_data`) by advancing `Timestamp::set_timestamp` far beyond `ExpiresIn` and observing `ModuleOracle::get(&key)` still returns the old `TimestampedValue`. [4](#0-3)

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

**File:** substrate/frame/honzon/oracle/src/tests.rs (L145-171)
```rust
#[test]
fn should_combined_data() {
	new_test_ext().execute_with(|| {
		let key: u32 = 50;

		assert_ok!(ModuleOracle::feed_values(
			RuntimeOrigin::signed(1),
			vec![(key, 1300)].try_into().unwrap()
		));
		assert_ok!(ModuleOracle::feed_values(
			RuntimeOrigin::signed(2),
			vec![(key, 1000)].try_into().unwrap()
		));
		assert_ok!(ModuleOracle::feed_values(
			RuntimeOrigin::signed(3),
			vec![(key, 1200)].try_into().unwrap()
		));

		let expected = Some(TimestampedValue { value: 1200, timestamp: 12345 });

		assert_eq!(ModuleOracle::get(&key), expected);

		Timestamp::set_timestamp(23456);

		assert_eq!(ModuleOracle::get(&key), expected);
	});
}
```
