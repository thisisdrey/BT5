### Title
Bulk Coretime Dutch-auction leadin price collapses to floor after a parachain/relay-parent stall, letting a purchaser buy cores far below market price - (File: `substrate/frame/broker/src/utility_impls.rs`)

### Summary
`pallet-broker`'s bulk coretime sale implements a Dutch-auction-style descending price (the "leadin period") exactly analogous to the L2 auction in the external report. The current price is computed on demand from the elapsed *relay-chain* block number obtained through `RCBlockNumberProviderOf<T::Coretime>`, which on the Coretime system chain resolves to Cumulus's `RelaychainDataProvider`. If the Coretime parachain fails to make progress against the relay chain for a stretch of time (e.g. it misses relay-parent inclusion, is squeezed out of blockspace, or otherwise stalls) — the exact analog of "sequencer offline" — the leadin timer keeps advancing in wall-clock/relay-block terms while nobody can submit a competing purchase. When the chain resumes, the elapsed-time calculation is capped at `leadin_length` and the price is found already collapsed to (or very near) `end_price`, the auction floor, with no mechanism to detect or invalidate a stall that occurred during the leadin period.

### Finding Description
The price at any given relay-chain block is: [1](#0-0) 

```rust
pub fn sale_price(sale: &SaleInfoRecordOf<T>, now: RelayBlockNumberOf<T>) -> BalanceOf<T> {
	let num = now.saturating_sub(sale.sale_start).min(sale.leadin_length).saturated_into();
	let through = FixedU64::from_rational(num, sale.leadin_length.saturated_into());
	T::PriceAdapter::leadin_factor_at(through).saturating_mul_int(sale.end_price)
}
```

`now` is supplied via `RCBlockNumberProviderOf::<T::Coretime>::current_block_number()`. On the coretime-westend/rococo runtimes this provider is `CoretimeAllocator::RelayChainBlockNumberProvider = RelaychainDataProvider<Runtime>` [2](#0-1) , whose value comes from the last-known relay parent stored by the parachain-system inherent: [3](#0-2) 

```rust
pub struct RelaychainDataProvider<T>(core::marker::PhantomData<T>);
impl<T: Config> BlockNumberProvider for RelaychainDataProvider<T> {
	type BlockNumber = relay_chain::BlockNumber;
	fn current_block_number() -> relay_chain::BlockNumber {
		ValidationData::<T>::get()
			.map(|d| d.relay_parent_number)
			.unwrap_or_else(|| Pallet::<T>::last_relay_block_number())
	}
}
```

The `AdaptPrice::leadin_factor_at` computes a strictly decreasing multiplier as `through` (fraction of `leadin_length` elapsed) grows, e.g. `CenterTargetPrice`'s two-phase linear decay from 100x down to 1x over the leadin window [4](#0-3) , and the price is documented explicitly as a "descending-price" auction phase in the pallet README [5](#0-4) .

The broken invariant: the decay is purely a function of *elapsed relay-chain blocks since `sale_start`*, with no requirement that the Coretime chain itself was continuously live and processing purchase transactions during that interval. If the parachain that hosts `pallet-broker` cannot include blocks on the relay chain for a period (collator outage, congestion, or any other cause preventing block production/inclusion — the exact functional equivalent of "sequencer down" in the report), nobody can call `purchase`/`renew` during the stall. But the relay-parent block number the chain eventually observes on resumption has still advanced by the full stalled duration. Because `num` is capped by `.min(sale.leadin_length)`, any stall that is ≥ the configured `leadin_length` (which can be a small window, e.g. `leadin_length: 2` used in the emulated tests [6](#0-5) ) causes `sale_price` to read as `end_price` (the auction floor) the instant the chain resumes — with zero opportunity for any other participant to have bid during the outage.

There is no guard anywhere in `do_purchase`/`purchase_core` [7](#0-6)  or in `rotate_sale`/`do_tick` [8](#0-7)  that checks whether the elapsed leadin interval actually corresponds to a period during which the Coretime chain was live and accepting transactions. The `min(sale.leadin_length)` clamp, intended to bound the decay, is precisely the mechanism that guarantees the floor price is reached deterministically once the stall exceeds the leadin window — mirroring the report's core finding that "the auction will continue to decrease in price while the sequencer is offline," and the resumed chain/L2 sees the auction already at its worst price.

### Impact Explanation
Whoever is first able to submit `purchase` after the Coretime chain resumes captures Bulk Coretime cores at (or near) the sale's minimum floor price, even though under normal, continuously-live operation the price would have been fixed via competitive purchase somewhere along the descending curve reflecting real market interest. This directly under-prices a public, permissionless sale of a valuable, scarce resource (parachain coretime), causing value leakage from the chain's `OnRevenue` beneficiary and unfairly favoring whichever account can act first on resumption (e.g., an operator running its own collator/relayer infra, or simply the fastest transaction after recovery). This is a "public underpriced work" / broken proof-of-market-clearing impact consistent with the required impact gate (degraded/incorrect settlement of a public sale mechanism), not a governance or admin-input error, since the manager cannot prevent chain-level stalls by tuning `end_price`/`leadin_length` alone — a sufficiently long stall (even one that is itself outside anyone's control) collapses the price regardless of parameters.

### Likelihood Explanation
Requires no malicious validator, collator, relayer, or governance action — purely a liveness/availability condition on the Coretime parachain (or a lagging/stale relay-parent read via `ValidationData`) coinciding with an in-progress leadin window, which is a plausible operational scenario for any live chain (congestion, collator downtime, XCM/relay delays). The exploiting transaction itself is an ordinary, unprivileged `purchase` call, requiring only being early after resumption.

### Recommendation
Do not derive the leadin discount solely from elapsed relay-chain blocks with no continuity guarantee. Track the Coretime chain's own liveness (e.g., number of parachain blocks actually authored since `sale_start`, or bound the leadin decay by `frame_system` block progress rather than raw relay-parent delta), and/or detect a stale/jumped relay-parent reading (large single-step jump in `RelaychainDataProvider::current_block_number()`) and pause/extend the leadin window by the stalled duration, or invalidate/reprice the sale if a stall is detected during the leadin period, analogous to checking a sequencer-uptime oracle before trusting a decayed Dutch-auction price.

### Proof of Concept
1. Configure and start a bulk coretime sale with a short `leadin_length` (as in the emulated tests, e.g. `leadin_length: 2` relay blocks) via `start_sales`/`configure` [9](#0-8) .
2. Simulate a Coretime-chain stall: stop advancing the parachain (or, in test harness, do not call `Broker::on_initialize`/do not update `ValidationData`) for a number of relay-chain blocks greater than `leadin_length`, while the relay chain itself continues to progress.
3. Resume the parachain and read `RCBlockNumberProviderOf::<T::Coretime>::current_block_number()`; observe it has jumped forward by more than `leadin_length` since `sale.sale_start`.
4. Call `Broker::purchase` (or query `Broker::sale_price` runtime API) immediately: `sale_price` returns `T::PriceAdapter::leadin_factor_at(1.0) * end_price`, i.e., the auction floor, per the `.min(sale.leadin_length)` clamp in `sale_price` [1](#0-0) , confirming a full price collapse was reached with zero legitimate competitive purchases having occurred during the leadin window.

### Citations

**File:** substrate/frame/broker/src/utility_impls.rs (L62-66)
```rust
	pub fn sale_price(sale: &SaleInfoRecordOf<T>, now: RelayBlockNumberOf<T>) -> BalanceOf<T> {
		let num = now.saturating_sub(sale.sale_start).min(sale.leadin_length).saturated_into();
		let through = FixedU64::from_rational(num, sale.leadin_length.saturated_into());
		T::PriceAdapter::leadin_factor_at(through).saturating_mul_int(sale.end_price)
	}
```

**File:** substrate/frame/broker/src/utility_impls.rs (L78-91)
```rust
	pub(crate) fn purchase_core(
		who: &T::AccountId,
		price: BalanceOf<T>,
		sale: &mut SaleInfoRecordOf<T>,
	) -> Result<CoreIndex, DispatchError> {
		Self::charge(who, price)?;
		log::debug!("Purchased core at: {:?}", price);
		let core = sale.first_core.saturating_add(sale.cores_sold);
		sale.cores_sold.saturating_inc();
		if sale.cores_sold <= sale.ideal_cores_sold || sale.sellout_price.is_none() {
			sale.sellout_price = Some(price);
		}
		Ok(core)
	}
```

**File:** cumulus/parachains/runtimes/coretime/coretime-westend/src/coretime.rs (L64-72)
```rust
/// Type that implements the `CoretimeInterface` for the allocation of Coretime. Meant to operate
/// from the parachain context. That is, the parachain provides a market (broker) for the sale of
/// coretime, but assumes a `CoretimeProvider` (i.e. a Relay Chain) to actually provide cores.
pub struct CoretimeAllocator;
impl CoretimeInterface for CoretimeAllocator {
	type AccountId = AccountId;
	type Balance = Balance;
	type RelayChainBlockNumberProvider = RelaychainDataProvider<Runtime>;

```

**File:** cumulus/pallets/parachain-system/src/lib.rs (L2046-2055)
```rust
pub struct RelaychainDataProvider<T>(core::marker::PhantomData<T>);

impl<T: Config> BlockNumberProvider for RelaychainDataProvider<T> {
	type BlockNumber = relay_chain::BlockNumber;

	fn current_block_number() -> relay_chain::BlockNumber {
		ValidationData::<T>::get()
			.map(|d| d.relay_parent_number)
			.unwrap_or_else(|| Pallet::<T>::last_relay_block_number())
	}
```

**File:** substrate/frame/broker/src/adapt_price.rs (L110-117)
```rust
impl<Balance: FixedPointOperand> AdaptPrice<Balance> for CenterTargetPrice<Balance> {
	fn leadin_factor_at(when: FixedU64) -> FixedU64 {
		if when <= FixedU64::from_rational(1, 2) {
			FixedU64::from(100).saturating_sub(when.saturating_mul(180.into()))
		} else {
			FixedU64::from(19).saturating_sub(when.saturating_mul(18.into()))
		}
	}
```

**File:** substrate/frame/broker/README.md (L14-26)
```markdown
### The Sale

```nocompile
					1 1 1 1 1 1 1 1 1 1 2 2 2 2 2 2 2 2
0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7
--------------------------------------------------------
< interlude  >
			  <                   sale                 >
							... of which ...
			  <  descending-price   ><   fixed-price   >
														| <-------\
price fixed, unsold assigned to instapool, system cores reserved -/
```
```

**File:** cumulus/parachains/integration-tests/emulated/tests/coretime/coretime-westend/src/tests/coretime_interface.rs (L66-85)
```rust
		// Configure broker and start sales.
		let config = ConfigRecord {
			advance_notice: 1,
			interlude_length: 1,
			leadin_length: 2,
			region_length: 1,
			ideal_bulk_proportion: Perbill::from_percent(40),
			limit_cores_offered: None,
			renewal_bump: Perbill::from_percent(2),
			contribution_timeout: 1,
		};
		assert_ok!(<CoretimeWestend as CoretimeWestendPallet>::Broker::configure(
			coretime_root_origin.clone(),
			config
		));
		assert_ok!(<CoretimeWestend as CoretimeWestendPallet>::Broker::start_sales(
			coretime_root_origin,
			100,
			0
		));
```

**File:** substrate/frame/broker/src/tick_impls.rs (L35-86)
```rust
	pub(crate) fn do_tick() -> Weight {
		let mut meter = WeightMeter::new();
		meter.consume(T::WeightInfo::do_tick_base());

		let (mut status, config) = match (Status::<T>::get(), Configuration::<T>::get()) {
			(Some(s), Some(c)) => (s, c),
			_ => return meter.consumed(),
		};

		if Self::process_core_count(&mut status) {
			meter.consume(T::WeightInfo::process_core_count(status.core_count.into()));
		}

		if Self::process_revenue() {
			meter.consume(T::WeightInfo::process_revenue());
		}

		if let Some(commit_timeslice) = Self::next_timeslice_to_commit(&config, &status) {
			status.last_committed_timeslice = commit_timeslice;
			if let Some(sale) = SaleInfo::<T>::get() {
				if commit_timeslice >= sale.region_begin {
					// Sale can be rotated.
					Self::rotate_sale(sale, &config, &status);
					meter.consume(T::WeightInfo::rotate_sale(status.core_count.into()));
				}
			}

			Self::process_pool(commit_timeslice, &mut status);
			meter.consume(T::WeightInfo::process_pool());

			let timeslice_period = T::TimeslicePeriod::get();
			let rc_begin = RelayBlockNumberOf::<T>::from(commit_timeslice) * timeslice_period;
			for core in 0..status.core_count {
				Self::process_core_schedule(commit_timeslice, rc_begin, core);
				meter.consume(T::WeightInfo::process_core_schedule());
			}
		}

		let current_timeslice = Self::current_timeslice();
		if status.last_timeslice < current_timeslice {
			status.last_timeslice.saturating_inc();
			let rc_block = T::TimeslicePeriod::get() * status.last_timeslice.into();
			T::Coretime::request_revenue_info_at(rc_block);
			meter.consume(T::WeightInfo::request_revenue_info_at());
			T::Coretime::on_new_timeslice(status.last_timeslice);
			meter.consume(T::WeightInfo::on_new_timeslice());
		}

		Status::<T>::put(&status);

		meter.consumed()
	}
```
