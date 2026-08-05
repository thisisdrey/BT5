### Title
Missing read-time staleness check in `pallet-oracle` aggregated values allows stale price consumption - (File: `substrate/frame/honzon/oracle/src/lib.rs`)

### Summary
The `pallet-oracle` aggregated `Values` storage can outlive the configured `ExpiresIn` window and is returned by `Pallet::get()` and `DataProvider::get()` without a freshness check, mirroring the PythOracle `getPriceUnsafe()` staleness bug class. [1](#0-0) [2](#0-1) 

### Finding Description
The exact corrupted value is the `TimestampedValue` stored in `Values<T, I>`, which is returned indefinitely after its timestamp has expired. [3](#0-2)  `Pallet::get()` reads directly from `Values` storage and returns the stored `TimestampedValue` without comparing its timestamp to the current time. [1](#0-0)  `DataProvider::get()` strips the timestamp and returns only the raw value, so consumers of the standard trait cannot verify freshness. [2](#0-1)  `do_feed_values()` only re-aggregates and updates `Values` when a new feed is submitted. [4](#0-3)  `DefaultCombineData` filters individual raw submissions by `timestamp + ExpiresIn > now` at feed time, but it returns the previous aggregated value when not enough fresh raw values exist, propagating stale data into `Values`. [5](#0-4)  Consequently, once `Values` is written, it is served until the next feed regardless of how old its timestamp becomes. [1](#0-0) 

### Impact Explanation
A runtime module consuming `DataProvider::get()` for price-sensitive decisions can operate on prices whose timestamp exceeds `ExpiresIn`. [2](#0-1)  This can trigger forced liquidations of healthy positions or allow undercollateralized borrows, producing the same fund-loss impact class as the reported PythOracle issue. [2](#0-1)  The bug is a runtime-level compromise of intended price-feed behavior. [1](#0-0) 

### Likelihood Explanation
The path requires oracle operators to stop submitting updates for longer than `ExpiresIn`, which can happen due to operator outage, network congestion, or membership changes. [4](#0-3)  No malicious oracle member, validator, collator, or governance actor is required; any unprivileged user can call downstream functions that read the stale aggregated value once it exists. [1](#0-0)  The existing `HasDispatched` one-feed-per-block limit and `DefaultCombineData` raw-value filter do not re-evaluate `Values` between feeds, so they do not prevent stale data from being served. [6](#0-5) [5](#0-4) 

### Recommendation
Enforce read-time freshness in `Pallet::get()` and `DataProvider::get()` by returning `None` when `value.timestamp + ExpiresIn <= T::Time::now()`. [1](#0-0)  Alternatively, re-validate `Values` in `on_initialize` before it is served, or expose only a timestamped getter and require consuming pallets to check freshness. [7](#0-6)  If `DefaultCombineData` falls back to `prev_value`, ensure the fallback value is also re-checked against the current time before storage. [5](#0-4) 

### Proof of Concept
The existing test `should_combined_data` demonstrates the behavior: three feeds at timestamp `12345` produce an aggregated `TimestampedValue { value: 1200, timestamp: 12345 }`. [8](#0-7)  The test then advances time to `23456`, which is `11111` units later and well beyond the mock `ExpiresIn` of `600`. [9](#0-8) [10](#0-9)  `ModuleOracle::get(&key)` still returns the same stale value, proving `Values` is served without read-time staleness enforcement. [11](#0-10)

### Citations

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

**File:** substrate/frame/honzon/oracle/src/lib.rs (L367-373)
```rust
			// ensure account hasn't dispatched an updated yet
			<HasDispatched<T, I>>::try_mutate(|set| {
				set.try_insert(who.clone())
					.map_err(|_| Error::<T, I>::ExceedsMaxHasDispatchedSize)?
					.then_some(())
					.ok_or(Error::<T, I>::AlreadyFeeded)
			})?;
```

**File:** substrate/frame/honzon/oracle/src/lib.rs (L396-398)
```rust
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

**File:** substrate/frame/honzon/oracle/src/lib.rs (L450-452)
```rust
	fn get(key: &T::OracleKey) -> Option<T::OracleValue> {
		Self::get(key).map(|timestamped_value| timestamped_value.value)
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

**File:** substrate/frame/honzon/oracle/src/tests.rs (L145-170)
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
```

**File:** substrate/frame/honzon/oracle/src/mock.rs (L79-92)
```rust
impl Config for Test {
	type OnNewData = ();
	type CombineData = DefaultCombineData<Self, ConstU32<3>, ConstU32<600>>;
	type Time = Timestamp;
	type OracleKey = Key;
	type OracleValue = Value;
	type PalletId = OraclePalletId;
	type Members = Members;
	type WeightInfo = ();
	type MaxHasDispatchedSize = ConstU32<100>;
	type MaxFeedValues = MaxFeedValues;
	#[cfg(feature = "runtime-benchmarks")]
	type BenchmarkHelper = ();
}
```
