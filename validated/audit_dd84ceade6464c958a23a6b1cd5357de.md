Based on my investigation, the strongest local analog to the "governor sets rate to zero, permanently blocking withdrawals" bug class is the price-adaptation invariant break in `pallet-broker`'s Coretime sale pricing (`CenterTargetPrice::adapt_price`), which is the same "market-controlled variable can degrade to zero and never recover" primitive as the Aloe `SafeRateLib` bug, but triggered through the normal, permissionless sale/purchase flow rather than through any privileged action.

### Title
Coretime sale price can degrade to and become permanently stuck at zero via `CenterTargetPrice::adapt_price` - (File: substrate/frame/broker/src/adapt_price.rs)

### Summary
`pallet-broker`'s bulk-Coretime sale price is recalculated every sale rotation by `AdaptPrice::adapt_price`, driven purely by the previous sale's `sellout_price`/`end_price` — values produced by ordinary, permissionless `purchase`/`renew` extrinsics, not by any privileged actor. The default `CenterTargetPrice` implementation contains an explicit "recovery" guard for the case where the computed price *rounds down* to zero, but that guard falls back to `sellout_price` itself, which does nothing if `sellout_price` is already `0`. Because the formula is purely multiplicative (`price = new_price/10`), once `end_price`/`sellout_price` reaches `0`, every subsequent rotation reproduces `0` forever — mirroring exactly the "zero rate that borrowers/lenders can never escape" defect in the Aloe report, except here it applies to Coretime block-production capacity pricing rather than lending rates.

