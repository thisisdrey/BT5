### Title
Single fixed `ExpiresIn` staleness window shared across all oracle keys allows stale price acceptance - (File: `substrate/frame/honzon/oracle/src/default_combine_data.rs`)

### Summary
`pallet-oracle`'s default data-combination logic, `DefaultCombineData`, filters raw operator submissions using one global `ExpiresIn` constant applied uniformly to every `OracleKey`, exactly mirroring the Renzo bug where a single `MAX_TIME_WINDOW` (24h+60s) was used to validate freshness for all tokens/chains regardless of their actual update cadence.

### Finding Description
`DefaultCombineData::combine_data` retains only raw values whose `timestamp + expires_in > now`, where `expires_in` is a single `Get<MomentOf<T, I>>` type parameter shared by the whole pallet instance, not scoped per `OracleKey`: [1](#0-0) 

In the reference runtime wiring, this is configured as one flat constant for the entire oracle instance regardless of how many distinct currency pairs / assets are fed through it: [2](#0-1) 

The pallet's `OracleKey`/`OracleValue` design explicitly supports many heterogeneous feeds ("e.g., a specific currency pair") sharing one `CombineData` implementation and therefore one `ExpiresIn`: [3](#0-2) 

This is the direct structural analog of the Renzo `M-03` finding: Renzo used one fixed `MAX_TIME_WINDOW` for all ezETH-style feeds across chains with different real heartbeats (24h on Ethereum, 6h on Arbitrum, 24h on Linea), so the same window was simultaneously too loose for fast-moving feeds and too tight for slow ones. Here, `pallet-oracle` uses one fixed `ExpiresIn` for every `OracleKey` configured on the pallet instance — different assets/currency pairs can have very different natural feeding cadences (e.g. a stable/low-volatility pair updated hourly vs. a volatile pair expected every block), yet the pallet has no per-key staleness configuration. When `expires_in` is tuned for the fastest-moving feed, slower feeds retain and combine values that are effectively stale relative to their own market conditions; when tuned for the slowest feed, fast-moving feeds accept overly old data as fresh. Because `Values<T,I>` (the aggregated, "trusted" price consumed by downstream pallets via `DataProvider`/`DataProviderExtended`) is written directly from whatever `combine_data` accepts, any consuming pallet inherits stale-price risk for a subset of its configured `OracleKey`s.

### Impact Explanation
Any runtime pallet that consumes `pallet-oracle` through `DataProvider`/`DataProviderExtended` (e.g. a stability/lending/liquidation module built on this in-tree pallet) receives an aggregated value that can be up to `ExpiresIn` stale for at least one of its configured asset keys, because there is no way to express "asset A tolerates 5 minutes of staleness, asset B tolerates 24 hours" within a single pallet instance. This falls under "runtime bugs that compromise intended behavior" and can propagate into asset accounting (mispriced collateral, wrong liquidation/mint thresholds) exactly as the original Renzo report describes — the protocol consuming an outdated price because the staleness window was calibrated for a different feed's update frequency.

### Likelihood Explanation
This is not a peer/validator/admin-triggered condition — it is a structural design gap present for any runtime that instantiates `pallet-oracle` with more than one `OracleKey` whose real-world feed cadence differs, which is the pallet's advertised normal use case ("Data Feeds: Operators feed data as key-value pairs ... e.g., a specific currency pair"). No malicious actor is required; the mismatch triggers under ordinary operation whenever operators feed multiple asset types through one pallet instance with one `ExpiresIn`.

### Recommendation
Extend `CombineData`/`DefaultCombineData` (or add a new combinator) to accept a per-`OracleKey` staleness/expiry mapping, similar to the report's own recommendation of "storing a mapping that would record the heartbeat parameter for the stale period of each token." Concretely, replace the single `ExpiresIn: Get<MomentOf<T, I>>` with a `Get`-like trait keyed by `OracleKey`, or require runtimes to run one `pallet-oracle` instance per staleness class of assets.

### Proof of Concept
1. Configure `pallet-oracle` with `DefaultCombineData<T, MinimumCount, ConstU64<3600>>` (one hour) as in the reference runtime. [2](#0-1) 
2. Feed the same pallet instance two `OracleKey`s: `KEY_FAST` (expected update every block, e.g. a volatile asset) and `KEY_SLOW` (naturally updates only every 24h due to low volatility, mirroring ezETH's own oracle heartbeat).
3. Stop feeding `KEY_SLOW` for 55 minutes while `KEY_FAST` continues to be fed normally.
4. `combine_data` for `KEY_SLOW`, per the retain check `x.timestamp.saturating_add(expires_in) > now`, still accepts the 55-minute-old value as "fresh" because `expires_in = 3600s` is calibrated for `KEY_FAST`'s expected cadence, not `KEY_SLOW`'s actual (larger) natural staleness tolerance/risk profile — there is no per-key override to tighten or loosen this window. [4](#0-3) 
5. Any downstream pallet reading `Values::<T,I>::get(KEY_SLOW)` via `DataProvider::get` receives this value believing it satisfies the pallet's freshness guarantee, when in fact the single global window was never validated against `KEY_SLOW`'s specific feed characteristics — the same class of miscalibration documented in the Renzo `M-03` report.

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

**File:** substrate/frame/honzon/oracle/src/lib.rs (L183-193)
```rust
		/// The key type for identifying oracle data feeds.
		///
		/// This type is used to uniquely identify different types of oracle data (e.g., currency
		/// pairs, asset prices, weather data).
		type OracleKey: Parameter + Member + MaxEncodedLen;

		/// The value type for oracle data.
		///
		/// This type represents the actual data submitted by oracle operators (e.g., prices,
		/// temperatures, scores).
		type OracleValue: Parameter + Member + Ord + MaxEncodedLen;
```
