### Title
Single global `ExpiresIn` staleness constant applied to all oracle keys causes stale-price acceptance or false-DoS in `pallet-oracle` — (File: `substrate/frame/honzon/oracle/src/default_combine_data.rs`)

### Summary
`pallet-oracle`'s `DefaultCombineData` aggregation strategy uses one single `ExpiresIn` constant to decide freshness for *every* `OracleKey`, even though the pallet is explicitly designed to serve heterogeneous data feeds (different currency/asset pairs) with different natural update frequencies. This is the exact bug class from the external report: a single staleness/heartbeat period applied uniformly across assets that update at different rates.

### Finding Description
`DefaultCombineData<T, MinimumCount, ExpiresIn, I>` filters raw values before computing the median: [1](#0-0) 

```rust
fn combine_data(
    _key: &<T as Config<I>>::OracleKey,
    mut values: Vec<TimestampedValueOf<T, I>>,
    prev_value: Option<TimestampedValueOf<T, I>>,
) -> Option<TimestampedValueOf<T, I>> {
    let expires_in = ExpiresIn::get();
    let now = T::Time::now();
    values.retain(|x| x.timestamp.saturating_add(expires_in) > now);
    ...
```

Note that `_key` (the `OracleKey`, i.e. the asset/currency identifier) is explicitly ignored — `expires_in` is a single runtime-wide constant, not a per-key value. This constant is wired into the runtime as one number for the whole pallet instance: [2](#0-1) 

```rust
impl pallet_oracle::Config for Runtime {
    type OnNewData = ();
    type CombineData = pallet_oracle::DefaultCombineData<Self, ConstU32<5>, ConstU64<3600>>;
    type Time = Timestamp;
    type OracleKey = u32;
    type OracleValue = u128;
    ...
```

`OracleKey = u32` is intended to represent arbitrary currency/asset pairs (per the pallet's own docs: "OracleKey is used to identify the data being fed (e.g., a specific currency pair)") [3](#0-2) , yet one 3600-second (1 hour) expiry is applied to all of them, mirroring the report's stablecoin (86400s heartbeat) vs. ETH/BTC (3600s heartbeat) mismatch scenario. This pallet backs the "Polkadot Stablecoin on AssetHub" effort [4](#0-3) , i.e. it is intended to feed asset-pricing data used for stablecoin-related asset accounting.

The `combine_data` function is invoked on every `feed_values`/`feed_value` submission and its result (`Values` storage) is what any downstream consumer reads via `DataProvider::get` / `DataProviderExtended::get_all_values`: [5](#0-4) [6](#0-5) 

Because there is only one `ExpiresIn` for the whole pallet instance:
- If configured tight enough for frequently-updated feeds (e.g. ETH/BTC at 1h), any less-frequently-updated feed (e.g. a stablecoin peg checked once per day) will have its submitted values discarded by `values.retain(...)` before the minimum-count threshold is reached, causing `combine_data` to fall back to `prev_value` — and once `prev_value` itself ages past `expires_in`, `Values` for that key stops updating even though `RawValues` were legitimately fed, denying any consumer relying on a "fresh enough" reading for that key.
- If configured loose enough for slow feeds, values for fast-moving assets that should be treated as stale are instead accepted as fresh and get included in the median calculation, i.e. genuinely stale data is fed into whatever pallet consumes `Oracle::get`.

There is no per-`OracleKey` freshness parameter anywhere in the `Config`, `CombineData` trait, or the `DefaultCombineData` generic parameters — `ExpiresIn: Get<MomentOf<T, I>>` is a single scalar, not a map: [7](#0-6) 

### Impact Explanation
Any runtime consumer built on top of `pallet-oracle::DataProvider`/`DataProviderExtended` for multi-asset pricing (the pallet's explicitly documented use case, and the reason it exists for the AssetHub stablecoin work) inherits this flaw: it either serves stale prices for fast-updating assets (degrading intended pricing/settlement behavior for stablecoin or asset-accounting logic downstream) or silently stops updating aggregated values for slower-updating assets, denying availability of price data for those assets to any dependent extrinsic/logic. This matches "runtime bugs that compromise intended behavior" and potential DoS of public functions gated on oracle freshness.

### Likelihood Explanation
This is a configuration-shape defect baked into the pallet's public API itself (`DefaultCombineData` generic signature has no room for per-key expiry), not just a bad runtime parameter choice — any runtime author using the provided default implementation with more than one type of asset/feed with differing update cadences will reproduce the issue by construction. No privileged actor, governance action, or malicious peer is required; it manifests purely from normal oracle operators feeding legitimate data at their asset's natural cadence.

### Recommendation
Change `CombineData`/`DefaultCombineData` to accept a per-`OracleKey` staleness lookup (e.g. `ExpiresIn: Get<(Key) -> Moment>` or a storage/config map keyed by `OracleKey`) instead of a single pallet-wide constant, and update `combine_data` to use `_key` when filtering `values.retain(...)`. Add tests exercising multiple keys with different configured heartbeats to verify neither false-stale rejection nor false-fresh acceptance occurs.

### Proof of Concept
1. Configure `pallet-oracle` in a runtime with `DefaultCombineData<Runtime, MinimumCount, ConstU64<3600>>` (as done in `substrate/bin/node/runtime/src/lib.rs`) and register two `OracleKey`s: `KEY_FAST` (real heartbeat 3600s, e.g. ETH/USD) and `KEY_SLOW` (real heartbeat 86400s, e.g. USDC/USD).
2. Oracle operators feed `KEY_SLOW` once per day, exactly as intended for that asset.
3. After ~3601 seconds since the last `KEY_SLOW` feed, call `feed_values` for `KEY_FAST` (triggers `combine_data` internal bookkeeping is per-key, but any consumer reading `Oracle::get(KEY_SLOW)` at t > 3600s since last feed observes `Values::<T,I>::get(KEY_SLOW)` retained from `prev_value`, which is only refreshed if new incoming raw values pass the `retain` filter using the *same* 3600s window)
4. Because `KEY_SLOW`'s legitimately-fresh (but >3600s old) raw values are filtered out by `values.retain(|x| x.timestamp.saturating_add(expires_in) > now)`, `combine_data` returns `prev_value`, and once `prev_value` itself also exceeds 3600s old, any downstream pallet calling `DataProvider::get(&KEY_SLOW)` either receives stale data indefinitely or a function gated on "data must be fresh" reverts/denies service for `KEY_SLOW`, despite the oracle operators behaving correctly per that asset's real update cadence — demonstrating the uniform-staleness DoS/incorrect-acceptance analog to the original report.

### Citations

**File:** substrate/frame/honzon/oracle/src/default_combine_data.rs (L25-37)
```rust
pub struct DefaultCombineData<T, MinimumCount, ExpiresIn, I = ()>(
	marker::PhantomData<(T, I, MinimumCount, ExpiresIn)>,
);

impl<T, I, MinimumCount, ExpiresIn>
	CombineData<<T as Config<I>>::OracleKey, TimestampedValueOf<T, I>>
	for DefaultCombineData<T, MinimumCount, ExpiresIn, I>
where
	T: Config<I>,
	I: 'static,
	MinimumCount: Get<u32>,
	ExpiresIn: Get<MomentOf<T, I>>,
{
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

**File:** substrate/bin/node/runtime/src/lib.rs (L3152-3165)
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
```

**File:** substrate/frame/honzon/oracle/README.md (L14-23)
```markdown
- **Oracle Operators**: A set of trusted accounts that are authorized to submit data to the oracle. The pallet
  uses the `frame_support::traits::SortedMembers` trait to manage the set of operators. This allows using pallets
  like `pallet-membership` to manage the oracle members.
- **Data Feeds**: Operators feed data as key-value pairs. The `OracleKey` is used to identify the data being fed
  (e.g., a specific currency pair), and the `OracleValue` is the data itself (e.g., the price).
- **Data Aggregation**: The pallet can be configured with a `CombineData` implementation to aggregate the raw
  values submitted by individual operators into a single, trusted value. A default implementation
  `DefaultCombineData` is provided, which takes the median of the values.
- **Timestamped Data**: All data submitted to the oracle is timestamped, allowing consumers of the data to know
  how fresh it is.
```

**File:** prdoc/stable2512/pr_9815.prdoc (L6-9)
```text
doc:
  - audience: Runtime Dev
    description: |
      This PR is part of #9765 - Polkadot Stablecoin on AssetHub. It introduces `pallet-oracle`, a new FRAME pallet that provides a decentralized and trustworthy way to bring external, off-chain data onto the blockchain. The pallet allows a configurable set of oracle operators to feed data, such as prices, into the system, which can then be consumed by other pallets.
```

**File:** substrate/frame/honzon/oracle/src/lib.rs (L400-428)
```rust
	fn combined(key: &T::OracleKey) -> Option<TimestampedValueOf<T, I>> {
		let values = Self::read_raw_values(key);
		T::CombineData::combine_data(key, values, Self::values(key))
	}

	fn ensure_account(who: Option<T::AccountId>) -> Result<T::AccountId, DispatchError> {
		// ensure feeder is authorized
		if let Some(who) = who {
			ensure!(T::Members::contains(&who), Error::<T, I>::NoPermission);
			Ok(who)
		} else {
			Ok(Self::get_pallet_account())
		}
	}

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