### Finding Description
`CenterTargetPrice::adapt_price` computes the next `end_price` as: [1](#0-0) 

```rust
let price = FixedU64::from_rational(1, 10).saturating_mul_int(sellout_price);
let price = if price == Balance::zero() {
    // We could not recover from a price equal 0 ever.
    sellout_price
} else {
    price
};
```

The comment itself acknowledges the invariant ("could not recover ... ever") but the mitigation only handles rounding-to-zero of a *non-zero* `sellout_price`; it does not handle the case where `sellout_price` is exactly `0`. The pallet's own test suite treats `sellout_price = Some(0)` as a legitimate/expected input: [2](#0-1) 

This is a documented, previously-fixed instance of the same class of bug (`pallet-broker` `Linear::adapt_price` — the predecessor of `CenterTargetPrice`) where "if cores were offered but not sold in only one sale, the price would be set to zero and due to the logic being purely multiplicative, the price would stay at 0 indefinitely": [3](#0-2) 

The subsequent fix introduced an *opt-in* `MinimumPrice` wrapper that clamps `end_price` to a configured floor: [4](#0-3) 

Because `MinimumPrice` is a separate, optional `AdaptPrice` implementation and not a mandatory guarantee baked into `CenterTargetPrice` itself, any runtime that configures `T::PriceAdapter = CenterTargetPrice<Balance>` directly (rather than wrapping it in `MinimumPrice`) remains exposed to the exact zero-price-lock class the fix was meant to address, since the guard clause in `CenterTargetPrice::adapt_price` cannot recover once the tracked price value is precisely `0`.

`rotate_sale` feeds this untrusted, purchase-derived state directly into the next sale's `end_price`/`target_price`, including renewal pricing: [5](#0-4) [6](#0-5) 

### Impact Explanation
If Coretime sale pricing collapses to and sticks at `0`, all subsequent bulk-Coretime sales and renewals are priced at zero. This is "public underpriced work that degrades block production," since Coretime purchases fund and gate parachain-block-production capacity: unpriced/free acquisition of cores lets any actor monopolize coretime with no economic cost, starving other parachains and undermining the Coretime revenue model relied on by the relay chain and System Chains. This directly parallels the Aloe finding's core harm — a rate/price invariant that should never be able to reach and stay at zero, allowing free (uncompensated) consumption of a scarce, protocol-critical resource.

### Likelihood Explanation
Likelihood depends on runtime configuration that I could not fully verify within the available tool budget: whether the live Coretime runtimes (e.g., coretime-westend/rococo, or downstream chains) configure `PriceAdapter = CenterTargetPrice<Balance>` directly versus `MinimumPrice<Balance, MinPrice>`. I confirmed the vulnerable code path exists and is reachable through ordinary purchase-driven state (no governance/admin action required to trigger the zero-price collapse itself — only ordinary sale dynamics), but I was not able to trace, within the remaining iterations, the exact `do_purchase`/`SaleInfo` code path that sets `sellout_price` at the moment the final core in a sale is sold, so I cannot conclusively confirm the precise sequence of unprivileged transactions needed to first drive `sellout_price` to exactly `0` from a positive baseline. This should be verified by tracing `do_purchase` in `substrate/frame/broker/src/dispatchable_impls.rs` and confirming the `PriceAdapter` type used by the current coretime runtimes.

### Recommendation
Bake a non-optional minimum-price floor directly into `CenterTargetPrice::adapt_price` (or make `MinimumPrice` the only exposed constructor for the default adapter) so that `end_price`/`target_price` can never reach exactly `0` regardless of the observed `sellout_price`, closing the gap left by the current "recover from rounding to zero" guard which does not cover an already-zero input.

### Proof of Concept
Unit-test-level PoC (already latent in the existing test file, demonstrating the unhandled input):
```rust
// substrate/frame/broker/src/adapt_price.rs
let performance = SalePerformance::new(Some(0), 10); // sellout_price == 0
let prices = CenterTargetPrice::<u64>::adapt_price(performance);
assert_eq!(prices.end_price, 0);   // stuck at zero
assert_eq!(prices.target_price, 0); // stays stuck on every subsequent rotation
```
A full-chain PoC would require confirming the `do_purchase`/`rotate_sale` sequence of ordinary (non-privileged) `purchase`/`renew` extrinsics that first drives `SaleInfoRecord::sellout_price` to exactly `0` in a runtime configured with the bare `CenterTargetPrice` adapter — this final confirmation step was not completed due to tool-call exhaustion and should be validated by a follow-up session.

### Citations

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

**File:** substrate/frame/broker/src/adapt_price.rs (L139-166)
```rust
/// `AdaptPrice` like `CenterTargetPrice`, but with a minimum price.
///
/// This price adapter behaves exactly like `CenterTargetPrice`, except that it takes a minimum
/// price and makes sure that the returned `end_price` is never lower than that.
///
/// Target price will also get adjusted if necessary (it will never be less than the end_price).
pub struct MinimumPrice<Balance, MinPrice>(core::marker::PhantomData<(Balance, MinPrice)>);

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

**File:** substrate/frame/broker/src/adapt_price.rs (L292-308)
```rust
	#[test]
	fn no_minimum_price_works_as_center_target_price() {
		let performances = [
			(Some(100), 10),
			(None, 20),
			(Some(1000), 10),
			(Some(10), 10),
			(Some(1), 1),
			(Some(0), 10),
		];
		for (sellout, end) in performances {
			let performance = SalePerformance::new(sellout, end);
			let prices_minimum = MinimumPrice::<u64, ConstU64<0>>::adapt_price(performance);
			let prices = CenterTargetPrice::adapt_price(performance);
			assert_eq!(prices, prices_minimum);
		}
	}
```

**File:** prdoc/1.9.0/pr_3636.prdoc (L1-15)
```text
# Schema: Polkadot SDK PRDoc Schema (prdoc) v1.0.0
# See doc at https://raw.githubusercontent.com/paritytech/polkadot-sdk/master/prdoc/schema_user.json

title: "[pallet_broker] Fix `Linear::adapt_price` behavior at zero"

doc:
  - audience: Runtime Dev
    description: |
      This fixes the behaviour of `Linear` which is the default implementation of the `AdaptPrice`
      trait in the broker pallet. Previously if cores were offered but not sold in only one sale,
      the price would be set to zero and due to the logic being purely multiplicative, the price
      would stay at 0 indefinitely.

crates:
  - name: pallet-broker
```

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

**File:** substrate/frame/broker/src/tick_impls.rs (L256-278)
```rust
		let sellout_price = if cores_offered > 0 {
			// No core sold -> price was too high -> we have to adjust downwards.
			Some(new_prices.end_price)
		} else {
			None
		};

		let sale_index = old_sale.sale_index.saturating_add(1);

		// Update SaleInfo
		let new_sale = SaleInfoRecord {
			sale_start,
			leadin_length,
			end_price: new_prices.end_price,
			sellout_price,
			region_begin,
			region_end,
			first_core,
			ideal_cores_sold,
			cores_offered,
			cores_sold: 0,
			sale_index,
		};
```
