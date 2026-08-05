### Title
Oracle `Values` storage returns unbounded-stale price with no freshness enforcement when live submissions drop below `MinimumCount` - (File: `substrate/frame/honzon/oracle/src/default_combine_data.rs`)

### Summary
`DefaultCombineData::combine_data` filters out expired raw submissions using `ExpiresIn`, but if the number of *fresh* submissions falls below `MinimumCount`, it silently falls back to `prev_value` — the last aggregated value stored in `Values<T, I>` — with **no check on how old that `prev_value` itself is**. The result is functionally identical to the reported Chainlink `minAnswer/maxAnswer` circuit-breaker bug: once the feed can no longer produce enough fresh quotes, the on-chain price freezes at whatever it was last, and any pallet/precompile that trusts `pallet_oracle::Pallet::get()` / `DataProvider::get()` will keep receiving that frozen value as if it were live, with no on-chain signal that it is stale.

### Finding Description
`combine_data` in `substrate/frame/honzon/oracle/src/default_combine_data.rs:38-59`:
```rust
values.retain(|x| x.timestamp.saturating_add(expires_in) > now);
let count = values.len() as u32;
let minimum_count = MinimumCount::get();
if count < minimum_count || count == 0 {
    return prev_value;
}
```
`prev_value` is `Self::values(key)` — the previously committed aggregate stored in the `Values<T, I>` StorageMap (`substrate/frame/honzon/oracle/src/lib.rs:277-283`). That value carries its own `timestamp`, but nothing in `combine_data` (or in `do_feed_values`, `get`, `DataProvider::get`, or `DataProviderExtended::get_all_values`, `substrate/frame/honzon/oracle/src/lib.rs:395-459`) re-checks that timestamp against `ExpiresIn`/`now` before returning it to a consumer. `Values::get` simply returns whatever is in storage forever once the number of fresh operator submissions can no longer clear `MinimumCount` (e.g. operators go offline, get removed via `ChangeMembers`, or simply stop feeding one key while continuing others).

This is the exact analog of the Chainlink `minAnswer/maxAnswer` circuit breaker: the aggregator (oracle round) hits a boundary condition (here: "not enough fresh votes") and, instead of reverting or surfacing "no data," it keeps returning an old committed value as though it were current. A downstream consumer (any `Config::CombineData`/`DataProvider` client wired into a runtime, as is done for `OraclePalletId` in `substrate/bin/node/runtime/src/lib.rs:3119-3151`) has no built-in way to distinguish "fresh consensus price" from "stale frozen price," unless it manually re-derives and compares `TimestampedValue::timestamp` against `now` on every read — which none of the pallet's own exposed APIs (`get`, `get_all_values`, the `OracleApi` runtime API) do.

### Impact Explanation
Any runtime that wires a lending/CDP/margin pallet against this oracle (the intended integration pattern, mirroring Acala's Honzon) inherits a silent stale-price acceptance path. If the real market price of the underlying asset moves sharply while feeder participation drops below `MinimumCount` (which can happen passively — operators removed via governance churn, an operator's feed simply stops for one key, or the last feeders' data expires simultaneously), the chain continues exposing the old price as the canonical value. A user can then interact with the consuming pallet (borrow, liquidate, redeem, mint) using the frozen mispriced value — over-borrowing against a collapsed asset or avoiding liquidation that should have triggered, directly causing fund loss/theft analogous to the Venus/LUNA scenario cited in the source report.

### Likelihood Explanation
No malicious/privileged actor is required: the fallback path triggers purely from insufficient fresh submissions, which is a normal operational condition (feeder downtime, feed removal, network partition). Because `MinimumCount` and `ExpiresIn` are runtime-configured constants, any runtime that sets a modest `MinimumCount` (to tolerate normal operator churn) is exposed by design. The bug is in the shared default aggregation logic (`DefaultCombineData`), so it affects every consumer of this pallet unless they implement a custom `CombineData` that separately re-validates `prev_value` freshness — which the pallet's documentation does not call out as required.

### Recommendation
- In `DefaultCombineData::combine_data`, before returning `prev_value`, check `prev_value.timestamp.saturating_add(expires_in) > now`; if it fails, return `None` instead of a stale value.
- Expose a `get_no_op` on the `Values` read path only through a freshness-checked accessor, or attach a public "staleness" flag/error from `DataProvider`/`OracleApi::get_value` so downstream pallets can revert rather than silently accept.
- Document in `Config::CombineData` that any custom implementation must not return unboundedly-old fallback values.

### Proof of Concept
1. Configure a runtime with `pallet_oracle` using `DefaultCombineData<T, MinimumCount = 3, ExpiresIn = 600>` (as in `substrate/frame/honzon/oracle/src/mock.rs:74-92`), feeding key `50`.
2. Three operators feed `50 -> 1000` at `t=0`; `combine_data` produces `Values[50] = {1000, t=0}` (see `substrate/frame/honzon/oracle/src/tests.rs:145-171`, which already demonstrates the value persists unchanged after time advances to `t=23456`, well past `ExpiresIn=600`).
3. Two of three operators stop feeding (removed by governance churn or simply idle) so subsequent `feed_values` calls never gather ≥`MinimumCount=3` fresh (unexpired) submissions.
4. Regardless of how far `now` advances beyond `timestamp + ExpiresIn`, `Pallet::get(&50)` / `DataProvider::get(&50)` continues to return `{1000, t=0}` verbatim (as shown by `should_combined_data` asserting the same `expected` value persists across the timestamp jump) — the on-chain "price" is frozen even though it is arbitrarily stale, with no error, event, or flag distinguishing it from a live quote. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

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

**File:** substrate/frame/honzon/oracle/src/lib.rs (L277-283)
```rust
	/// The aggregated values for each oracle key.
	///
	/// Maps `OracleKey` to `TimestampedValue`.
	#[pallet::storage]
	#[pallet::getter(fn values)]
	pub type Values<T: Config<I>, I: 'static = ()> =
		StorageMap<_, Twox64Concat, <T as Config<I>>::OracleKey, TimestampedValueOf<T, I>>;
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

**File:** substrate/bin/node/runtime/src/lib.rs (L3119-3151)
```rust
#[cfg(feature = "runtime-benchmarks")]
pub struct OracleBenchmarkingHelper;

#[cfg(feature = "runtime-benchmarks")]
impl pallet_oracle::BenchmarkHelper<u32, u128, OracleMaxFeedValues> for OracleBenchmarkingHelper {
	fn get_currency_id_value_pairs() -> BoundedVec<(u32, u128), OracleMaxFeedValues> {
		use rand::{distributions::Uniform, prelude::*};

		// Use seeded RNG like in contracts benchmarking
		let mut rng = rand_pcg::Pcg32::seed_from_u64(0x1234567890ABCDEF);
		let max_values = OracleMaxFeedValues::get() as usize;

		// Generate random pairs like in election-provider-multi-phase
		let currency_range = Uniform::new_inclusive(1, 1000);
		let value_range = Uniform::new_inclusive(1000, 1_000_000);

		let pairs: Vec<(u32, u128)> = (0..max_values)
			.map(|_| {
				let currency_id = rng.sample(currency_range);
				let value = rng.sample(value_range);
				(currency_id, value)
			})
			.collect();

		// Use try_from pattern like in core-fellowship and broker
		BoundedVec::try_from(pairs).unwrap_or_default()
	}
}

parameter_types! {
	pub const OraclePalletId: PalletId = PalletId(*b"py/oracl");
}

```
