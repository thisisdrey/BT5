### Title
Oracle pallet applies a single hardcoded freshness window (`ExpiresIn`) to all `OracleKey`s, allowing stale per-asset price data to be aggregated and consumed by value-bearing pallets - (File: `substrate/frame/honzon/oracle/src/default_combine_data.rs`)

### Summary
`pallet-oracle`'s `DefaultCombineData::combine_data` filters raw price submissions for staleness using a single, pallet-wide `ExpiresIn` constant, completely ignoring the `OracleKey` (asset/currency pair) being aggregated. This is the same broken invariant as the reported `ChainlinkPriceOracle` bug: one fixed heartbeat/staleness interval is applied uniformly to feeds that legitimately require different freshness intervals, so data that is objectively stale for one asset is accepted as fresh because it fits within the generic threshold tuned for a different asset.

### Finding Description
`combine_data` computes freshness purely from a type-level constant, independent of which key is being processed: [1](#0-0) 

The `_key` parameter (the `OracleKey`, i.e. the specific asset/currency pair being priced) is explicitly unused - the underscore prefix confirms the staleness filter cannot differentiate between feeds. The only source of a threshold is the generic `ExpiresIn: Get<MomentOf<T, I>>` associated type, which is set once, globally, for the whole pallet instance: [2](#0-1) 

Here `ConstU64<3600>` (1 hour) is used for *every* `OracleKey = u32` fed into the pallet, regardless of the fact that different assets naturally have very different acceptable staleness windows (e.g. a highly volatile token pair needs a much shorter freshness window than a stable or thinly-traded pair, exactly as Chainlink's LINK/USD heartbeat of 3600s differs from other feeds). This mirrors the external report's root cause precisely: a single time-interval constant used indiscriminately across all price feeds.

The Oracle pallet's own documentation confirms the intended purpose of the timestamp is to guard "how fresh" the data is, and states the aggregation strategy is configurable per pallet instance - but the built-in `DefaultCombineData` provides no per-key granularity to actually realize that intent: [3](#0-2) 

Downstream, this aggregated (`Values<T,I>`) price is exposed unconditionally via the `DataProvider`/`DataProviderExtended` traits with no additional freshness metadata surfaced to consumers beyond the already-diluted "is it younger than the single global `ExpiresIn`" check: [4](#0-3) 

The kitchensink runtime wires this oracle directly into `pallet_psm` (Peg Stability Module) via `type OracleKey = u32`, i.e. the PSM's swap/mint/redeem logic for one or more asset pairs relies on the same single-hour freshness threshold regardless of the actual volatility or required freshness of the specific asset being priced: [5](#0-4) 

### Impact Explanation
Any pallet that consumes `pallet-oracle` prices for value-bearing operations (e.g. a PSM-style pallet that mints/redeems assets, or any future consumer using `DataProvider`) can be driven to accept a price for an asset that is stale relative to that asset's real volatility, because the aggregation step only checks "is this timestamp within the single, pallet-wide `ExpiresIn` window," not "is this timestamp fresh enough for this specific asset." This can cause incorrect mint/redeem amounts, mis-priced swaps, or other financial miscalculations built on outdated market data - a direct compromise of intended runtime behavior for any consumer pallet that assumes the oracle enforces per-asset freshness.

### Likelihood Explanation
This is not a byzantine-input attack; it requires no malicious oracle operator, admin, or governance action. It is a deterministic design flaw: as soon as multiple `OracleKey`s with materially different natural volatility/staleness requirements are fed through the same `Config<I>` instance (which is the pallet's standard, documented usage pattern - `OracleKey` is explicitly described as identifying "different types of oracle data (e.g., currency pairs...)"), the single `ExpiresIn` constant will necessarily be miscalibrated for at least one of them, silently accepting overly old data as "fresh" for any consumer relying on `Pallet::get`/`DataProvider::get`.

### Recommendation
Make the freshness threshold a function of `OracleKey` rather than a single associated constant: extend the `CombineData`/`Config` interface so `ExpiresIn` can be resolved per-key (e.g. via a `Get<(OracleKey, MomentOf<T,I>)>`-style trait or a storage/map-based per-key configuration), and update `combine_data` to use the key-specific value instead of a single global constant.

### Proof of Concept
1. Configure `pallet-oracle` (as kitchensink does) with `CombineData = DefaultCombineData<Runtime, ConstU32<5>, ConstU64<3600>>` and feed two distinct `OracleKey`s through the same instance: key `A` (e.g. a stablecoin pair that should only be considered fresh within ~60s) and key `B` (e.g. a low-volatility pair for which 3600s is appropriate).
2. Have oracle operators submit a price for key `A` at `t = 0`.
3. Advance the clock (`Timestamp::set_timestamp`) to `t = 3000` (50 minutes later) - well past any reasonable freshness window for a volatile asset like `A`, but still within the single hardcoded `ExpiresIn = 3600`.
4. Call `combine_data`/`Pallet::get(&A)` (as exercised in `substrate/frame/honzon/oracle/src/tests.rs::should_combined_data`, which shows the aggregated value being returned unchanged even after `Timestamp::set_timestamp(23456)` is advanced far past a reasonable freshness window): [6](#0-5) 
5. Any downstream pallet (e.g. `pallet_psm`, configured with this same `OracleKey`/`CombineData` in `substrate/bin/node/runtime/src/lib.rs:3152-3165`) reading `A`'s price via `DataProvider::get` receives this 50-minute-old value as valid, because the retain filter in `default_combine_data.rs:46` only compares against the single global `ExpiresIn`, never against a threshold appropriate to key `A`.

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

**File:** substrate/bin/node/runtime/src/lib.rs (L3152-3175)
```rust
impl pallet_oracle::Config for Runtime {
	type OnNewData = ();
	type CombineData = pallet_oracle::DefaultCombineData<Self, ConstU32<5>, ConstU64<3600>>;
	type Time = Timestamp;
	type OracleKey = u32;
	type OracleValue = u128;
	type PalletId = OraclePalletId;
	type Members = TechnicalMembership;
	type WeightInfo = ();
	type MaxHasDispatchedSize = OracleMaxHasDispatchedSize;
	type MaxFeedValues = OracleMaxFeedValues;
	#[cfg(feature = "runtime-benchmarks")]
	type BenchmarkHelper = OracleBenchmarkingHelper;
}

parameter_types! {
	/// PalletId for deriving the PSM system account.
	pub const PsmPalletId: PalletId = PalletId(*b"py/pegsm");
	/// Base deposit held for the footprint of a PSM created via `create_psm`.
	pub const PsmCreationDeposit: Balance = 10 * DOLLARS;
	/// Per-byte deposit slope; PSM footprints are fixed-size, so this is zero.
	pub const PsmDepositSlope: Balance = 0;
	pub PsmHoldReason: RuntimeHoldReason = RuntimeHoldReason::Psm(pallet_psm::HoldReason::CreationDeposit);
}
```

**File:** substrate/frame/honzon/oracle/src/lib.rs (L170-181)
```rust
		/// The implementation to combine raw values into a single aggregated value.
		///
		/// This type defines how multiple oracle operator submissions are combined into a single
		/// trusted value. Common implementations include taking the median (to resist outliers)
		/// or weighted averages based on operator reputation.
		type CombineData: CombineData<Self::OracleKey, TimestampedValueOf<Self, I>>;

		/// The time provider for timestamping oracle data.
		///
		/// This type provides the current timestamp used to mark when oracle data was submitted.
		/// Timestamps are crucial for determining data freshness and preventing stale data usage.
		type Time: Time;
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
