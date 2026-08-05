### Title
Coretime sale price for an entire period is derived from a single unmoving-averaged previous-sale checkpoint, letting an unprivileged buyer set a manipulated floor/target price for the next sale - (File: `substrate/frame/broker/src/tick_impls.rs`)

### Summary
`pallet-broker`'s `rotate_sale` computes the price for the *entire next sale period* (`end_price`/`target_price`, which floor both `sale_price` decay and `do_renew` price caps) from a single `SalePerformance` snapshot of the sale that just ended — `sellout_price`/`end_price` recorded by whichever account happens to make the last relevant purchase. There is no smoothing, moving-average, or multi-sale window; exactly one prior "checkpoint" feeds the price-setting formula for the whole following period, mirroring the Hyperdrive `_addLiquidity` circuit-breaker flaw where a safety-relevant reference value is taken from only the single latest checkpoint instead of an average that resists a last-second data point.

### Finding Description
`rotate_sale` calculates prices at the sale boundary using only the outgoing sale's recorded performance: [1](#0-0) 

`SalePerformance::from_sale` copies exactly the fields set during the just-ended sale (`sellout_price`, `end_price`, `cores_sold`, etc.) with no history from earlier sales: [2](#0-1) 

`sellout_price` — the single most-influential value fed to `adapt_price` — is set by `purchase_core`, an unprivileged, public-entrypoint function invoked from `do_purchase`/`do_renew`, and it is simply overwritten by *whichever* purchase happens to satisfy `cores_sold <= ideal_cores_sold`: [3](#0-2) 

All three shipped `AdaptPrice` implementations (`()` , `CenterTargetPrice`, `MinimumPrice`) take only this one-sale `SalePerformance` value, with no averaging across multiple past sales: [4](#0-3) 

The resulting `end_price`/`target_price` then determines:
- The starting price of the *entire next sale* via `sale_price`, which multiplies `end_price` by a leadin factor: [5](#0-4) 
- The floor for renewal prices for the whole next period via `do_renew`'s `price_cap = max(record.price + bump, end_price)`: [6](#0-5) 

This is structurally identical to the reported Hyperdrive defect: a safety/pricing-relevant reference is derived from exactly one most-recent checkpoint (here, one prior sale's last recorded trade) rather than a windowed/weighted average across several checkpoints, so a value set right at the checkpoint boundary — which an ordinary purchaser fully controls by timing their trade — propagates unchecked into decisions made immediately after the boundary (the entire following sale, including public `do_purchase`/`do_renew` calls in the very next block).

### Impact Explanation
Coretime purchased through `pallet-broker` grants the right to a parachain core, i.e., block-production capacity on the relay chain. Because the whole following sale's start/floor price is pinned to a single, attacker-influenceable prior-sale data point, an actor able to control the last qualifying purchase in a sale (buying at/near the sale's already-decayed floor while `cores_sold <= ideal_cores_sold` holds) fixes a low `end_price`/`target_price` for the *entire* next sale period. This directly depresses:
- The opening price of the next sale (`sale_price` at leadin start), letting cores be bought cheaply immediately after rotation.
- The floor for auto/manual renewal prices for that period (`do_renew`'s `price_cap`).

This falls under "public underpriced work that degrades block production" in the impact gate: coretime cores are the mechanism by which parachains obtain block-production capacity, and mispricing this resource both reduces relay-chain/treasury revenue and lets a single account cheaply acquire disproportionate core capacity for a whole sale period.

### Likelihood Explanation
The action requires only a normal signed account calling the existing public extrinsics `purchase`/`renew` (`do_purchase`/`do_renew`) — no admin, governance, relayer, or validator role is needed. No malicious peer/node assumption is required; it is purely a timing choice by an ordinary buyer near a sale boundary, similar to the "wait for a block, then trade" primitive described in the original report. Actual exploitability/magnitude is sensitive to configuration (`ideal_cores_sold`, `renewal_bump`, chosen `AdaptPrice` implementation such as `MinimumPrice`'s floor), exactly as the original report notes severity depends on pool parameters — this caveat carries over directly here and I could not fully confirm the economic magnitude of profit under production Coretime-chain configurations without further modeling.

### Recommendation
Extend `SalePerformance`/`AdaptPrice::adapt_price` to consume a smoothed/windowed average (e.g., an EMA or average over N prior sales) of `sellout_price`/`end_price`, rather than the single most recent sale's snapshot, so that one purchase timed at a sale boundary cannot unilaterally set the reference price for the following sale. This mirrors the report's own recommendation of using a moving window or multi-checkpoint average instead of a single latest-checkpoint value.

### Proof of Concept
Conceptual (not a full executable PoC, derived from existing test scaffolding in `substrate/frame/broker/src/tests.rs`, e.g. `renewals_affect_price`/`renewal_price_adjusts_to_lower_market_end`):
1. Configure a sale with `ideal_cores_sold` set such that the attacker can be the qualifying purchaser late in the sale while `cores_sold <= ideal_cores_sold` still holds (or simply be the sole buyer near leadin-end where price has decayed to `end_price`).
2. Attacker calls `Broker::purchase` at/just above the decayed floor price; `purchase_core` records this as `sale.sellout_price`.
3. At `rotate_sale` (sale boundary — a normal, permissionless block-driven transition, no admin action), `T::PriceAdapter::adapt_price(SalePerformance::from_sale(&old_sale))` computes the new sale's `end_price`/`target_price` purely from this single manipulated value.
4. Immediately in the new sale (next block), the attacker or accomplices call `Broker::purchase`/`Broker::renew` and obtain cores at the depressed opening price / depressed renewal cap for the entire new sale period, as shown by the existing `renewal_price_adjusts_to_lower_market_end` test demonstrating how strongly a single sale's end price propagates forward: [7](#0-6) 

**Note on limitations:** I was unable to fully quantify the economic feasibility of this attack (i.e., whether the cost of manipulating `sellout_price` is smaller than the discount obtained in the next sale) under realistic Coretime-chain configurations within this analysis; this is analogous to the original report's own acknowledgment that severity is configuration-dependent. A background Devin session with the ability to run the pallet-broker test suite and simulate concrete parameter sets (as used on Rococo/Westend Coretime and any planned Kusama/Polkadot Coretime chain) would be needed to confirm concrete profitable parameter ranges.

### Citations

**File:** substrate/frame/broker/src/tick_impls.rs (L176-184)
```rust

		// Calculate the start price for the upcoming sale.
		let new_prices = T::PriceAdapter::adapt_price(SalePerformance::from_sale(&old_sale));

		log::debug!(
			"Rotated sale, new prices: {:?}, {:?}",
			new_prices.end_price,
			new_prices.target_price
		);
```

**File:** substrate/frame/broker/src/adapt_price.rs (L63-79)
```rust
impl<Balance: Copy> SalePerformance<Balance> {
	/// Construct performance via data from a `SaleInfoRecord`.
	pub fn from_sale<BlockNumber>(record: &SaleInfoRecord<Balance, BlockNumber>) -> Self {
		Self {
			sellout_price: record.sellout_price,
			end_price: record.end_price,
			ideal_cores_sold: record.ideal_cores_sold,
			cores_offered: record.cores_offered,
			cores_sold: record.cores_sold,
		}
	}

	#[cfg(test)]
	fn new(sellout_price: Option<Balance>, end_price: Balance) -> Self {
		Self { sellout_price, end_price, ideal_cores_sold: 0, cores_offered: 0, cores_sold: 0 }
	}
}
```

**File:** substrate/frame/broker/src/adapt_price.rs (L94-137)
```rust
impl<Balance: Copy> AdaptPrice<Balance> for () {
	fn leadin_factor_at(_: FixedU64) -> FixedU64 {
		FixedU64::one()
	}
	fn adapt_price(performance: SalePerformance<Balance>) -> AdaptedPrices<Balance> {
		let price = performance.sellout_price.unwrap_or(performance.end_price);
		AdaptedPrices { end_price: price, target_price: price }
	}
}

/// Simple implementation of `AdaptPrice` with two linear phases.
///
/// One steep one downwards to the target price, which is 1/10 of the maximum price and a more flat
/// one down to the minimum price, which is 1/100 of the maximum price.
pub struct CenterTargetPrice<Balance>(core::marker::PhantomData<Balance>);

impl<Balance: FixedPointOperand> AdaptPrice<Balance> for CenterTargetPrice<Balance> {
	fn leadin_factor_at(when: FixedU64) -> FixedU64 {
		if when <= FixedU64::from_rational(1, 2) {
			FixedU64::from(100).saturating_sub(when.saturating_mul(180.into()))
		} else {
			FixedU64::from(19).saturating_sub(when.saturating_mul(18.into()))
		}
	}

	fn adapt_price(performance: SalePerformance<Balance>) -> AdaptedPrices<Balance> {
		let Some(sellout_price) = performance.sellout_price else {
			return AdaptedPrices {
				end_price: performance.end_price,
				target_price: FixedU64::from(10).saturating_mul_int(performance.end_price),
			};
		};

		let price = FixedU64::from_rational(1, 10).saturating_mul_int(sellout_price);
		let price = if price == Balance::zero() {
			// We could not recover from a price equal 0 ever.
			sellout_price
		} else {
			price
		};

		AdaptedPrices { end_price: price, target_price: sellout_price }
	}
}
```

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

**File:** substrate/frame/broker/src/dispatchable_impls.rs (L207-212)
```rust
		let begin = sale.region_end;
		let end_price = sale.end_price;
		// Renewals should never be priced lower than the current `end_price`:
		let price_cap = cmp::max(record.price + config.renewal_bump * record.price, end_price);
		let now = RCBlockNumberProviderOf::<T::Coretime>::current_block_number();
		let price = Self::sale_price(&sale, now).min(price_cap);
```

**File:** substrate/frame/broker/src/tests.rs (L618-685)
```rust
#[test]
/// Renewals adjust to lower end of market
fn renewal_price_adjusts_to_lower_market_end() {
	sp_tracing::try_init_simple();
	let b = 100_000_000;
	let region_length_blocks = 40;
	let config = ConfigRecord {
		advance_notice: 2,
		interlude_length: 10,
		leadin_length: 20,
		ideal_bulk_proportion: Perbill::from_percent(100),
		limit_cores_offered: None,
		// Region length is in time slices (2 blocks):
		region_length: 20,
		renewal_bump: Perbill::from_percent(10),
		contribution_timeout: 5,
	};
	TestExt::new_with_config(config.clone())
		.endow(1, b)
		.endow(2, b)
		.execute_with(|| {
			let price = 910;
			assert_ok!(Broker::do_start_sales(10, 2));
			advance_to(11);
			let region = Broker::do_purchase(1, u64::max_value()).unwrap();
			// Price is lower, because already one block in:
			let b = b - price;
			assert_eq!(balance(1), b);
			assert_ok!(Broker::do_assign(region, None, 1001, Final));
			advance_to(region_length_blocks);
			assert_noop!(Broker::do_purchase(1, u64::max_value()), Error::<Test>::TooEarly);

			let core = Broker::do_renew(1, region.core).unwrap();
			// First renewal has same price as initial purchase.
			let b = b - price;
			assert_eq!(balance(1), b);
			// Ramp up price:
			advance_to(region_length_blocks + config.interlude_length + 1);
			Broker::do_purchase(2, u64::max_value()).unwrap();

			advance_to(2 * region_length_blocks);
			assert_ok!(Broker::do_renew(1, core));
			// Renewal bump in effect
			let price = price + Perbill::from_percent(10) * price;
			let b = b - price;
			assert_eq!(balance(1), b);
			// Ramp up price again:
			advance_to(2 * region_length_blocks + config.interlude_length + 1);
			Broker::do_purchase(2, u64::max_value()).unwrap();

			advance_to(3 * region_length_blocks);
			assert_ok!(Broker::do_renew(1, core));
			// Renewal bump still in effect
			let price = price + Perbill::from_percent(10) * price;
			let b = b - price;
			assert_eq!(balance(1), b);
			// No further price ramp up necessary - the price of this sale is relevant for next
			// renewal.
			let end_price = SaleInfo::<Test>::get().unwrap().end_price;

			advance_to(4 * region_length_blocks);
			assert_ok!(Broker::do_renew(1, core));
			// Renewal bump trumped by end price of previous sale.
			let price = end_price;
			let b = b - price;
			assert_eq!(balance(1), b);
		});
}
```
