## Finding



### Title
Oracle price-update rate-limit (`HasDispatched`) is enforced only on the `feed_values` extrinsic and is fully bypassable via the `DataFeeder::feed_value` trait path, enabling repeated same-block price manipulation - ([File: substrate/frame/honzon/oracle/src/lib.rs])

### Summary
`pallet-oracle` restricts oracle operators to a single price submission per block through the `HasDispatched` set, checked only inside the `feed_values` extrinsic [1](#0-0) . The pallet also exposes the exact same state-mutating logic (`do_feed_values`, which re-runs `CombineData::combine_data` and updates the aggregated `Values` storage) through the `DataFeeder::feed_value` trait implementation, which performs no `HasDispatched` insertion or check whatsoever [2](#0-1) . This is the same class of bug as the ReserveOracle report: a per-call/per-block bound meant to prevent large or repeated price swings is enforced on only one code path and can be trivially bypassed by driving the same underlying update function through a second, unguarded entry point multiple times before the block finalizes.

### Finding Description
The `HasDispatched` storage is documented explicitly as the anti-spam / fair-participation control: "A set of accounts that have already fed data in the current block... enforce the 'one submission per block' rule... cleared at the end of each block in `on_finalize`" [3](#0-2) .

The `feed_values` extrinsic enforces this by trying to insert the caller into `HasDispatched` and failing with `AlreadyFeeded` on a repeat call within the same block [4](#0-3) .

However, `do_feed_values` — the function that actually writes `RawValues`, recomputes the combined/median price via `T::CombineData::combine_data`, and updates the trusted `Values` storage that other pallets read as the oracle price — is a private helper called from two places [5](#0-4) :
1. `feed_values` extrinsic (guarded by `HasDispatched`).
2. `DataFeeder::feed_value`, which calls `Self::do_feed_values(Self::ensure_account(who)?, vec![(key, value)])` directly with **no** `HasDispatched` check [6](#0-5) .

The pallet's own test confirms the asymmetry: after the extrinsic path rejects a second same-block submission with `AlreadyFeeded`, the exact same account can call `feed_value` (the trait method) repeatedly in the same block with no restriction, explicitly annotated "But not if fed thought the trait internally" [7](#0-6) .

Because `combine_data` recomputes the median/aggregate on every call to `do_feed_values` (not just once per block), an operator (or any pallet/consumer wired to call `feed_value` with attacker-influenced input) can repeatedly push new raw values through the trait path within a single block/transaction sequence, each time immediately overwriting the trusted `Values` entry that price-sensitive consumers read via `DataProvider::get` [8](#0-7) . The per-block anti-manipulation guard that the extrinsic advertises is therefore not an actual invariant of the oracle's price-writing logic — it is only a guard around one specific entry point.

### Impact Explanation
Any consumer of this oracle's aggregated price (used for collateral valuation / liquidation-style logic, matching exactly the role of `ReserveOracle` in the original report) inherits a false sense of protection from "one update per block" when the real invariant is per-*extrinsic-call*, not per-block-and-value. A caller with access to the `feed_value` trait surface (directly, or indirectly through any pallet that forwards data into the oracle without re-implementing the `HasDispatched` check) can force multiple aggregation recomputations in one block, moving the reported price rapidly and using it to misprice collateral before consumers or automated liquidation logic account for staleness — same "false state acceptance via a bound that only applies per single call" pattern as the source finding.

### Likelihood Explanation
This is provable purely from local pallet code and its own test suite (`multiple_calls_should_fail`) without needing a malicious validator, collator, relayer, or governance actor — an existing member of `T::Members` (a normal, non-privileged permissioned oracle feeder, analogous to a price-feed operator in the source report) is sufficient to trigger the trait path with attacker-influenced values. What I could **not** confirm from the indexed portion of this repository is whether any currently wired runtime pallet forwards externally-controlled input into `DataFeeder::feed_value` in a loop within a single extrinsic/transaction (the `substrate/bin/node/runtime/src/lib.rs` matches for `Oracle`/`DataFeeder` could not be resolved to source lines from the index). The vulnerability in the pallet's own invariant enforcement is concrete and directly demonstrated by its test; the blast radius depends on which downstream pallet/runtime wires up `feed_value`, which would need to be verified with full repository access.

### Recommendation
Move the `HasDispatched` per-block check (or an equivalent time/block-based rate limit on aggregate changes) into `do_feed_values` itself so it applies uniformly to both the `feed_values` extrinsic and the `DataFeeder::feed_value` trait path, rather than gating only the extrinsic entry point. Alternatively, bound the maximum aggregate price movement `CombineData::combine_data` will accept within a fixed block/time window regardless of how many times `do_feed_values` is invoked, mirroring the recommended time/block-based fix applied in the referenced Ion Protocol PR.

### Proof of Concept
Demonstrated directly in the existing pallet test:
```rust
// substrate/frame/honzon/oracle/src/tests.rs:180-207
assert_ok!(ModuleOracle::feed_values(RuntimeOrigin::signed(1), vec![(50, 1300)].try_into().unwrap()));

// Extrinsic path is correctly blocked a second time in the same block:
assert_noop!(
    ModuleOracle::feed_values(RuntimeOrigin::signed(1), vec![(50, 1300)].try_into().unwrap()),
    Error::<Test, _>::AlreadyFeeded,
);

// But the trait path bypasses the guard entirely and can be called repeatedly:
assert_ok!(ModuleOracle::feed_value(Some(1), 50, 1300));
``` [9](#0-8)

### Citations

**File:** substrate/frame/honzon/oracle/src/lib.rs (L285-295)
```rust
	/// A set of accounts that have already fed data in the current block.
	///
	/// This storage item tracks which oracle operators have already submitted data in the
	/// current block to enforce the "one submission per block" rule. This prevents spam and
	/// ensures fair participation among oracle operators.
	///
	/// The storage is cleared at the end of each block in the `on_finalize` hook, resetting
	/// the state for the next block.
	#[pallet::storage]
	pub(crate) type HasDispatched<T: Config<I>, I: 'static = ()> =
		StorageValue<_, BoundedBTreeSet<T::AccountId, T::MaxHasDispatchedSize>, ValueQuery>;
```

**File:** substrate/frame/honzon/oracle/src/lib.rs (L357-377)
```rust
		#[pallet::call_index(0)]
		#[pallet::weight(T::WeightInfo::feed_values(values.len() as u32))]
		pub fn feed_values(
			origin: OriginFor<T>,
			values: BoundedVec<(T::OracleKey, T::OracleValue), T::MaxFeedValues>,
		) -> DispatchResultWithPostInfo {
			let feeder = ensure_signed_or_root(origin.clone())?;

			let who = Self::ensure_account(feeder)?;

			// ensure account hasn't dispatched an updated yet
			<HasDispatched<T, I>>::try_mutate(|set| {
				set.try_insert(who.clone())
					.map_err(|_| Error::<T, I>::ExceedsMaxHasDispatchedSize)?
					.then_some(())
					.ok_or(Error::<T, I>::AlreadyFeeded)
			})?;

			Self::do_feed_values(who, values.into());
			Ok(Pays::No.into())
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

**File:** substrate/frame/honzon/oracle/src/lib.rs (L462-473)
```rust
impl<T: Config<I>, I: 'static> DataFeeder<T::OracleKey, T::OracleValue, T::AccountId>
	for Pallet<T, I>
{
	fn feed_value(
		who: Option<T::AccountId>,
		key: T::OracleKey,
		value: T::OracleValue,
	) -> DispatchResult {
		Self::do_feed_values(Self::ensure_account(who)?, vec![(key, value)]);
		Ok(())
	}
}
```

**File:** substrate/frame/honzon/oracle/src/tests.rs (L180-207)
```rust
#[test]
fn multiple_calls_should_fail() {
	new_test_ext().execute_with(|| {
		assert_ok!(ModuleOracle::feed_values(
			RuntimeOrigin::signed(1),
			vec![(50, 1300)].try_into().unwrap()
		));

		// Fails feeding by the extrinsic
		assert_noop!(
			ModuleOracle::feed_values(
				RuntimeOrigin::signed(1),
				vec![(50, 1300)].try_into().unwrap()
			),
			Error::<Test, _>::AlreadyFeeded,
		);

		// But not if fed thought the trait internally
		assert_ok!(ModuleOracle::feed_value(Some(1), 50, 1300));

		ModuleOracle::on_finalize(1);

		assert_ok!(ModuleOracle::feed_values(
			RuntimeOrigin::signed(1),
			vec![(50, 1300)].try_into().unwrap()
		));
	});
}
```
