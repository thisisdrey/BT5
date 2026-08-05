### Title
`DefaultCombineData` silently retains an unbounded-age stale aggregated price when live feeds drop below quorum, letting a short feed manipulation propagate indefinitely into every downstream consumer of `pallet_oracle::Values` - (File: `substrate/frame/honzon/oracle/src/default_combine_data.rs`)

### Summary
The report's core broken invariant is: a liquidation/valuation decision is made against a deliberately-lagged price value rather than the live price, and a brief attacker-controlled feed manipulation gets a multi-day window to be exploited, while the *same* stale value is also reused to size the payout, compounding the damage. The local analog is `DefaultCombineData::combine_data` in the shared `pallet-oracle` used by this runtime: when fresh submissions drop below `MinimumCount`, the pallet does not clear or expire the aggregated value in `Values<T, I>` — it returns `prev_value` unconditionally, with no bound on how stale that value can become. Any pallet consuming `pallet_oracle::get()`/`DataProvider::get()` (e.g. CDP/lending/liquidation logic built on this oracle, or the PSM/asset-rate pallets in this same runtime that could be wired to it) will keep trusting an arbitrarily old, and thus manipulable, price forever, exactly mirroring the "2-day-low reused for both liquidation and reward sizing" pattern from the report.

### Finding Description
`DefaultCombineData::combine_data`, in `substrate/frame/honzon/oracle/src/default_combine_data.rs:38-59`, first filters submitted `values` for freshness against `ExpiresIn`:
```rust
values.retain(|x| x.timestamp.saturating_add(expires_in) > now);
let count = values.len() as u32;
let minimum_count = MinimumCount::get();
if count < minimum_count || count == 0 {
    return prev_value;
}
```
When the number of *fresh* raw submissions falls below `MinimumCount` (in the runtime wiring, `ConstU32<5>` with `ConstU64<3600>` expiry, `substrate/bin/node/runtime/src/lib.rs:3152-3165`), the function returns `prev_value` — the previously aggregated value stored in `Values<T, I>` — with **no check on how old `prev_value.timestamp` itself is**. This value is then written back unchanged in `do_feed_values` (`substrate/frame/honzon/oracle/src/lib.rs:415-429`):
```rust
if let Some(combined) = Self::combined(key) {
    <Values<T, I>>::insert(key, combined);
}
```
So `Values` for a key can be re-persisted indefinitely with the *original* timestamp from whenever quorum was last met, even if that was days or weeks ago. There is no on-chain enforcement that consumers reject data older than `ExpiresIn`; the pallet's own `get()` (`substrate/frame/honzon/oracle/src/lib.rs:396-398`) returns the `TimestampedValue` as-is, and it is entirely up to each downstream consumer to independently re-check freshness — a check the oracle pallet itself does not centrally guarantee once quorum is lost.

This exactly parallels the Inverse Finance bug: the report's root cause is trusting an intentionally-lagged (there: 2-day-low) value for a security decision instead of the live value, which gives an attacker who can move the feed for a short window a multi-day exploitation surface, and the same corrupted value is reused for a second purpose (liquidation payout sizing), doubling the damage. Here, the corrupted value is the *entire aggregated oracle output* for a key: once fresh submissions drop below quorum (a state trivially reachable — a single oracle operator going offline, or an attacker who is one of few operators withholding/delaying a feed), the last quorum-derived price is frozen and kept alive by every subsequent `feed_values` call from any other operator, with unbounded staleness. If price moved substantially since the freeze, any downstream module treating `pallet_oracle::get()` as "current price" (mint/redeem sizing, collateral valuation, liquidation triggers) will act on stale/manipulable data indefinitely, not just for a bounded window.

### Impact Explanation
Any runtime pallet in this repo that consumes `pallet_oracle`'s `DataProvider`/`DataProviderExtended` trait for economically sensitive decisions (asset valuation, collateral pricing, liquidation thresholds) inherits this staleness gap. Because `Values` is never expired or cleared on quorum loss, and is instead re-affirmed on every `feed_values` call, an attacker who can suppress fresh submissions from enough operators (or who controls even one operator combined with natural operator downtime) can freeze the on-chain price at a stale/favorable level with no time bound, then exploit any downstream pallet that trusts `get()` without independently re-validating the embedded timestamp. This is a false-state-acceptance class issue: the oracle pallet's own storage advances past the point where its data should be considered valid, without atomic invalidation.

### Likelihood Explanation
Reaching the below-quorum state requires no privileged access — it only requires that fewer than `MinimumCount` (5 in the runtime config) *distinct, currently-live* operators submit within the last `ExpiresIn` window (3600s here), a condition reachable by ordinary operator churn/downtime and does not require a malicious validator, collator, or admin. The bug is latent in the shared library logic (`DefaultCombineData`) rather than in a specific downstream consumer, so likelihood of the underlying condition being hit is moderate-to-high in any deployment with a small operator set, while the actual severity depends on which downstream pallet trusts the value without its own freshness check.

### Recommendation
`DefaultCombineData::combine_data` should not unconditionally return `prev_value` when quorum is lost — either return `None` (forcing the stored `Values` entry to age out / be treated as unavailable by consumers) or attach the current staleness explicitly so consumers can enforce their own maximum-age policy independent of `ExpiresIn`'s original semantics. At minimum, `pallet_oracle::get()`/`DataProvider::get()` should expose enough information (or a companion `is_stale` check against `T::Time::now()`) so that every consumer is forced to reason about freshness rather than trusting `Values` as always-current.

### Proof of Concept
1. Configure `MinimumCount = 5`, `ExpiresIn = 3600` as in `substrate/bin/node/runtime/src/lib.rs:3152-3165`.
2. Five operators feed a price at `t=0`; quorum met, `Values[key] = {value: P, timestamp: 0}`.
3. At `t=3601+`, only 2 operators (below `MinimumCount`) continue feeding (the rest go offline or are throttled by an attacker who is one of the operators controlling submission timing).
4. `combine_data` filters out the two live-but-fresh submissions' irrelevant count check — count(2) < minimum_count(5) → returns `prev_value = {value: P, timestamp: 0}` (`substrate/frame/honzon/oracle/src/default_combine_data.rs:50-51`).
5. `do_feed_values` re-inserts this into `Values[key]` (`substrate/frame/honzon/oracle/src/lib.rs:422-423`), refreshing nothing about its trustworthiness — the timestamp field remains `0`, but the entry persists in storage indefinitely across every subsequent feed as long as quorum is never re-reached.
6. Any pallet calling `Oracle::get(&key)` continues to receive `P` as "the" oracle value with no forced expiry, arbitrarily far past `t=3600`, at which point `P` may be arbitrarily divorced from the real market price — mirroring the "2-day low used indefinitely" attack surface from the source report, but here unbounded in time rather than bounded to 2 days. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

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

**File:** substrate/frame/honzon/oracle/src/lib.rs (L396-398)
```rust
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
