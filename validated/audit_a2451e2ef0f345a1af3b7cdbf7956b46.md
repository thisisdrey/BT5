Audit Report

## Title
`CenterTargetPrice::adapt_price` can lock the Broker coretime sale price at zero forever when a sale sells out at price zero - ([File: substrate/frame/broker/src/adapt_price.rs])

## Summary
`CenterTargetPrice::adapt_price` only substitutes `sellout_price` for the computed `1/10 * sellout_price` when that multiplication rounds down to zero, but if `sellout_price` itself is already `0`, the substitution reproduces `0`, so both `end_price` and `target_price` become `0` and remain `0` on every subsequent call. This value is written into `SaleInfoRecord::end_price`/`sellout_price` and `PotentialRenewalRecord::price` in `rotate_sale`, permanently underpricing coretime cores and renewals unless the runtime opts into wrapping the adapter with a nonzero `MinimumPrice`.

## Finding Description
`CenterTargetPrice::adapt_price` computes the next `end_price` as `1/10 * sellout_price` and only guards the case where this multiplication rounds to zero, falling back to `sellout_price` itself: [1](#0-0) 

If `sellout_price == 0`, `price = 1/10 * 0 == 0`, the recovery branch triggers, and the function returns `sellout_price` (still `0`) for both `end_price` and `target_price`. Since `sale_price` in `purchase_core` sets `sale.sellout_price = Some(price)` for any purchase price (including `0`) up to `ideal_cores_sold`, and `sale_price` itself is `leadin_factor_at(through) * sale.end_price`, an `end_price` of `0` guarantees `sale_price` is `0` for the entire following sale: [2](#0-1) 

That `0` `sellout_price` then feeds `rotate_sale`'s call to `adapt_price` on the next rotation, reproducing `0 -> 0` with no additive/lower-bound floor to escape the fixed point: [3](#0-2) [4](#0-3) 

The zero `target_price` is also stored directly as the renewal price for expiring leases: [5](#0-4) 

The only mitigation is the optional `MinimumPrice<Balance, MinPrice>` wrapper, which clamps `end_price` up to `MinPrice::get()`, but this is not enforced by `CenterTargetPrice` itself: [6](#0-5) 

The pallet's own test suite explicitly exercises a `MinPrice = 0` configuration (`ConstU64<0>`) including the `(Some(0), 10)` performance case, confirming this is a supported and untreated configuration path within the pallet's code, not merely a hypothetical: [7](#0-6) 

## Impact Explanation
Once `sellout_price` reaches `0` in a live sale (e.g. via a core purchased at price `0`, which the `purchase_core` code path fully permits and records without any nonzero check), `CenterTargetPrice::adapt_price` locks `end_price`/`target_price` at `0` permanently for that core-sale timeline, and every renewal computed from `target_price` inherits the same `0` price. This is a public underpriced-work condition: coretime cores that should require DOT/KSM payment become permanently free, degrading the economic security of coretime allocation with no recovery path absent operator intervention (e.g., manual governance action to change the price adapter or floor), matching the allowed "public underpriced work" impact class.

## Likelihood Explanation
This requires `sellout_price` to become exactly `0` in a real sale and the runtime to use a `MinPrice` of `0` (or use `CenterTargetPrice` un-wrapped). This is a function of runtime configuration choice combined with market conditions (a very low starting price plus purchases at that price); it does not require any privileged actor, only ordinary public purchase extrinsics. Whether any live/production coretime runtime (e.g., Coretime Polkadot/Kusama) actually configures `MinPrice = 0` was not confirmed within available context — the un-guarded configuration is only shown to be supported by the pallet's type system and its own tests, not proven to be the active production configuration.

## Recommendation
In `CenterTargetPrice::adapt_price`, treat `sellout_price == 0` the same as the "computed price rounds to zero" case by substituting a fixed nonzero minimum (e.g., `Balance::one()`) rather than echoing back the zero `sellout_price`, so the multiplicative recurrence cannot enter a permanent zero fixed point. Longer term, fold a mandatory nonzero floor into `CenterTargetPrice`/`AdaptPrice` itself instead of relying on the optional `MinimumPrice` wrapper being configured correctly by every runtime integrator.

## Proof of Concept
Using the pallet's test harness in `substrate/frame/broker/src/adapt_price.rs`:
```rust
#[test]
fn price_locks_at_zero_when_sellout_price_is_zero() {
    let mut performance = SalePerformance::new(Some(0), 10);
    for _ in 0..5 {
        let prices = CenterTargetPrice::adapt_price(performance);
        assert_eq!(prices.end_price, 0);
        assert_eq!(prices.target_price, 0);
        performance.sellout_price = Some(prices.end_price); // stays 0
        performance.end_price = prices.end_price;
    }
}
```
Chained through `rotate_sale` in `substrate/frame/broker/src/tick_impls.rs`, this zero `end_price`/`target_price` is written into `SaleInfoRecord` and `PotentialRenewalRecord::price`, so every subsequent sale and renewal is priced at zero unless the runtime wraps the adapter in `MinimumPrice` with a nonzero floor.

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

**File:** substrate/frame/broker/src/utility_impls.rs (L62-91)
```rust
	pub fn sale_price(sale: &SaleInfoRecordOf<T>, now: RelayBlockNumberOf<T>) -> BalanceOf<T> {
		let num = now.saturating_sub(sale.sale_start).min(sale.leadin_length).saturated_into();
		let through = FixedU64::from_rational(num, sale.leadin_length.saturated_into());
		T::PriceAdapter::leadin_factor_at(through).saturating_mul_int(sale.end_price)
	}

	pub(crate) fn charge(who: &T::AccountId, amount: BalanceOf<T>) -> DispatchResult {
		let credit = T::Currency::withdraw(&who, amount, Exact, Expendable, Polite)?;
		T::OnRevenue::on_unbalanced(credit);
		Ok(())
	}

	/// Buy a core at the specified price (price is to be determined by the caller).
	///
	/// Note: It is the responsibility of the caller to write back the changed `SaleInfoRecordOf` to
	/// storage.
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

**File:** substrate/frame/broker/src/tick_impls.rs (L228-241)
```rust
				// last time for this one - make it renewable in the next sale.
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
