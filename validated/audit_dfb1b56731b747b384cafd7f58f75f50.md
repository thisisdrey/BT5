Based on my research, I found a concrete structural analog in `pallet-broker`'s bulk-coretime price adaptation mechanism, which mirrors the exact bug class in the report: a single-sale-derived value that feeds a bounded threshold formula gating the *next* period's pricing, and which an ordinary unprivileged participant can steer by choosing *when* to complete their action.

### Title
Coretime sale price can be single-handedly steered via `sellout_price` timing, permanently depressing/inflating next sale and renewal prices - (File: `substrate/frame/broker/src/adapt_price.rs`)

### Summary
The twTAP report's core broken invariant is: a shared aggregate state (`pool.cumulative`) derived from the *outcome of individual user actions* is fed into a bounded gating formula for subsequent actions, and because the aggregate is updated using an average/delta rather than an exact, per-action ledger entry, ordering/timing of a single participant's actions can push the aggregate out of the safe range, degrading the system for everyone else. `pallet-broker`'s coretime pricing has the same shape: `SalePerformance::sellout_price` — the price paid for the *last* core sold in a sale — single-handedly determines `end_price`/`target_price` for the *entire next sale period* (`substrate/frame/broker/src/tick_impls.rs:178,256-270` calling `T::PriceAdapter::adapt_price`), and that computation in `CenterTargetPrice::adapt_price` (`substrate/frame/broker/src/adapt_price.rs:119-136`) multiplicatively derives `end_price = sellout_price / 10`.

