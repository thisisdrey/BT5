### Title
`pallet-oracle`'s `DataProvider` trait strips price staleness data, letting consumers act on unbounded-age oracle values - (File: substrate/frame/honzon/oracle/src/lib.rs, substrate/frame/honzon/oracle/src/traits.rs)

### Summary
`pallet-oracle` (introduced in [1](#0-0) , intended to feed price data for the upcoming Polkadot Stablecoin on AssetHub) exposes two ways to read aggregated price data: the pallet's own `get`/`get_value`, which returns the full `TimestampedValue` (value + timestamp), and the `DataProvider` trait implementation, which is the interface the pallet's own documentation tells consumer pallets to use, and which silently discards the timestamp entirely.

### Finding Description
The pallet computes and stores an aggregated median value on every `feed_values` call via `combined()`/`DefaultCombineData::combine_data`, which does filter *raw* per-operator submissions by `ExpiresIn` before aggregating: [2](#0-1) . However, that filtering only happens at the moment new data is fed. The resulting aggregate is written to the `Values` storage item and is returned unconditionally by `Pallet::get`, with no re-check of staleness at read time: [3](#0-2) .

The pallet's `DataProvider` implementation — the interface the README explicitly tells other pallets to consume ("other pallets can use the `DataProvider` trait to read the aggregated data") — then discards the timestamp altogether: [4](#0-3) 
and the generic trait signature itself has no timestamp/freshness channel: [5](#0-4) 

This is the exact structural analog of the reported Chainlink issue: `latestAnswer()` silently returns a value with no completeness/staleness signal, while `latestRoundData()` (here, `get_value`/`TimestampedValueOf`) carries that information but is not the one consumers are steered toward using. Any pallet built against the documented `DataProvider<Key, Value>` interface (the "correct"/recommended integration point per the pallet's own docs) has structurally no way to detect that:
- the on-chain median has not been refreshed for an arbitrarily long time (e.g. all oracle operators stop feeding, or are removed from `Members` without a new median ever being recomputed — `combined()` is only invoked from `do_feed_values`, so `Values` is never cleared or invalidated on operator removal, it just stops updating), or
- the single remaining value being returned is far outside the `ExpiresIn` window that was used to validate freshness at aggregation time.

### Impact Explanation
Since this pallet is explicitly being introduced as the price-data backbone for the Polkadot Stablecoin work on AssetHub, any downstream pallet (collateral valuation, liquidation triggers, minting/redemption logic) that follows the documented integration pattern and consumes prices exclusively through `DataProvider::get` will operate on stale prices with no built-in guard, which can lead to incorrect collateralization decisions, unbacked minting, or failure to liquidate — i.e., the "theft or unbacked mint" / "runtime bugs that compromise intended behavior" impact classes. This is a genuine interface-level defect already merged into the runtime crate, not a hypothetical external protocol issue.

### Likelihood Explanation
Likelihood is moderate: exploitation does not require a malicious oracle operator, validator, or governance actor — it only requires oracle operators to go offline/stop submitting (a routine operational condition, not an attack), after which any consumer using the recommended `DataProvider` interface will keep receiving the last (now stale) value indefinitely, with no on-chain signal to fall back or halt. The severity depends on which downstream consumer eventually wires into `DataProvider`; as of this scan no stablecoin/collateral pallet has landed yet, but the interface contract is already fixed and documented as the intended consumption path.

### Recommendation
- Change the documented/primary integration path to require timestamped data (`DataProviderExtended`/`TimestampedValueOf`) rather than the bare `DataProvider::get`, or add a staleness parameter to `DataProvider` itself.
- Have `Pallet::get`/`get_value` re-validate `timestamp.saturating_add(ExpiresIn) > now` at read time (not just at aggregation time) and return `None` if stale, mirroring a `latestRoundData`-style completeness check.
- Document and enforce that any consumer must check the timestamp and refuse to act on prices older than an application-defined bound.

### Proof of Concept
1. Configure `pallet-oracle` with `Members = {A, B, C}` and `ExpiresIn = T`.
2. Operators feed prices normally; `Values::get(key)` holds a fresh median.
3. All three operators stop calling `feed_values` (e.g. removed via governance from `Members`, or simply go offline) — no malicious action required.
4. `combined()` is never invoked again (it only runs inside `do_feed_values`), so `Values` storage is frozen at the last computed value forever.
5. Any consumer pallet using `<OraclePallet as DataProvider<Key, Value>>::get(&key)` (per [4](#0-3) ) receives this frozen value with no timestamp and no way to know it is stale, and will use it as if it were current — reproducing the "deprecated `latestAnswer` silently returns outdated data" pattern from the source report.

### Citations

**File:** prdoc/stable2512/pr_9815.prdoc (L4-9)
```text
title: Introduce `pallet-oracle`

doc:
  - audience: Runtime Dev
    description: |
      This PR is part of #9765 - Polkadot Stablecoin on AssetHub. It introduces `pallet-oracle`, a new FRAME pallet that provides a decentralized and trustworthy way to bring external, off-chain data onto the blockchain. The pallet allows a configurable set of oracle operators to feed data, such as prices, into the system, which can then be consumed by other pallets.
```

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

**File:** substrate/frame/honzon/oracle/src/lib.rs (L395-398)
```rust
	/// Returns the aggregated and timestamped value for a given key.
	pub fn get(key: &T::OracleKey) -> Option<TimestampedValueOf<T, I>> {
		Self::values(key)
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

**File:** substrate/frame/honzon/oracle/src/traits.rs (L29-33)
```rust
/// A simple trait for providing data.
pub trait DataProvider<Key, Value> {
	/// Returns the data for a given key.
	fn get(key: &Key) -> Option<Value>;
}
```
