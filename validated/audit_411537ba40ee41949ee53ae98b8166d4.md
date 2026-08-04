Based on my research, I found a strong local analog to the Curve `AggregateStablePrice` bug class in `pallet-broker`'s price-adaptation logic.

### Title
`CenterTargetPrice::adapt_price` recovers from a manipulable zero/near-zero price using the corrupted `sellout_price` itself, allowing an attacker to lock in an artificially low Coretime price - (File: `substrate/frame/broker/src/adapt_price.rs`)

### Summary
`pallet-broker`'s default `AdaptPrice` implementation, `CenterTargetPrice::adapt_price`, computes the next sale's `end_price` as `1/10` of the previous sale's `sellout_price`. When that computation floors to `0`, the code "recovers" by falling back to `sellout_price` itself rather than to a safe reference price, unlike the fixed historical bug in `Linear::adapt_price` (see `prdoc/1.9.0/pr_3636.prdoc`) where the price got stuck at `0` forever. The current fallback trades one problem for another: an attacker who can influence `sellout_price` to be a tiny nonzero value can force the pricing algorithm to lock onto that attacker-chosen value as both `end_price` and `target_price` for the next sale, similar in spirit to Curve's `AggregateStablePrice` returning a fixed sentinel (`10**18`) when the invariant `Dsum == 0` — both are "degenerate-input → attacker-controlled/knowable price" fallbacks in a price-setting function later trusted for economic settlement (Coretime `renewal` pricing, `PotentialRenewalRecord.price`).

### Finding Description
In `substrate/frame/broker/src/adapt_price.rs`: [1](#0-0) 

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

The `sellout_price` is the price at which the *last* core sold in a sale [2](#0-1) . Because it is an integer `Balance`, `1/10 * sellout_price` floors to `0` whenever `sellout_price < 10`. In that branch the code sets `price = sellout_price` (a tiny, attacker-influenced value, e.g. `1`), and both `end_price` and `target_price` for the *next* sale become exactly that tiny value — with no floor and no reference to any external/independent price signal. This is directly wired into the sale-rotation logic: [3](#0-2) 

`new_prices.end_price`/`new_prices.target_price` then become the new sale's `end_price`, the `sellout_price` candidate for the *following* sale (`sellout_price = Some(new_prices.end_price)` at line 258 of `tick_impls.rs`), and the price stored in `PotentialRenewalRecord` used for lease renewals: [4](#0-3) 

The "fix" documented in `prdoc/1.9.0/pr_3636.prdoc` only prevented the price from getting permanently stuck at literal `0` [5](#0-4)  — it did not add any floor/minimum. A later PR (`stable2503-6`/`stable2506` pr_8630) introduced a separate `MinimumPrice` adapter that *can* enforce a floor, but it is opt-in configuration, not the behavior of the default `CenterTargetPrice` [6](#0-5) . Any runtime using `CenterTargetPrice` directly (without wrapping it in `MinimumPrice`) inherits the unguarded recovery-to-`sellout_price` path.

### Impact Explanation
An unprivileged buyer can deliberately buy the last core of a sale at the lowest price permitted during the leadin decay (as low as `1` unit, since there is no minimum bound in `CenterTargetPrice`), making `sellout_price = 1`. This single action sets both `end_price` and `target_price` for every subsequent sale/renewal to `1`, since `1/10 * 1 == 0`, triggering the "recover" branch that just reuses the attacker's `1`. From that point, the entire Coretime market for that chain is anchored to a near-zero price: new sales start their leadin from `1`, and lease renewals recorded via `PotentialRenewalRecord` are priced at `1` — a public underpriced resource allocation that degrades the economic security of block-space allocation and can be sustained indefinitely by repeatedly buying the last core cheaply each sale.

### Likelihood Explanation
This requires only an unprivileged account with minimal funds purchasing the cheapest (last) core of a sale — a normal, permissionless `purchase`/`renew` action, not a privileged or governance action, and not reliant on a malicious relayer/validator/collator. It is easiest to trigger at chain launch or during low-demand periods, which matches exactly the "early stage of the project" caveat in the original Curve report.

### Recommendation
Enforce a protocol-level minimum price (not opt-in) in the default `CenterTargetPrice::adapt_price`, or make its "recover from zero" branch use a bounded reference price (e.g., a configured floor or the previous `end_price`) instead of directly re-adopting the attacker-influenced `sellout_price`. Consider always requiring `MinimumPrice` wrapping in production runtime configs, or moving the floor logic into `CenterTargetPrice` itself so it cannot be omitted by configuration.

### Proof of Concept
1. Configure `pallet-broker` with the default `CenterTargetPrice` adapter (no `MinimumPrice` wrapper).
2. During a live sale, wait for or drive the leadin price down, and have any unprivileged account purchase the final (last) core when the price is `< 10` (e.g. `sellout_price = 1`).
3. On `rotate_sale`, `adapt_price` computes `1/10 * 1 = 0`, hits the zero-guard, and returns `AdaptedPrices { end_price: 1, target_price: 1 }` [7](#0-6) .
4. The next sale's `end_price` and any lease `PotentialRenewalRecord.price` are now `1`, and the cycle can repeat every sale by the same attacker, permanently pinning Coretime pricing to a de minimis value.

### Citations

**File:** substrate/frame/broker/src/adapt_price.rs (L25-34)
```rust
/// Performance of a past sale.
#[derive(Copy, Clone)]
pub struct SalePerformance<Balance> {
	/// The price at which the last core was sold.
	///
	/// Will be `None` if no cores have been offered.
	pub sellout_price: Option<Balance>,

	/// The minimum price that was achieved in this sale.
	pub end_price: Balance,
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

**File:** substrate/frame/broker/src/tick_impls.rs (L177-184)
```rust
		// Calculate the start price for the upcoming sale.
		let new_prices = T::PriceAdapter::adapt_price(SalePerformance::from_sale(&old_sale));

		log::debug!(
			"Rotated sale, new prices: {:?}, {:?}",
			new_prices.end_price,
			new_prices.target_price
		);
```

**File:** substrate/frame/broker/src/tick_impls.rs (L229-241)
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
				Self::deposit_event(Event::LeaseEnding { when: region_end, task });
```

**File:** prdoc/1.9.0/pr_3636.prdoc (L1-14)
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
```

**File:** prdoc/stable2503-6/pr_8630.prdoc (L1-16)
```text
title: "Broker: Introduce min price and adjust renewals to lower market"

doc:
- audience: Runtime Dev
  description: |-
    pallet-broker now provides an additional `AdaptPrice` implementation:
    `MinimumPrice`. This price adapter works exactly the same as the
    `CenterTargetPrice` adapter, except that it can be configured with a
    minimum price. If set, it will never drop the returned `end_price` (nor the
    `target_price`) below that minimum. 

    Apart from having an adapter to ensure a minimum price, the behavior of
    renewals was also adjusted: Renewals are now either bumped by renewal bump
    or set to the `end_price` of the current sale - whatever number is higher.
    This ensures some market coupling of renewal prices, while still
    maintaining some predictability. 
```
