## Finding

### Title
Oracle price aggregation silently returns unbounded-age stale data instead of failing when fresh quorum is unavailable - (File: `substrate/frame/honzon/oracle/src/default_combine_data.rs`)

### Summary
The Band price-feed bug is a "no enough historical data → silently accept stale/short-window data instead of reverting" pattern. The same class of defect exists in `pallet-oracle`'s default aggregation logic (`substrate/frame/honzon/oracle`), which is wired into the reference node runtime (`substrate/bin/node/runtime/src/lib.rs`). When a quorum of fresh values isn't available, the pallet returns the *previous* aggregated value with no bound on how old that previous value is, instead of returning `None`/reverting the read for consumers.

### Finding Description
`DefaultCombineData::combine_data` filters out expired raw submissions using `ExpiresIn`, but if fewer than `MinimumCount` fresh values remain, it falls back to `prev_value` unconditionally: [1](#0-0) 

`prev_value` is whatever was last stored in `Values<T, I>` — it can be arbitrarily old, because nothing in `combine_data`, in `Pallet::combined`, or in `Pallet::get` checks the age of `prev_value` itself before returning it to callers: [2](#0-1) 

This mirrors the Band feed's edge case #2 exactly: instead of reverting/signaling "insufficient fresh data," the system substitutes an old observation (`prev_value`/`lastestObservation.price`) as if it were current, with the staleness only implicitly bounded by whatever downstream consumer *chooses* to check (which `pallet-oracle`'s public interface — `get`, `get_value`, `all_values` — does not enforce or even expose in a way that forces a check).

The `Values` storage that becomes `prev_value` is never cleared or expired on its own; it only changes when `combine_data` succeeds in producing a new quorum-backed value. So if oracle operators stop feeding fresh data (e.g., go offline, get slashed/removed from `Members`, or an asset simply becomes illiquid/abandoned), `Values` keeps returning the last known price indefinitely, with its timestamp field intact but with no protocol-level enforcement that consumers check it — exactly the second Band edge case ("if the last update... then the returned price will equal the outdated... price").

### Impact Explanation
Any pallet built on top of `pallet-oracle`'s `DataProvider`/`get` interface (e.g., a CDP/lending, DEX price oracle, or collateral-valuation pallet integrated into a runtime) that does not itself re-check `TimestampedValue.timestamp` freshness will silently operate on stale prices. This can lead to under-collateralized loans, incorrect liquidations, or mispriced swaps — a runtime-level "compromise of intended behavior," matching the Impact Gate's "runtime bugs that compromise intended behavior" and "theft or unbacked mint/unlock" categories if the stale price allows extracting more value than backed.

### Likelihood Explanation
This requires no privileged actor: it is triggered purely by oracle operator inactivity or a newly-added key that never reaches `MinimumCount` fresh submissions, both of which are realistic operational conditions (matching the report's "new markets before enough historical data" scenario) rather than any malicious peer/validator/admin action, satisfying the Pivots' requirement for a non-privileged root cause.

### Recommendation
`DefaultCombineData::combine_data` (or `Pallet::get`) should not return `prev_value` unconditionally. Either bound the fallback by re-checking `prev_value.timestamp.saturating_add(expires_in) > now` before returning it, or surface a distinct "stale" state (e.g., return `None`) so consumers can decide, rather than silently returning a possibly infinitely-old value as if it were current.

### Proof of Concept
1. Configure `pallet-oracle` with `MinimumCount = 3`, `ExpiresIn = T`.
2. Three operators feed a price for key `K` at time `t0`; `Values[K] = (price=P, timestamp=t0)`.
3. All three operators stop feeding new values for `K` (e.g., go offline or the token becomes inactive).
4. At time `t0 + 100*T` (far beyond expiry), a consumer pallet calls `Oracle::get(&K)`. `read_raw_values` returns entries that are now all expired/removed via `retain`, `count == 0 < MinimumCount`, so `combine_data` returns `prev_value = (P, t0)` — see [3](#0-2) .
5. `Oracle::get(&K)` still returns `Some((P, t0))` with no error, and nothing in the pallet forces the caller to check `t0` against current time before using `P` as a live price.

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