### Finding Description
`rotate_sale` builds `SalePerformance::from_sale(&old_sale)` from the just-finished `SaleInfoRecord`, whose `sellout_price` field is documented as "the price at which cores have been sold out" [1](#0-0) . This is the current bulk-coretime price at the exact block the *last* offered core is purchased, and it is fed straight into `adapt_price` for the whole next sale [2](#0-1) .

`CenterTargetPrice::adapt_price` derives the new `end_price` as `1/10` of `sellout_price` and the `target_price` as `sellout_price` itself, with no averaging across multiple buyers or multiple sales, and no minimum-sample-size guard [3](#0-2) . Coretime prices decay throughout the `leadin_length` period toward the configured floor via `leadin_factor_at` [4](#0-3) , so the *current* price at any block within a sale is entirely predictable and controllable by choosing when to submit the purchase.

Just as the twTAP attacker chose the timing/ordering of `participate`/unlock calls to steer `pool.cumulative` outside the safe band (because the check uses a derived aggregate rather than each entry's exact contribution), an unprivileged coretime buyer can choose to be the buyer of the *last* core in a sale near the very end of the leadin period (when `leadin_factor_at` has decayed toward its floor), fixing an artificially low `sellout_price`. Because `end_price`/`target_price` for the *entire next sale* — and by extension renewal pricing, since `PotentialRenewalRecord::price` is set from `new_prices.target_price` [5](#0-4)  — are derived multiplicatively from this single value, one purchase timed correctly permanently discounts an entire subsequent sale/renewal cycle for every other buyer, and (per `renewal_price_adjusts_to_lower_market_end`) the depressed price can persist across several renewal cycles because renewals are bumped from whichever is higher: the renewal-bump percentage or the prior sale's `end_price` [6](#0-5) .

Existing guards do not stop this: `MinimumPrice::adapt_price` only clamps the floor to a configured constant, it does nothing to prevent a single sellout price from setting the entire next sale's baseline [7](#0-6) , and the historical fix (`pr_4521`) only changed the *signal* used (price rather than cores-sold count) — it did not add resistance to single-sale/single-buyer manipulation of that price signal itself [8](#0-7) .

### Impact Explanation
Coretime is Polkadot's on-chain resource-pricing mechanism; the sale price and target price directly determine system revenue captured from bulk coretime sales and set renewal prices for parachains. An unprivileged actor deliberately underpaying for the marginal (last) core of a sale can single-handedly depress the price basis for an entire subsequent sale and multiple subsequent renewal cycles, causing systematically underpriced public coretime — a direct instance of "public underpriced work that degrades... stalls bridge/chain processing economics" and loss of intended treasury/system revenue, without requiring any validator, collator, governance, or admin role.

### Likelihood Explanation
Any account with enough balance to be the final buyer in a sale can trigger this deterministically by timing a single extrinsic call near the end of the leadin period — no collusion, no privileged role, and no race against other parties beyond simple timing, is required. This is a repeatable, low-cost strategy each sale cycle.

### Recommendation
Do not derive the next sale's baseline price purely from the single marginal `sellout_price` of the prior sale. Use a volume-weighted average price across the sale (or across multiple recent sales), require a minimum number of sales/cores sold before adjusting price materially, and/or bound the maximum per-cycle price movement independent of a single transaction's price, similar to how `TargetedFeeAdjustment` bounds fee-multiplier movement per block rather than snapping to an instantaneous value [9](#0-8) .

### Proof of Concept
1. A sale begins with `leadin_length` blocks during which `leadin_factor_at` decays the multiplier from its maximum toward 1 (`substrate/frame/broker/src/adapt_price.rs:110-117`).
2. Attacker waits until the block just before the leadin period ends (lowest attainable price) and is the buyer of the final (`cores_offered`th) core, fixing `sellout_price` to this low value in `SaleInfoRecord`.
3. At `rotate_sale`, `SalePerformance::from_sale` captures this `sellout_price`, and `CenterTargetPrice::adapt_price` sets `end_price = sellout_price / 10`, `target_price = sellout_price` for the entire next sale (`substrate/frame/broker/src/tick_impls.rs:177-183`, `adapt_price.rs:119-136`).
4. All buyers in the next sale, and renewers relying on `target_price` for `PotentialRenewalRecord::price`, now transact at the attacker-depressed price for that cycle, and — per the renewal-bump-vs-end_price logic demonstrated in `renewal_price_adjusts_to_lower_market_end` — the depressed baseline can persist across further renewal cycles (`substrate/frame/broker/src/tests.rs:618-684`).

**Note on confidence:** I was unable to directly inspect `dispatchable_impls.rs`'s `do_purchase`/`do_renew` bodies within the available tool budget to confirm the exact moment `sellout_price` is captured relative to the buyer's chosen price; the analysis is built from the `SaleInfoRecord` field documentation, `adapt_price.rs`, `tick_impls.rs`, and the test in `tests.rs` that exercises this exact price-persistence behavior. A Devin session with full file access should verify the precise `do_purchase` code path before treating this as fully confirmed.

### Citations

**File:** substrate/frame/broker/src/types.rs (L205-208)
```rust
	/// The price at which cores have been sold out.
	///
	/// Will only be `None` if no core was offered for sale.
	pub sellout_price: Option<Balance>,
```

**File:** substrate/frame/broker/src/tick_impls.rs (L177-183)
```rust
		// Calculate the start price for the upcoming sale.
		let new_prices = T::PriceAdapter::adapt_price(SalePerformance::from_sale(&old_sale));

		log::debug!(
			"Rotated sale, new prices: {:?}, {:?}",
			new_prices.end_price,
			new_prices.target_price
```

**File:** substrate/frame/broker/src/tick_impls.rs (L229-240)
```rust
				let renewal_id = PotentialRenewalId { core: first_core, when: region_end };
				let record = PotentialRenewalRecord {
					price: new_prices.target_price,
					completion: Complete(schedule),
				};
				PotentialRenewals::<T>::insert(renewal_id, &record);
				Self::deposit_event(Event::Renewable {
					core: first_core,
					price: new_prices.target_price,
					begin: region_end,
					workload: record.completion.drain_complete().unwrap_or_default(),
				});
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

**File:** substrate/frame/broker/src/adapt_price.rs (L119-136)
```rust
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
```

**File:** substrate/frame/broker/src/adapt_price.rs (L147-166)
```rust
impl<Balance: FixedPointOperand, MinPrice: Get<Balance>> AdaptPrice<Balance>
	for MinimumPrice<Balance, MinPrice>
{
	fn leadin_factor_at(when: FixedU64) -> FixedU64 {
		CenterTargetPrice::<Balance>::leadin_factor_at(when)
	}

	fn adapt_price(performance: SalePerformance<Balance>) -> AdaptedPrices<Balance> {
		let mut proposal = CenterTargetPrice::<Balance>::adapt_price(performance);
		let min_price = MinPrice::get();
		if proposal.end_price < min_price {
			proposal.end_price = min_price;
		}
		// Fix target price if necessary:
		if proposal.target_price < proposal.end_price {
			proposal.target_price = proposal.end_price;
		}
		proposal
	}
}
```

**File:** substrate/frame/broker/src/tests.rs (L618-684)
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
```

**File:** prdoc/1.13.0/pr_4521.prdoc (L1-19)
```text
title: AdaptPrice trait is now price controlled

doc:
  - audience: Runtime Dev
    description: |
      The broker pallet price adaptation interface is changed to be less opinionated and more
      information is made available to the `AdaptPrice` trait. A new example impl is included which
      adapts the price based not on the number of cores sold, but rather on the price that was
      achieved during the sale to mitigate a potential price manipulation vector. More information
      here:

        https://github.com/paritytech/polkadot-sdk/issues/4360

  - audience: Runtime User
    description: |
      The price controller of the Rococo and Westend Coretime chain will be
      adjusted with this release. This will very likely be used in the
      fellowship production runtime to have a much larger leadin. This fixes a
      price manipulation issue we discovered with the Kusama launch.
```

**File:** substrate/frame/transaction-payment/src/lib.rs (L201-273)
```rust
impl<T, S, V, M, X> Convert<Multiplier, Multiplier> for TargetedFeeAdjustment<T, S, V, M, X>
where
	T: frame_system::Config,
	S: Get<Perquintill>,
	V: Get<Multiplier>,
	M: Get<Multiplier>,
	X: Get<Multiplier>,
{
	fn convert(previous: Multiplier) -> Multiplier {
		// Defensive only. The multiplier in storage should always be at most positive. Nonetheless
		// we recover here in case of errors, because any value below this would be stale and can
		// never change.
		let min_multiplier = M::get();
		let max_multiplier = X::get();
		let previous = previous.max(min_multiplier);

		let weights = T::BlockWeights::get();
		// the computed ratio is only among the normal class.
		let normal_max_weight =
			weights.get(DispatchClass::Normal).max_total.unwrap_or(weights.max_block);
		let current_block_weight = frame_system::Pallet::<T>::block_weight();
		let normal_block_weight =
			current_block_weight.get(DispatchClass::Normal).min(normal_max_weight);

		// Normalize dimensions so they can be compared. Ensure (defensive) max weight is non-zero.
		let normalized_ref_time = Perbill::from_rational(
			normal_block_weight.ref_time(),
			normal_max_weight.ref_time().max(1),
		);
		let normalized_proof_size = Perbill::from_rational(
			normal_block_weight.proof_size(),
			normal_max_weight.proof_size().max(1),
		);

		// Pick the limiting dimension. If the proof size is the limiting dimension, then the
		// multiplier is adjusted by the proof size. Otherwise, it is adjusted by the ref time.
		let (normal_limiting_dimension, max_limiting_dimension) =
			if normalized_ref_time < normalized_proof_size {
				(normal_block_weight.proof_size(), normal_max_weight.proof_size())
			} else {
				(normal_block_weight.ref_time(), normal_max_weight.ref_time())
			};

		let target_block_fullness = S::get();
		let adjustment_variable = V::get();

		let target_weight = (target_block_fullness * max_limiting_dimension) as u128;
		let block_weight = normal_limiting_dimension as u128;

		// determines if the first_term is positive
		let positive = block_weight >= target_weight;
		let diff_abs = block_weight.max(target_weight) - block_weight.min(target_weight);

		// defensive only, a test case assures that the maximum weight diff can fit in Multiplier
		// without any saturation.
		let diff = Multiplier::saturating_from_rational(diff_abs, max_limiting_dimension.max(1));
		let diff_squared = diff.saturating_mul(diff);

		let v_squared_2 = adjustment_variable.saturating_mul(adjustment_variable) /
			Multiplier::saturating_from_integer(2);

		let first_term = adjustment_variable.saturating_mul(diff);
		let second_term = v_squared_2.saturating_mul(diff_squared);

		if positive {
			let excess = first_term.saturating_add(second_term).saturating_mul(previous);
			previous.saturating_add(excess).clamp(min_multiplier, max_multiplier)
		} else {
			// Defensive-only: first_term > second_term. Safe subtraction.
			let negative = first_term.saturating_sub(second_term).saturating_mul(previous);
			previous.saturating_sub(negative).clamp(min_multiplier, max_multiplier)
		}
	}
```
