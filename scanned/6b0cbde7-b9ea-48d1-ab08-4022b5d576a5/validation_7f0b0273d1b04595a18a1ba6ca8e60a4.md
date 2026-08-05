## Title
Oracle `DataProvider::get()` strips the price timestamp, forcing consumers of `pallet-honzon-oracle` to use potentially stale aggregated prices with no way to validate freshness - ([File: substrate/frame/honzon/oracle/src/lib.rs])

## Summary
`pallet-honzon-oracle` (`substrate/frame/honzon/oracle`) is a live, standalone oracle pallet in this repository that stores operator-submitted values with timestamps (`TimestampedValue`) and aggregates them via `CombineData`. The pallet exposes two ways to read the aggregated price: the raw view `get_value()`/`get()` which returns the full `TimestampedValueOf<T, I>` (value + timestamp), and the `DataProvider` trait implementation, which is the interface intended for other pallets (e.g. lending/CDP/liquidation logic) to consume prices. That trait implementation unconditionally discards the timestamp and returns only the bare value, making it structurally impossible for any downstream consumer using the standard `DataProvider` interface to detect or reject a stale price - this is the same broken invariant as the external report's `_getAtomicPrices()` calling `oracle.getAssetPrice()` without a staleness check.

## Finding Description
The pallet aggregates timestamped submissions into `Values<T, I>`: [1](#0-0) 

The internal accessor `get()` returns the full timestamped value: [2](#0-1) 

But the `DataProvider` trait implementation - the canonical interface documented for other pallets to consume oracle prices - drops the timestamp entirely and returns only the raw value: [3](#0-2) 

There is no mechanism anywhere in this pallet enforcing a maximum data age before a value is served through `DataProvider::get()`: no `MaxStaleness`/`ExpiryPeriod` config item, no check against `T::Time::now()` at read-time, and stale entries in `RawValues`/`Values` are only ever cleared when an operator is removed via `change_members_sorted`, not on a time basis: [4](#0-3) 

The pallet's own documentation claims "Timestamped Data: All submitted data includes timestamps for freshness tracking," but the `DataProvider` trait - the one exposed for cross-pallet integration - never surfaces or checks that timestamp: [5](#0-4) 

If oracle operators stop submitting fresh data (e.g. due to liveness issues, network partition, or simply because the price hasn't moved so no one bothers updating), `Values<T,I>` will retain the last aggregated value indefinitely. Any consuming pallet built against `DataProvider::get()` (the documented, generic integration point) has no timestamp to inspect and therefore cannot itself implement a staleness guard - the information needed to do so has already been discarded by the oracle pallet before it reaches the consumer. This exactly mirrors the external report's root cause: a price-serving function that omits the staleness check that its own underlying data model supports, allowing critical financial logic built on top (asset valuation, collateral pricing, liquidation triggers, mint/burn calculations) to act on outdated prices and create value-extraction opportunities for whoever controls the timing of transactions against the stale value.

## Impact Explanation
Any runtime/pallet integrated against `pallet-honzon-oracle` via the standard `DataProvider` trait (the intended, generic extension point) inherits an unconditional stale-price acceptance path. Because the price is a chain-level state used for collateral valuation, minting, liquidation, or fee calculations in typical oracle-consumer designs, an attacker who can predict or induce a period of oracle staleness (e.g. operators temporarily failing to update during a market move) can transact against the frozen price to extract value from any pallet built on top of this interface - forged/mis-bound acceptance of price data without a freshness guard, directly matching the "runtime bugs that compromise intended behavior" and "theft or unbacked mint" impact classes in the accepted gate. Severity is Medium: it requires organic staleness (operator inactivity or lag) rather than a malicious oracle operator, and requires a downstream consumer pallet to exist and be exploited, but no privileged actor, admin abuse, or malicious peer is needed to trigger the underlying condition.

## Likelihood Explanation
Likelihood is Medium: oracle push-based aggregation naturally develops staleness gaps whenever off-chain operator infrastructure lags or when values don't change fast enough for operators to resubmit, and this pallet has no expiry/staleness config to reduce that window. Because `DataProvider::get()` is the sole generic integration surface (the raw `get_value()` still carries a timestamp but is not part of the trait contract other modules are expected to use for interoperability), any code written to the standard interface is architecturally prevented from doing its own staleness check, making exploitation only a matter of waiting for or inducing a stale period on an existing consumer.

## Recommendation
Add a configurable staleness bound (e.g. `type MaxStaleness: Get<MomentOf<T, I>>`) and enforce it in the `DataProvider::get()` implementation (and in `get_value()`/`get()`), returning `None`/rejecting the read when `T::Time::now().saturating_sub(timestamp) > MaxStaleness::get()`. Alternatively, change the `DataProvider` implementation to preserve and expose the timestamp so that all consumers are forced to make an explicit freshness decision, matching the fix pattern the external report recommends (replace the unchecked getter with a staleness-checked one).

## Proof of Concept
1. Configure a runtime with `pallet-honzon-oracle` and any downstream pallet (e.g. a hypothetical lending/CDP pallet) that reads prices via `<HonzonOracle as DataProvider<OracleKey, OracleValue>>::get(&key)`.
2. Oracle operators submit a price for `key` at block `N` via `feed_values`, populating `Values<T,I>` with `TimestampedValue { value: P, timestamp: t_N }`. [6](#0-5) 
3. Let a long interval pass with no operator resubmitting (network partition, off-chain infra failure, or simply no incentive to update). `Values<T,I>` still contains the stale `(P, t_N)` pair; nothing in the pallet purges or flags it as expired.
4. The downstream pallet calls `DataProvider::get(&key)`, receiving only `P` with no timestamp, and uses it for a critical calculation (mint, liquidation threshold, collateral ratio) as if it were current. [3](#0-2) 
5. An attacker who knows the real off-chain price has diverged from `P` executes a transaction against the downstream pallet's stale-price-dependent logic (e.g. mint against overvalued collateral, or avoid liquidation despite being undercollateralized), extracting value at the expense of the protocol/other users - with no code path available to reject the read because the timestamp was never propagated past `pallet-honzon-oracle`'s `DataProvider` boundary.

### Citations

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

**File:** substrate/frame/honzon/oracle/src/lib.rs (L395-398)
```rust
	/// Returns the aggregated and timestamped value for a given key.
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

**File:** substrate/frame/honzon/oracle/src/lib.rs (L432-442)
```rust
impl<T: Config<I>, I: 'static> ChangeMembers<T::AccountId> for Pallet<T, I> {
	fn change_members_sorted(
		_incoming: &[T::AccountId],
		outgoing: &[T::AccountId],
		_new: &[T::AccountId],
	) {
		// remove values
		for removed in outgoing {
			let _ = RawValues::<T, I>::clear_prefix(removed, u32::MAX, None);
		}
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
