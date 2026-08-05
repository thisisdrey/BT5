### Title
Oracle price staleness is enforced only at write-time, allowing consumption of unbounded-age prices via `DataProvider::get` - ([File: substrate/frame/honzon/oracle/src/lib.rs])

### Summary
`pallet-oracle` (newly introduced for the "Polkadot Stablecoin on AssetHub" effort, see `prdoc/stable2512/pr_9815.prdoc`) computes a median-aggregated, timestamped price only when a `feed_values` extrinsic executes for a given key. If oracle operators stop submitting for that key (analogous to an L2 sequencer going down and no fresh price submissions being possible), the previously aggregated value simply remains in the `Values` storage map forever — there is no on-chain mechanism that re-validates or expires it independent of a new feed. Worse, the primary `DataProvider::get` interface strips the timestamp entirely, so any downstream consumer (e.g. a CDP/liquidation engine built on top, which this pallet is explicitly designed to feed) that uses the plain `DataProvider` trait has no way at all to detect that the price it just read may be arbitrarily old. This mirrors exactly the ChainLink sequencer-uptime bug class: a critical price value is consumed for liquidation-relevant decisions without any freshness/liveness check at the point of use.

### Finding Description
Staleness filtering exists, but only inside the aggregation function, and only runs as a side effect of a *new* submission: [1](#0-0) 

`combine_data` discards raw values older than `ExpiresIn` before computing the median, but if the retained set drops below `MinimumCount` it returns `prev_value` — the old aggregate — unchanged.

Crucially, this function is only invoked from `do_feed_values`, i.e., only when *someone submits new data*: [2](#0-1) 

If feeders stop submitting for a key entirely (operator downtime, membership churn, an oracle-side outage — the direct analog of "the sequencer is down so no new price submissions can land"), `Values::<T,I>` is **never touched again**. The last computed `TimestampedValue` sits in storage indefinitely.

Consumers read this stale value through: [3](#0-2) 

The `DataProvider::get` implementation (line 449-452) unwraps `TimestampedValueOf` and returns only `.value`, discarding the `timestamp` field entirely. Any consumer coded against the basic `DataProvider<Key, Value>` trait — which the pallet's own docs advertise as the primary integration point (`substrate/frame/honzon/oracle/README.md:37-41`, "Data Providers... allowing other pallets to easily consume the oracle data") — cannot recover the age of the price it just fetched, even in principle. Only `DataProviderExtended::get_all_values` exposes the timestamp, and using it correctly is left entirely to each downstream integrator; the pallet provides no shared, enforced check.

This is the direct on-chain analog of the reported bug: ChainLink price data goes stale when the Arbitrum sequencer halts and L2-submitted price updates cannot land; a protocol consuming that price without checking a sequencer/staleness oracle silently uses the outdated value for liquidation math. Here, oracle-operator submissions halting for any reason (node outage, membership set shrinking below `MinimumCount`, deliberate censorship, chain congestion) produces the same effect — a frozen aggregate price — and the pallet's primary consumption interface offers no way to guard against it.

### Impact Explanation
Since this pallet is being built specifically to back a Polkadot stablecoin (per `pr_9815.prdoc`), a stale price silently returned by `DataProvider::get` would feed directly into collateralization-ratio and liquidation logic in whatever CDP/vault pallet consumes it. A frozen stale price (either too high or too low relative to real market price) can:
- Prevent triggering of legitimate liquidations while collateral value has actually collapsed, causing bad debt / unbacked stablecoin exposure.
- Trigger unwarranted liquidations of solvent positions, causing unjust loss of user collateral.
Both outcomes match the "theft or unbacked mint", "duplicate settlement or payout", and "runtime bugs that compromise intended behavior" impact categories, without requiring any privileged, malicious, or off-chain actor — it is a pure protocol design gap in a first-party FRAME pallet shipped in this repository.

### Likelihood Explanation
No attacker action is required at all: this is a passive availability/liveness condition (feeders going idle, membership set shrinking, or normal operational gaps causing `count < MinimumCount`) which is entirely foreseeable and already partially anticipated by the `ExpiresIn`/`MinimumCount` logic in `combine_data` — but that logic is only reachable on write, not on read. Any runtime wiring this pallet's `DataProvider::get` directly into settlement-critical code (exactly the stated purpose of the pallet) inherits this gap by construction, since the trait itself provides no staleness signal.

### Recommendation
- Change `DataProvider::get` (or add a new consumption path) to re-apply the `ExpiresIn`/`MinimumCount` staleness check against `T::Time::now()` at read time, not only at aggregation/write time, returning `None` if the stored `Values` entry has expired.
- Alternatively, force `Values` staleness enforcement via an `on_initialize`/`on_idle` hook that expires (removes) old aggregate entries so `DataProvider::get` cannot return them.
- At minimum, document and strongly discourage consuming price data through the untimestamped `DataProvider` trait for any liquidation/settlement-critical logic, and provide a safe wrapper (e.g., `get_no_op`-style function) that internally enforces `ExpiresIn` before returning a value, mirroring the ChainLink-recommended sequencer/staleness check pattern.

### Proof of Concept
1. Configure a runtime with `pallet-oracle`, `MinimumCount = 3`, `ExpiresIn = N` blocks, and 3 oracle operators.
2. All three operators feed a price for key `K` at block `T`; `combine_data` computes and stores `Values[K] = TimestampedValue { value: P, timestamp: T }`.
3. Two of the three operators go offline (analogous to sequencer down / relayer outage) and stop submitting entirely; the third also stops (or the remaining feeders' raw entries individually expire in `RawValues`, but since no `feed_values` call ever occurs again, `combine_data` is never re-invoked).
4. At block `T + 10*N`, a downstream pallet calls `OraclePallet::get(&K)` via the `DataProvider` trait and receives `P` — the price from block `T` — with no indication that it is stale, because the timestamp was discarded before being handed to the caller.
5. The downstream pallet uses `P` to evaluate collateral value and make a liquidation/mint decision based on a price that may be arbitrarily disconnected from current market conditions. [4](#0-3)

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

**File:** substrate/frame/honzon/oracle/src/lib.rs (L449-459)
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
```
