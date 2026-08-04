### Title
`pallet-oracle`'s aggregated price is served to consumers with no on-read staleness check, allowing stale price data to be treated as current - (File: `substrate/frame/honzon/oracle/src/lib.rs`)

### Summary
`pallet-oracle` timestamps each operator submission and its `DefaultCombineData` aggregation step filters out expired inputs *only at the moment new data is fed*. Once an aggregated value is written to the `Values` storage map, every downstream reader (`Pallet::get`, the `DataProvider`/`DataProviderExtended` implementations, and the `OracleApi` runtime API) returns that stored value verbatim, with no re-check of `TimestampedValue::timestamp` against the current time. This mirrors the Tigris `latestAnswer` bug: freshness is validated at write/aggregation time but not at consumption time, so a value that was fresh when last aggregated can be served indefinitely as if current.

### Finding Description
`DefaultCombineData::combine_data` in `substrate/frame/honzon/oracle/src/default_combine_data.rs:38-59` filters raw per-operator submissions using `ExpiresIn` before computing the median: [1](#0-0) 

This filtering only runs from within `do_feed_values` -> `combined` -> `T::CombineData::combine_data`, i.e. **only when an oracle operator submits a new feed**: [2](#0-1) 

The resulting aggregate is persisted in the `Values<T, I>` storage map and is never re-validated for freshness on read. `Pallet::get`, the `DataProvider` implementation, `DataProviderExtended::get_all_values`, and the public `OracleApi` runtime API all return this stored `TimestampedValue` (including its `timestamp` field for `get`, but the plain `DataProvider::get` strips the timestamp entirely) without comparing it to the current time: [3](#0-2) [4](#0-3) [5](#0-4) 

If oracle members stop feeding a given key (e.g. because the underlying value hasn't materially changed, they go offline, or `feed_values` transactions simply aren't included for a period), `Values<T, I>` retains the last computed value forever — there is no expiry sweep, no "last updated" guard, and no error path for "stale" analogous to Chainlink's `latestRoundData` freshness checks. Any consuming pallet configured with `type CombineData = pallet_oracle::DefaultCombineData<..., ExpiresIn>` (as `substrate/bin/node/runtime/src/lib.rs:3154` does, with `ConstU64<3600>`) or any consumer using `DataProvider::get` receives this potentially arbitrarily-old value as if it were fresh, because `DataProvider::get` (line 450-452) drops the timestamp altogether, denying the consumer even the *option* to self-check freshness.

### Impact Explanation
Any runtime pallet that treats `pallet_oracle::Pallet::get` / `DataProvider::get` as a live price/data feed (e.g., a PSM/stablecoin peg pallet, a lending/liquidation pallet, or any economic logic gated on an oracle value) inherits this staleness gap. An attacker does not need to compromise an oracle operator: they simply need market conditions to move after the last honest feed while operators are naturally idle or rate-limited by `AlreadyFeeded`/`MaxHasDispatchedSize`, and then interact with the consuming pallet while it still reads the outdated `Values` entry. This can cause economically-incorrect settlement (e.g. minting/redeeming at a stale peg rate, triggering/avoiding liquidations incorrectly), i.e. false state acceptance and mispriced value transfer, matching the impact class of forged/mis-validated price acceptance in the gate criteria.

### Likelihood Explanation
This is not a hypothetical governance/admin misconfiguration — it is the pallet's designed behavior: freshness filtering exists only inside `combine_data`, called solely on write. Any unprivileged user of a consumer pallet can trigger the vulnerable read path simply by calling normal extrinsics during a lull in oracle feeds (which will always eventually occur, since `MaxHasDispatchedSize`/one-feed-per-block-per-operator caps submission frequency and there's no automatic invalidation). No malicious relayer, validator, or governance actor is required — only ordinary usage timing.

### Recommendation
Add an explicit freshness check on read, mirroring the mitigation recommended for the Chainlink bug:
- Store (or make available) the `ExpiresIn` bound to `Pallet::get`/`DataProvider::get`, and return `None` (or an error) when `now > timestamp + expires_in`, instead of silently returning the stale value.
- Alternatively, extend `DataProvider`/`DataProviderExtended` to always expose the `TimestampedValue` (not just the raw value) so that every consumer is forced to perform its own staleness check before using the data, and update `OracleApi::get_value` similarly.
- Consider actively clearing/expiring `Values` entries once they age out, rather than relying purely on downstream reads.

### Proof of Concept
1. Oracle members feed a value for key `K` at time `T0`; `DefaultCombineData` aggregates it (values fresh, count ≥ `MinimumCount`), and `Values::<T>::insert(K, TimestampedValue { value: V0, timestamp: T0 })` runs (`substrate/frame/honzon/oracle/src/lib.rs:415-424`).
2. No further `feed_values` calls occur for key `K` for longer than `ExpiresIn` (e.g., > 3600s per `substrate/bin/node/runtime/src/lib.rs:3154`) — this is normal, permissionless idle behavior, not an attack requiring any privileged party.
3. A consuming pallet (or the `OracleApi::get_value` RPC / `pallet_oracle::Pallet::get`) is queried/used at time `T1 = T0 + 4000s`. `Pallet::get(&K)` (`substrate/frame/honzon/oracle/src/lib.rs:396-398`) returns `Some(TimestampedValue { value: V0, timestamp: T0 })` unchanged — no comparison of `T0` vs `T1` is performed anywhere in the read path.
4. Any consumer using `DataProvider::get(&K)` (`substrate/frame/honzon/oracle/src/lib.rs:449-452`) receives raw `V0` with the timestamp discarded entirely, so it cannot even detect staleness itself. If the real-world value has since diverged by more than an acceptable tolerance, the consuming logic (e.g., a peg/PSM conversion) executes against a stale, incorrect price — exactly analogous to the Tigris `latestAnswer` stale-price acceptance.

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

**File:** substrate/frame/honzon/oracle/src/lib.rs (L395-403)
```rust
	/// Returns the aggregated and timestamped value for a given key.
	pub fn get(key: &T::OracleKey) -> Option<TimestampedValueOf<T, I>> {
		Self::values(key)
	}

	fn combined(key: &T::OracleKey) -> Option<TimestampedValueOf<T, I>> {
		let values = Self::read_raw_values(key);
		T::CombineData::combine_data(key, values, Self::values(key))
	}
```

**File:** substrate/frame/honzon/oracle/src/lib.rs (L415-424)
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

**File:** substrate/bin/node/runtime/src/lib.rs (L3597-3608)
```rust
	impl polkadot_sdk::pallet_oracle_runtime_api::OracleApi<Block, u32, u32, u128> for Runtime {
		fn get_value(_provider_id: u32, key: u32) -> Option<u128> {
			// ProviderId is unused as we only have 1 provider
			pallet_oracle::Pallet::<Runtime>::get(&key).map(|v| v.value)
		}

		fn get_all_values(_provider_id: u32) -> Vec<(u32, Option<u128>)> {
			use pallet_oracle::DataProviderExtended;
			pallet_oracle::Pallet::<Runtime>::get_all_values()
				.map(|(k, v)| (k, v.map(|tv| tv.value)))
				.collect()
		}
```
