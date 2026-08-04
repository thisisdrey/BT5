## Analysis

The Chainlink report's broken invariant is: **a price feed is consumed without checking whether the underlying data source is still live/fresh**, so a caller silently accepts stale data as if it were current.

The closest verifiable analog in this repo is in `pallet-oracle`. The pallet timestamps every value it stores specifically so that "consumers of the data ... know how fresh it is," but the primary consumer-facing interface throws that information away.

### Root cause

`DefaultCombineData::combine_data` filters *raw* per-operator submissions by `ExpiresIn`, but if fewer than `MinimumCount` fresh submissions remain, it silently falls back to whatever was previously aggregated, with no re-check of that fallback's own age: [1](#0-0) 

That aggregated value (with its timestamp) is stored in `Values<T, I>`. The pallet's `DataProvider` implementation — the interface documented as the way "other pallets can easily consume the oracle data" — then hands that value to callers **with the timestamp stripped out**: [2](#0-1) 

So the full path is: `feed_values` → `do_feed_values` → `combined` (falls back to `prev_value` if quorum of fresh feeds is lost) → `Values` storage → `DataProvider::get()` (drops the timestamp entirely). Any pallet that only calls `DataProvider::get()` (the standard, recommended API per the pallet's own docs) has no mechanism to detect that the value it received may be arbitrarily old, exactly mirroring `getPrice()` in the report calling `latestRoundData()` without checking `updatedAt`/sequencer liveness. [3](#0-2) 

### Why existing guards don't stop it

- `MinimumCount`/`ExpiresIn` only gate whether a *new* aggregation is computed; they never invalidate an already-stored stale `Values` entry.
- `DataProviderExtended::get_all_values()` does expose the `TimestampedValue`, but `DataProvider::get()` — the simpler, more commonly implemented trait — does not, and nothing in the pallet forces consumers to use the extended trait or check freshness themselves.
- This is not a malicious-operator, governance, or admin scenario: it triggers purely from operators going idle/offline (e.g., all but a couple of feeders stop submitting, dropping below `MinimumCount`), which is an ordinary operational condition, not an attack requiring privileged access.

Note: `pallet-psm` in this repo does **not** use the oracle (it's a fixed decimals-based 1:1 peg module, confirmed by inspecting `substrate/frame/psm/src/lib.rs`), so the impact here is scoped to the oracle pallet's public API contract itself rather than a specific downstream financial pallet in-tree. Any future or external consumer wiring `DataProvider::get()` for pricing (as the crate is explicitly designed for, e.g. currency-pair prices per its own docs) inherits this staleness blind spot.

### Title
Oracle `DataProvider::get()` discards timestamp, letting stale aggregated prices be silently consumed - (File: `substrate/frame/honzon/oracle/src/lib.rs`)

### Summary
`pallet-oracle`'s `combine_data` can fall back to a previously aggregated `TimestampedValue` when too few fresh submissions exist, and `DataProvider::get()` returns only the raw `value`, stripping the `timestamp`. Consumers using the pallet's primary data-access trait have no way to detect that the price they receive is stale, mirroring the Chainlink report's missing liveness check on an external data feed.

### Finding Description
`DefaultCombineData::combine_data` (`substrate/frame/honzon/oracle/src/default_combine_data.rs:38-59`) filters raw values by `ExpiresIn`, but when the resulting count is below `MinimumCount`, it returns `prev_value` unconditionally — without checking whether `prev_value` itself has expired. This value is written into `Values<T, I>` storage by `do_feed_values` (`substrate/frame/honzon/oracle/src/lib.rs:415-429`). The `DataProvider` implementation (`substrate/frame/honzon/oracle/src/lib.rs:449-453`) then returns only `.value`, discarding `.timestamp`, which is the only signal a caller could use to judge freshness.

### Impact Explanation
Any pallet consuming oracle-fed prices via the standard `DataProvider::get()` API (the interface the pallet's own documentation directs integrators to use) receives no freshness signal. If oracle operators stop feeding data (offline, removed via membership changes, or simply inactive) and the feed count drops below `MinimumCount`, the last aggregated value keeps being served indefinitely as if current. A consuming pallet using this for balance/valuation logic would use out-of-date pricing indefinitely, which under this program's impact criteria falls under "runtime bugs that compromise intended behavior" for value conservation/settlement correctness.

### Likelihood Explanation
This requires no privileged actor, admin, governance action, or malicious peer — it is triggered by ordinary operational conditions (oracle operators going idle, e.g., after a `ChangeMembers` removal or feeders simply stopping submissions), which is a realistic and unprivileged occurrence in any long-running deployment.

### Recommendation
Either (a) make `DataProvider::get()` return `None`/error once the underlying `TimestampedValue.timestamp` exceeds `ExpiresIn` relative to `T::Time::now()`, instead of blindly stripping and returning stale values, or (b) require consumers to use `DataProviderExtended`/raw timestamped storage and explicitly validate freshness before use, and clear `Values` (rather than falling back to `prev_value`) once a key's data has aged past `ExpiresIn` with no fresh quorum.

### Proof of Concept
1. Configure `Config::CombineData = DefaultCombineData<T, MinimumCount, ExpiresIn>` with e.g. `MinimumCount = 3`.
2. Three operators feed a price at time `t0`; `Values` stores `TimestampedValue { value: P, timestamp: t0 }`.
3. Advance time past `ExpiresIn`, and let all but one operator stop submitting (or get removed via `ChangeMembers`).
4. Any subsequent `feed_values` call from the remaining operator triggers `combined()` → `combine_data` with `count < MinimumCount` → returns `prev_value` (`{P, t0}`) unchanged.
5. A pallet calling `Oracle::get(key)` (`DataProvider::get`) still receives `P` with no indication it is now older than `ExpiresIn`, and continues using it as if fresh — matching the report's "no verification the feed is live" pattern.

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

**File:** substrate/frame/honzon/oracle/README.md (L17-23)
```markdown
- **Data Feeds**: Operators feed data as key-value pairs. The `OracleKey` is used to identify the data being fed
  (e.g., a specific currency pair), and the `OracleValue` is the data itself (e.g., the price).
- **Data Aggregation**: The pallet can be configured with a `CombineData` implementation to aggregate the raw
  values submitted by individual operators into a single, trusted value. A default implementation
  `DefaultCombineData` is provided, which takes the median of the values.
- **Timestamped Data**: All data submitted to the oracle is timestamped, allowing consumers of the data to know
  how fresh it is.
```
