## Title
`pallet-oracle`'s `DefaultCombineData` can silently keep serving an arbitrarily stale aggregated price to consumers with no enforced freshness check - ([File: substrate/frame/honzon/oracle/src/default_combine_data.rs])

## Summary
The newly introduced `pallet-oracle` (`substrate/frame/honzon/oracle`, intended to back the Polkadot Stablecoin on AssetHub per `prdoc/stable2512/pr_9815.prdoc`) aggregates operator-fed prices into a `TimestampedValue` stored in `Values`. When fresh submissions are insufficient, `DefaultCombineData::combine_data` falls back to returning the previous aggregated value unchanged, and that stale `TimestampedValue` continues to be exposed via `Pallet::get`/`DataProvider::get` with no built-in staleness enforcement — mirroring the exact defect in the external report where `Oracle.viewPrice`/`getPrice` accepted a positive-but-stale Chainlink answer because only existence (`price > 0`), not freshness, was validated.

## Finding Description
`DefaultCombineData::combine_data` filters raw operator submissions by timestamp (`values.retain(|x| x.timestamp.saturating_add(expires_in) > now)`), but if too few *fresh* values remain (`count < minimum_count`), it returns `prev_value` — the previously stored aggregate — verbatim: [1](#0-0) 

That `prev_value` retains its **original** timestamp from whenever it was last legitimately aggregated. It is written into `Values` storage as-is: [2](#0-1) 

Consumers read this value through `Pallet::get`, `DataProvider::get`, or `DataProviderExtended::get_all_values`: [3](#0-2) [4](#0-3) 

Crucially, `DataProvider::get` (the interface most consumer pallets are documented to use, per the pallet README) **discards the timestamp entirely** and returns only the bare `value`: [5](#0-4) 

This is functionally identical to the Chainlink `Oracle.sol` bug: the report's `viewPrice`/`getPrice` fetched `latestAnswer()` and checked only `price > 0` (existence), never freshness (`answeredInRound`/`updatedAt`). Here, `DataProvider::get` returns the last combined `value` with **no timestamp attached at all**, so even a diligent consumer pallet using the `DataProvider` trait (rather than `DataProviderExtended`) has no way to detect that the price is stale — the staleness signal is architecturally stripped before it reaches the consumer.

If oracle operators stop feeding data for any liveness reason (network partition, censorship of the feed inputs, benign operator downtime — not a malicious-operator or governance-abuse scenario), the aggregated `Values` entry simply freezes at its last value indefinitely. Nothing in the pallet halts, pauses, or flags downstream consumption; `get` keeps returning `Some(stale_value)` forever.

## Impact Explanation
Per the PRDoc, this pallet is the price-feed backbone for a stablecoin/CDP-style system on AssetHub (`prdoc/stable2512/pr_9815.prdoc`) — i.e., exactly the collateral/credit-limit computation class flagged as in-scope ("runtime bugs that compromise intended behavior," "theft or unbacked mint," "unauthorized... liquidation"). A consumer pallet built on this oracle for collateral valuation, borrow limits, or liquidation thresholds that uses `DataProvider::get` (the primary documented interface) inherits a price with zero freshness metadata. An attacker (an ordinary, unprivileged user interacting with the downstream lending/CDP pallet, exactly as "Bob" did in the source report) can repeatedly probe liquidation/borrow functions; once the feed goes stale (through no fault of the attacker — a live oracle-operator liveness gap, not an admin/governance action), the frozen price can misrepresent a healthy position as undercollateralized (or vice versa), enabling unjustified liquidation and unbacked value transfer, or conversely letting borrowers over-extract against a stale high price.

## Likelihood Explanation
Requires only (a) a normal, transient gap in oracle-operator submissions — which is a routine liveness condition, not a malicious actor, governance failure, or leaked key — and (b) any downstream consumer using the pallet's primary `DataProvider` interface as documented. No privileged action, no relayer/validator collusion, and no front-running is needed; the attacker merely calls a public dispatchable at the right moment, identical in structure to the original report's PoC.

## Recommendation
- Have `DataProvider::get` return `(value, timestamp)` (or otherwise preserve the timestamp) instead of discarding it, so consumers can enforce freshness themselves.
- Add an explicit `is_stale`/expiry check inside `Pallet::get`/`combined` so that once `prev_value.timestamp + ExpiresIn <= now`, the pallet returns `None` rather than silently continuing to serve the old value.
- Document/require that any consumer pallet built on `pallet-oracle` for collateral/liquidation logic must check `TimestampedValue::timestamp` against current time before trusting the price, analogous to Chainlink's `answeredInRound`/`updatedAt` checks recommended in the source report.

## Proof of Concept
1. Configure `pallet-oracle` with `DefaultCombineData<T, MinimumCount, ExpiresIn>` (as in `substrate/bin/node/runtime/src/lib.rs:3154`, `ConstU64<3600>` expiry) feeding a downstream collateral pallet via `DataProvider::get`.
2. Operators feed a price at `t0`; `Values` stores `TimestampedValue { value: P, timestamp: t0 }`.
3. Oracle operators stop submitting (liveness gap, no operator is malicious or colluding).
4. At `t0 + 3600 + 1`, `combine_data` is invoked on any subsequent submission attempt or the value is simply never refreshed — `Values` still returns `TimestampedValue { value: P, timestamp: t0 }` because no expiry check exists on read, only on the *input* filtering step, and only when new submissions occur.
5. An unprivileged user interacting with a downstream lending/CDP pallet calls `DataProvider::get(key)`, receiving bare `P` with no timestamp — the downstream pallet computes credit/liquidation limits using this arbitrarily stale price, exactly as Bob exploited the stale Chainlink `price` in the source report to trigger an unwarranted `liquidate`.

### Citations

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

**File:** substrate/frame/honzon/oracle/src/lib.rs (L396-403)
```rust
	pub fn get(key: &T::OracleKey) -> Option<TimestampedValueOf<T, I>> {
		Self::values(key)
	}

	fn combined(key: &T::OracleKey) -> Option<TimestampedValueOf<T, I>> {
		let values = Self::read_raw_values(key);
		T::CombineData::combine_data(key, values, Self::values(key))
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
