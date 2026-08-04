### Title
No staleness/liveness check on Oracle aggregated price before trusting it for financial decisions - (File: `substrate/frame/honzon/oracle/src/lib.rs`, `substrate/frame/honzon/oracle/src/default_combine_data.rs`)

### Summary
The external report's core broken invariant is: an oracle-consuming contract trusts price data without any check that the underlying data source is still live/fresh, allowing indefinitely stale prices to be used for solvency-critical decisions. `pallet-oracle` (`substrate/frame/honzon/oracle`) exhibits the same broken invariant: its `DefaultCombineData::combine_data` silently falls back to the previous aggregated value with no freshness check on that fallback, and the pallet's `DataProvider` implementation — the interface any consuming pallet is expected to use — strips the timestamp entirely, making it impossible for a downstream consumer to detect staleness at all.

### Finding Description
`DefaultCombineData::combine_data` filters only the newly submitted raw values for freshness: [1](#0-0) 

```rust
values.retain(|x| x.timestamp.saturating_add(expires_in) > now);

let count = values.len() as u32;
let minimum_count = MinimumCount::get();
if count < minimum_count || count == 0 {
    return prev_value;
}
```

If fewer than `MinimumCount` operators have fed a fresh value (e.g. operators go offline, the oracle membership set shrinks, or there's simply low feed activity for an infrequently-queried key), the function returns `prev_value` — the previously aggregated `TimestampedValue` — with **no check on how old `prev_value.timestamp` is**. This value can be arbitrarily old and will keep being returned as "the" aggregated value on every subsequent call to `Values::<T,I>::get`/`Pallet::get`.

Compounding this, the pallet's generic `DataProvider` implementation, which is the documented integration point for other pallets (per the pallet's own README: "other pallets can use the `DataProvider` trait to read the aggregated data"), discards the timestamp entirely: [2](#0-1) 

```rust
impl<T: Config<I>, I: 'static> DataProvider<T::OracleKey, T::OracleValue> for Pallet<T, I> {
	fn get(key: &T::OracleKey) -> Option<T::OracleValue> {
		Self::get(key).map(|timestamped_value| timestamped_value.value)
	}
}
```

Any consumer that uses the standard `DataProvider` trait (rather than reaching into pallet-oracle-specific storage directly) receives only the raw value, with zero ability to check that it isn't stale — the exact analog of the WSTETHOracle bug: a client trusts the value returned by an oracle-like source with no mechanism to verify liveness/freshness of the underlying feed before using it in a financial decision (e.g. valuing collateral, computing swap/borrow limits).

### Impact Explanation
Any pallet built against pallet-oracle's `DataProvider` trait (the pallet's intended generic integration surface) inherits this blind spot: it cannot distinguish a freshly-aggregated price from a price that has not been updated in hours/days because oracle operators stopped feeding data (network issue, governance removed members, chain congestion, etc.). This mirrors the WSTETHOracle finding's impact pattern — stale data being trusted for solvency-critical math (collateral valuation, borrow limits, liquidation thresholds) — which could allow users to borrow/withdraw more than they should, or avoid liquidation, based on outdated collateral pricing, once any lending/PSM-like pallet wires itself to this `DataProvider` interface as the pallet's docs recommend.

### Likelihood Explanation
This requires no privileged actor, malicious relayer, or governance abuse — it is a design gap in the default aggregation fallback and the standard trait implementation that ships in-tree and is wired into the kitchensink runtime (`pallet_oracle::Config` in `substrate/bin/node/runtime/src/lib.rs`). It manifests any time oracle feed liveness degrades (a routine, expected operational condition, not an attack), so likelihood of the stale-data condition occurring is high; the only missing piece is a downstream consumer pallet built on the `DataProvider` trait rather than on the raw `TimestampedValue` storage.

### Recommendation
- Add an explicit staleness check before returning `prev_value` in `DefaultCombineData::combine_data`, e.g. reject/return `None` if `prev_value.timestamp.saturating_add(expires_in) <= now`, mirroring the freshness filter already applied to fresh submissions.
- Change (or add a variant of) the `DataProvider` implementation to preserve the timestamp, or expose a `DataProviderExtended`/timestamp-aware API as the canonical integration surface, and document that consumers MUST check `timestamp` freshness before trusting the value for financial logic — the same fix pattern as adding the L2 sequencer liveness check in the referenced report.

### Proof of Concept
1. Configure `pallet-oracle` with `DefaultCombineData<T, MinimumCount=3, ExpiresIn=3600>` (as done in `substrate/bin/node/runtime/src/lib.rs`, `ConstU32<5>`/`ConstU64<3600>`).
2. Oracle members feed a price for key `K` at `t=0`; `Values::<T>::get(K)` becomes `{value: P0, timestamp: 0}`.
3. Time advances far beyond `ExpiresIn` (e.g. `t=100000`) and fewer than `MinimumCount` fresh values are fed for `K` (e.g. two of five operators stop feeding).
4. `combined()` filters out the stale raw submissions, `count < MinimumCount`, so `combine_data` returns `prev_value = {value: P0, timestamp: 0}` unchanged.
5. A pallet calling `<pallet_oracle::Pallet<T> as DataProvider<_,_>>::get(&K)` receives `P0` with no indication it is 100000 time units stale, and uses it as if it were current — reproducing the "trust stale oracle data" condition from the external report.

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

**File:** substrate/frame/honzon/oracle/src/lib.rs (L449-453)
```rust
impl<T: Config<I>, I: 'static> DataProvider<T::OracleKey, T::OracleValue> for Pallet<T, I> {
	fn get(key: &T::OracleKey) -> Option<T::OracleValue> {
		Self::get(key).map(|timestamped_value| timestamped_value.value)
	}
}
```
