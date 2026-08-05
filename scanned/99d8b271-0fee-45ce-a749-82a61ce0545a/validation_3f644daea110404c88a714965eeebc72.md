### Title
Stale oracle prices silently reused because `pallet-oracle`'s `Values`/`get()` API returns aggregated data with no staleness check at read time - ([File: substrate/frame/honzon/oracle/src/lib.rs])

### Summary
`pallet-oracle` (Acala's on-chain oracle, `substrate/frame/honzon/oracle`) only filters price freshness at the moment new data is *fed*, inside `DefaultCombineData::combine_data`. Once a `TimestampedValue` is aggregated into the `Values` storage item, any downstream consumer that calls `Pallet::get(key)` or the `DataProvider::get(key)` trait implementation receives the raw stored value with **no re-check against the current time**. This is the exact analog of the reported `PriceOracle.getTokenPrice()` bug: publish-time metadata (the `timestamp` field) exists but is never verified by the consumer before the price is used, so an old/frozen price is silently treated as fresh.

### Finding Description
- `feed_values` timestamps every submission with `T::Time::now()` [1](#0-0) .
- `DefaultCombineData::combine_data` filters *raw* values by `expires_in` **only when a new feed arrives**, i.e. `values.retain(|x| x.timestamp.saturating_add(expires_in) > now)` — this happens as part of `do_feed_values`, not as part of reading `Values` [2](#0-1) .
- If the filtered/aggregated result cannot be produced (not enough fresh values), `combine_data` returns `prev_value` — the last aggregated value, regardless of its age — and that is what gets written back into `Values` [3](#0-2) .
- The public read path `Pallet::get(key)` simply returns whatever is in `Values` storage with no time comparison at all: `pub fn get(key: &T::OracleKey) -> Option<TimestampedValueOf<T, I>> { Self::values(key) }` [4](#0-3) .
- The `DataProvider` trait implementation used by consumer pallets strips even the timestamp and returns only the bare value: `fn get(key: &T::OracleKey) -> Option<T::OracleValue> { Self::get(key).map(|timestamped_value| timestamped_value.value) }` [5](#0-4) .
- `DataProviderExtended::get_all_values` likewise exposes the timestamp, but nothing in the pallet enforces that a consumer actually checks it: `<Values<T, I>>::iter().map(|(k, v)| (k, Some(v)))` [6](#0-5) .

If oracle operators simply stop submitting for any reason (network partition, RPC failure, lost interest in an illiquid asset pair, deliberate withholding by a subset of operators, chain congestion, etc. — no malicious/privileged actor required), `Values[key]` keeps returning the last-known price indefinitely through `DataProvider::get()`. Any pallet built on top of `T::Source: DataProvider<...>` (this is the generic hook this pallet is explicitly designed to be consumed through, per its own doc: "This data can then be used by other pallets" [7](#0-6) ) will use that stale price for whatever financial calculation it performs (collateral valuation, liquidation thresholds, swap/collateral-ratio checks, fee computation) without any additional guard, because the pallet does not expose a "freshness-checked" get variant nor documents an obligation for callers to inspect the timestamp themselves. The pallet's own tests demonstrate this: `should_combined_data` advances the clock (`Timestamp::set_timestamp(23456)`) and shows `ModuleOracle::get(&key)` still returns the old, unfiltered aggregate unchanged [8](#0-7) .

### Impact Explanation
This matches the "runtime bugs that compromise intended behavior" and "public underpriced work" impact classes from the gate: any consumer pallet relying on `pallet-oracle`'s `Values`/`DataProvider::get()` for balance-sensitive decisions (e.g. collateralized debt valuation, liquidation, swap pricing, fee scaling) can be driven by a frozen/stale price with no on-chain safeguard, potentially enabling under/over-collateralization, incorrect liquidations, or mispriced settlement — all without needing a malicious validator, governance actor, or leaked key. The root cause is a missing invariant enforcement in the core oracle primitive itself, not in a downstream consumer that could reasonably be assumed to add its own check.

### Likelihood Explanation
High-likelihood precondition: oracle feed simply stalling (operators going offline, network split, lack of liquidity for a pair) is a benign, expected real-world condition — not an attacker action. Any runtime that wires a financial pallet directly to `pallet-oracle::Pallet` as its `DataProvider` inherits this gap automatically, since the trait contract gives no timestamp and the pallet API gives no staleness gate.

### Recommendation
Add a staleness check inside `pallet-oracle` itself rather than relying on every consumer to reimplement it correctly:
- Introduce a `MaxStaleness: Get<MomentOf<T, I>>` (or reuse `ExpiresIn`) config parameter and have `Pallet::get()` / `DataProvider::get()` return `None` (or an explicit `Stale` variant) whenever `now().saturating_sub(timestamp) > MaxStaleness::get()`, mirroring `getPriceNoOlderThan`.
- Ensure `DataProviderExtended::get_all_values()` also exposes staleness status explicitly so consumers can't accidentally trust old data.
- Document clearly that `Values` storage may lag; consumers must not treat presence of a value as freshness.

### Proof of Concept
1. Configure `pallet-oracle` with 3 operators, `MinimumCount = 3`, some `ExpiresIn` duration `E`.
2. All 3 operators call `feed_values` at `t0`; `combine_data` succeeds and `Values[key] = { value: P, timestamp: t0 }`.
3. Advance the chain clock far beyond `E` (e.g. `Timestamp::set_timestamp(t0 + 10*E)`) without any further `feed_values` calls (simulating stalled/offline operators — no attacker action needed).
4. Call `Oracle::get(&key)` or `<Oracle as DataProvider<_,_>>::get(&key)`: it still returns `Some(P)` (or `Some({value: P, timestamp: t0})`), even though `P` is `10*E` old and by the configured `ExpiresIn` policy should be considered expired. This reproduces the "return stale price as fresh" pattern from the reported issue, confirmed directly by the existing test `should_combined_data`, which asserts the unchanged value persists after the clock has moved forward [9](#0-8) .

### Citations

**File:** substrate/frame/honzon/oracle/src/lib.rs (L1-7)
```rust
// This file is part of Substrate.

// Copyright (C) 2020-2025 Acala Foundation.
// SPDX-License-Identifier: Apache-2.0

// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
```

**File:** substrate/frame/honzon/oracle/src/lib.rs (L396-398)
```rust
	pub fn get(key: &T::OracleKey) -> Option<TimestampedValueOf<T, I>> {
		Self::values(key)
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

**File:** substrate/frame/honzon/oracle/src/lib.rs (L449-453)
```rust
impl<T: Config<I>, I: 'static> DataProvider<T::OracleKey, T::OracleValue> for Pallet<T, I> {
	fn get(key: &T::OracleKey) -> Option<T::OracleValue> {
		Self::get(key).map(|timestamped_value| timestamped_value.value)
	}
}
```

**File:** substrate/frame/honzon/oracle/src/lib.rs (L457-459)
```rust
	fn get_all_values() -> impl Iterator<Item = (T::OracleKey, Option<TimestampedValueOf<T, I>>)> {
		<Values<T, I>>::iter().map(|(k, v)| (k, Some(v)))
	}
```

**File:** substrate/frame/honzon/oracle/src/default_combine_data.rs (L38-59)
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

		let mid_index = count / 2;
		// Won't panic as `values` ensured not empty.
		let (_, value, _) =
			values.select_nth_unstable_by(mid_index as usize, |a, b| a.value.cmp(&b.value));
		Some(value.clone())
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
