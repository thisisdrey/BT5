### Title
`pallet-oracle`'s median `Values` aggregate can go arbitrarily stale with no expiry or staleness signal exposed to consumers - ([File: substrate/frame/honzon/oracle/src/default_combine_data.rs])

### Summary
The newly introduced `pallet-oracle` (`substrate/frame/honzon/oracle`) aggregates trusted-operator price feeds into a single on-chain value using a median (`DefaultCombineData`). Just like the Open Oracle report describes, this median-of-N design only updates once enough fresh reports arrive. In this implementation, if fewer than `MinimumCount` operators have submitted a *fresh* (non-expired) value in a given block, the pallet silently falls back to returning the **previous** aggregated value forever — with no upper bound on how stale that fallback value can become, and no way for a downstream consumer using the plain `DataProvider` interface to detect that the value is stale at all.

### Finding Description
`DefaultCombineData::combine_data` filters out expired raw values, and if the number of *remaining fresh* values is below `MinimumCount`, it just returns `prev_value` unconditionally: [1](#0-0) 

Critically, `prev_value` here is whatever is currently stored in `Values<T, I>` — which may itself have been produced arbitrarily long ago, by another earlier fallback. There is no timestamp-based ceiling that ever forces `Values` to become `None`/cleared when it becomes too old; staleness can compound block after block indefinitely as long as fewer than `MinimumCount` operators keep reporting fresh data (e.g. during operator downtime, membership churn via `ChangeMembers`, or simply during a period of extreme, fast-moving price action exactly as described in the external report).

Compounding this, the public `DataProvider` trait implementation that other pallets are expected to consume strips the timestamp entirely and exposes only the raw value: [2](#0-1) 

Only the separate `DataProviderExtended::get_all_values()` path retains timestamps: [3](#0-2) 

So any pallet that integrates via the standard `DataProvider::get(key)` API (the interface explicitly documented as the intended integration point) has **no mechanism whatsoever** to know whether the value it just read is fresh or ancient. The pallet's own doc comments frame `CombineData`/timestamping as the freshness safety mechanism: [4](#0-3) 
but that mechanism is not actually enforced end-to-end — it only prevents *stale raw inputs* from being counted in a new median calculation; it does nothing to prevent an old *aggregate* from being served indefinitely once quorum drops.

### Impact Explanation
This pallet is explicitly built (per its own PR description) to feed pricing data into other consuming pallets as part of the Polkadot Stablecoin on Asset Hub work. Any future or existing consumer that trusts `DataProvider::get()` for financial decisions (minting, redemption pricing, liquidation thresholds, collateral valuation) risks acting on a price that is stale by an unbounded number of blocks during exactly the kind of congested/volatile conditions where correctness matters most — mirroring the Open Oracle report's core concern that the median fails to track fast market moves in a timely way, except here it's compounded by a complete lack of a staleness signal on the primary consumption interface. This is a runtime-level defect that compromises the pallet's intended behavior (delivering "trusted", timely aggregated data), not a theoretical off-chain issue.

### Likelihood Explanation
No malicious actor, governance, or privileged access is required. Ordinary, honest operating conditions — a temporary drop in the number of active feeders below `MinimumCount` (member removal via `ChangeMembers`, an operator missing a block, or simply market conditions causing the freshness window `ExpiresIn` to filter out all raw entries) — are sufficient to trigger indefinite staleness of the reported aggregate, with zero indication to any downstream consumer using the standard `DataProvider` interface.

### Recommendation
- Track a "last successfully updated" timestamp for the `Values` storage per key, and have `DataProvider::get()` (or a new bounded variant) refuse to serve values older than a configurable max-staleness threshold.
- Expose the timestamp in the primary `DataProvider` trait (or make `DataProviderExtended` the only consumption path) so consumers can enforce their own staleness policy rather than being forced to trust an opaque raw value.
- Consider having `combine_data` return `None` (rather than silently reusing `prev_value`) once the previous value's own timestamp has exceeded `ExpiresIn`, so `Values` is cleared instead of perpetually re-serving an outdated median.

### Proof of Concept
1. Configure `MinimumCount = 5`, `ExpiresIn = 3600` (as wired in `kitchensink-runtime`): [5](#0-4) 
2. Five operators feed a price for `key`, producing a valid median stored in `Values<T,I>` at `timestamp = t0`.
3. Over the next several hours (or blocks, in test time), 3 of the 5 operators go offline/are removed via `change_members_sorted` (see `should_clear_data_for_removed_members`), leaving only 2 active — below `MinimumCount`.
4. Time passes well beyond `ExpiresIn`, so all *raw* values expire and are filtered by `values.retain(...)` in `combine_data`.
5. `count < minimum_count` is true, so `combine_data` returns `prev_value` — the same `t0` median — forever, block after block, regardless of how far the real price has since moved.
6. Any pallet calling `Oracle::get(key)` via `DataProvider::get()` receives this `t0` value with no way to know it is stale, since the timestamp is not part of that interface's return type.

### Citations

**File:** substrate/frame/honzon/oracle/src/default_combine_data.rs (L43-52)
```rust
		let expires_in = ExpiresIn::get();
		let now = T::Time::now();

		values.retain(|x| x.timestamp.saturating_add(expires_in) > now);

		let count = values.len() as u32;
		let minimum_count = MinimumCount::get();
		if count < minimum_count || count == 0 {
			return prev_value;
		}
```

**File:** substrate/frame/honzon/oracle/src/lib.rs (L41-49)
```rust
//! ### Key Concepts
//!
//! * **Oracle Operators**: A set of trusted accounts authorized to submit data. Managed through the
//!   [`SortedMembers`] trait, allowing integration with membership pallets.
//! * **Data Feeds**: Key-value pairs where keys identify the data type (e.g., currency pair) and
//!   values contain the actual data (e.g., price).
//! * **Data Aggregation**: Configurable algorithms to combine multiple operator inputs into a
//!   single trusted value, with median aggregation provided by default.
//! * **Timestamped Data**: All submitted data includes timestamps for freshness tracking.
```

**File:** substrate/frame/honzon/oracle/src/lib.rs (L449-453)
```rust
impl<T: Config<I>, I: 'static> DataProvider<T::OracleKey, T::OracleValue> for Pallet<T, I> {
	fn get(key: &T::OracleKey) -> Option<T::OracleValue> {
		Self::get(key).map(|timestamped_value| timestamped_value.value)
	}
}
```

**File:** substrate/frame/honzon/oracle/src/lib.rs (L454-460)
```rust
impl<T: Config<I>, I: 'static> DataProviderExtended<T::OracleKey, TimestampedValueOf<T, I>>
	for Pallet<T, I>
{
	fn get_all_values() -> impl Iterator<Item = (T::OracleKey, Option<TimestampedValueOf<T, I>>)> {
		<Values<T, I>>::iter().map(|(k, v)| (k, Some(v)))
	}
}
```

**File:** substrate/bin/node/runtime/src/lib.rs (L3152-3158)
```rust
impl pallet_oracle::Config for Runtime {
	type OnNewData = ();
	type CombineData = pallet_oracle::DefaultCombineData<Self, ConstU32<5>, ConstU64<3600>>;
	type Time = Timestamp;
	type OracleKey = u32;
	type OracleValue = u128;
	type PalletId = OraclePalletId;
```
