### Title
`pallet-oracle`'s `DataProvider::get` strips the freshness timestamp, letting consumers act on stale price data with no staleness guard - (File: `substrate/frame/honzon/oracle/src/lib.rs`)

### Summary
`pallet-oracle` timestamps every aggregated value it stores, but the trait implementation that other pallets are expected to use to consume oracle data (`DataProvider::get`) discards that timestamp entirely, and nothing in the pallet enforces a maximum data age. This is the direct structural analog of the Chainlink report: a price/data feed is read without any check that it is recent, so a consumer relying on `DataProvider::get` can silently act on an arbitrarily old value.

### Finding Description
`pallet-oracle` aggregates operator-submitted values into `Values<T, I>`, storing them as `TimestampedValue { value, timestamp }` so that "other pallets can react to oracle updates" and can, per the pallet's own documentation, know "how fresh" the data is: [1](#0-0) 

The pallet exposes two ways to read a value:
- `Pallet::get(key) -> Option<TimestampedValueOf<T, I>>`, which does include the timestamp
- `DataProvider::get(key) -> Option<T::OracleValue>`, the standard trait interface most consumer pallets are meant to use, which maps away the timestamp and returns only the bare value: [2](#0-1) 

Because `DataProvider` is the generic, documented integration point ("The pallet implements the `DataProvider` and `DataProviderExtended` traits, allowing other pallets to easily consume the oracle data"): [3](#0-2) 

any consumer that goes through this trait has no timestamp to compare against `T::Time::now()`, and the oracle pallet itself performs no age check anywhere in `do_feed_values`/`combined`/`get`: [4](#0-3) 

There is also no configuration item such as a max-age bound, no `Error::StaleData`, and no expiry logic in the pallet: `Values<T,I>` simply holds whatever was last aggregated, indefinitely, until a fresh `feed_values` overwrites it. If oracle operators stop feeding data (e.g. go offline, are slow, or are removed via `change_members_sorted` for a key that is never re-fed), `Values` keeps returning the old price forever through `DataProvider::get` with no signal to the caller that it is stale — exactly the `latestRoundData`-without-`updatedAt` pattern from the external report.

### Impact Explanation
`pallet-oracle` is explicitly built as the on-chain price source for a runtime deploying a stablecoin ("Polkadot Stablecoin on AssetHub", per `pr_9815.prdoc`): [5](#0-4) 

Any downstream pallet (e.g. collateral valuation, liquidation triggers, PSM-style peg pallets, or a future consumer of `pallet-oracle` in the stablecoin design) that integrates via the standard `DataProvider::get` interface — as the pallet's own README instructs — will act on a price with no freshness bound. This can cause mispriced mint/redeem/liquidation decisions on genuinely stale data (e.g. minting against an inflated collateral price, or skipping a liquidation that should have triggered), i.e. wrong-amount settlement and fund-safety impact in the accounting/asset-conservation sense called out in the pivots.

### Likelihood Explanation
This is a design-level gap rather than a rare edge case: it requires no malicious oracle operator, governance action, or privileged actor — merely oracle operators pausing or lagging (network partition, liveness issue, removed membership) while a consumer keeps calling the unguarded `DataProvider::get`. Because `DataProvider` is the intended generic integration surface, any future or existing consumer pallet that does not independently re-implement a staleness check (by instead using `Pallet::get`/`get_value` and comparing timestamps itself) inherits the vulnerability by construction.

### Recommendation
Add an explicit staleness guard in `pallet-oracle`:
- Introduce a `MaxFeedAge: Get<Moment>` (or per-key) config bound.
- Have `DataProvider::get` (and/or add a new `DataProviderExtended`/dedicated fresh-price accessor) check `T::Time::now().saturating_sub(timestamped.timestamp) <= MaxFeedAge::get()` before returning `Some(value)`, returning `None` otherwise.
- Alternatively, deprecate the timestamp-stripping `DataProvider::get` impl in favor of forcing all consumers to use the timestamped `get`/`get_value` and require them to perform the staleness check themselves, with clear documentation/enforcement (e.g. a shared helper trait) so the check isn't silently skippable.

### Proof of Concept
1. Configure a runtime with `pallet-oracle` and a downstream consumer pallet (e.g. a stablecoin/PSM-style pallet) that reads the price solely via `<pallet_oracle::Pallet<T> as DataProvider<Key, Value>>::get(&key)`.
2. Oracle operators feed a price at time `t0` via `feed_values`, populating `Values<T,I>` with `TimestampedValue { value: P, timestamp: t0 }`.
3. Oracle operators stop submitting further updates (simulating downtime/liveness loss) — no code path expires or invalidates `Values<T,I>`.
4. Time advances arbitrarily (`t1 >> t0`), yet `DataProvider::get(&key)` at `t1`, per [2](#0-1) , still returns `Some(P)` with no timestamp exposed and no error.
5. The consumer pallet uses `P` as the current price for mint/redeem/liquidation math, even though it is arbitrarily stale, demonstrating the missing-staleness-check analog of the Chainlink `latestRoundData`/`updatedAt` issue.

### Citations

**File:** substrate/frame/honzon/oracle/src/lib.rs (L177-181)
```rust
		/// The time provider for timestamping oracle data.
		///
		/// This type provides the current timestamp used to mark when oracle data was submitted.
		/// Timestamps are crucial for determining data freshness and preventing stale data usage.
		type Time: Time;
```

**File:** substrate/frame/honzon/oracle/src/lib.rs (L395-429)
```rust
	/// Returns the aggregated and timestamped value for a given key.
	pub fn get(key: &T::OracleKey) -> Option<TimestampedValueOf<T, I>> {
		Self::values(key)
	}

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

**File:** substrate/frame/honzon/oracle/README.md (L37-40)
```markdown
### Data Providers

The pallet implements the `DataProvider` and `DataProviderExtended` traits, allowing other pallets to easily
consume the oracle data.
```

**File:** prdoc/stable2512/pr_9815.prdoc (L8-9)
```text
    description: |
      This PR is part of #9765 - Polkadot Stablecoin on AssetHub. It introduces `pallet-oracle`, a new FRAME pallet that provides a decentralized and trustworthy way to bring external, off-chain data onto the blockchain. The pallet allows a configurable set of oracle operators to feed data, such as prices, into the system, which can then be consumed by other pallets.
```
