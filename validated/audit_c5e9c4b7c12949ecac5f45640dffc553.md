All code paths described in the claim are confirmed accurate against the repository.

Audit Report

## Title
Renewals pollute `sellout_price`/`cores_sold` used for next-period price discovery in `pallet-broker` - (File: `substrate/frame/broker/src/utility_impls.rs`)

## Summary
`pallet-broker`'s `purchase_core` in `substrate/frame/broker/src/utility_impls.rs` L78-91 is shared by both `do_purchase` (market price) and `do_renew` (non-market, capped-bump price) and unconditionally overwrites `sale.sellout_price` with whatever price it is called with while `cores_sold <= ideal_cores_sold`. Since `do_renew` (`substrate/frame/broker/src/dispatchable_impls.rs` L180-193) has no `now > sale.sale_start` gate (unlike `do_purchase` at L158), renewals can execute before any market purchase is even possible, letting non-market renewal prices saturate the `ideal_cores_sold` gate and lock `sellout_price` at a stale value that `rotate_sale` (`tick_impls.rs` L178) feeds directly into `T::PriceAdapter::adapt_price` to set the next sale's `end_price`/`target_price` (`adapt_price.rs` L119-136).

## Finding Description
`purchase_core` is the single choke point for updating `sale.cores_sold` and `sale.sellout_price`:
```rust
pub(crate) fn purchase_core(...) -> Result<CoreIndex, DispatchError> {
    Self::charge(who, price)?;
    let core = sale.first_core.saturating_add(sale.cores_sold);
    sale.cores_sold.saturating_inc();
    if sale.cores_sold <= sale.ideal_cores_sold || sale.sellout_price.is_none() {
        sale.sellout_price = Some(price);
    }
    Ok(core)
}
```
`do_purchase` supplies a true, leadin-decayed market price (`Self::sale_price(&sale, now)`), gated by `ensure!(now > sale.sale_start, Error::<T>::TooEarly)`. `do_renew` supplies a capped-bump, historical price (`record.price`, derived from a prior period, not the current market), and critically has **no** equivalent `now > sale.sale_start` gate — only `ensure_cores_for_sale` — so it can execute as soon as the sale opens, strictly earlier than any legitimate purchase can occur. Because `purchase_core` treats both inputs identically, a handful of renewals submitted at the start of a period can drive `cores_sold` past the (often small) `ideal_cores_sold` threshold while recording a stale, non-market price into `sellout_price`. Once `cores_sold > ideal_cores_sold`, subsequent genuine `do_purchase` calls at the true market price no longer update `sellout_price`. `rotate_sale` then builds `SalePerformance::from_sale(&old_sale)` purely from these fields and passes it to `T::PriceAdapter::adapt_price`, which for `CenterTargetPrice`/`MinimumPrice` derives the *entire* next period's `end_price` and `target_price` from `sellout_price` alone. No existing guard separates renewal-sourced pricing data from market-sourced pricing data at the `purchase_core` level.

## Impact Explanation
This corrupts the `sellout_price` field — the sole basis for computing the next sale's opening/target price for every participant. An account holding renewable cores (a normal permissionless capability, not requiring any privileged role) can bias the next sale's price up or down for all other buyers by choosing when/whether to renew before the sale-open gate that legitimate purchases must wait for. This matches the "public underpriced work that degrades... price acceptance" category in the impact gate: it lets an ordinary user cause the chain to accept/offer coretime at an incorrect, manipulated price rather than the intended market-discovered price.

## Likelihood Explanation
`do_renew` is a normal, permissionless extrinsic (`Call::renew`) available to anyone with an entry in `PotentialRenewals`. It has no `now > sale.sale_start` timing gate unlike `do_purchase`, so renewals are naturally submitted before market purchases become valid. Because `ideal_cores_sold` is typically a small fraction of `cores_offered` (governed by `config.ideal_bulk_proportion`), only a small number of renewals is needed to saturate the gate before any real bid lands, making this readily and repeatably triggerable by normal, expected usage patterns rather than a contrived edge case.

## Recommendation
Decouple renewal-derived pricing contributions from market-purchase-derived ones: either have `do_renew` bypass writes to `sale.sellout_price`/`cores_sold` entirely, or track a separate counter/price field for renewals and feed `AdaptPrice` only with data derived from genuine `do_purchase` transactions.

## Proof of Concept
1. Configure a sale where `ideal_cores_sold` is small (e.g., 2) relative to `cores_offered` (e.g., 50), via `config.ideal_bulk_proportion`.
2. Ensure two cores have entries in `PotentialRenewals` with a low/stale `record.price` from a prior low-demand period.
3. Immediately after the new sale opens (before `now > sale.sale_start` becomes true, i.e., before `do_purchase` is even callable), submit two `Call::renew` extrinsics for those cores. Each call runs `do_renew` → `purchase_core`, incrementing `cores_sold` to 1 then 2 and setting `sale.sellout_price = Some(record.price)` both times since `cores_sold <= ideal_cores_sold`.
4. Once purchases become valid, genuine `Call::purchase` calls at the true market leadin price no longer update `sellout_price` since `cores_sold` (>2) exceeds `ideal_cores_sold`.
5. At `rotate_sale`, `SalePerformance::from_sale(&old_sale).sellout_price` equals the stale renewal price, and `T::PriceAdapter::adapt_price` computes the next sale's `end_price`/`target_price` from this value instead of the true market-clearing price — verifiable via a unit test asserting `SaleInfo::<T>::get().sellout_price` after step 3 equals `record.price`, followed by a purchase in step 4 that does not change `sellout_price`. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

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

**File:** substrate/frame/broker/src/dispatchable_impls.rs (L149-162)
```rust
	pub(crate) fn do_purchase(
		who: T::AccountId,
		price_limit: BalanceOf<T>,
	) -> Result<RegionId, DispatchError> {
		let status = Status::<T>::get().ok_or(Error::<T>::Uninitialized)?;
		let mut sale = SaleInfo::<T>::get().ok_or(Error::<T>::NoSales)?;
		Self::ensure_cores_for_sale(&status, &sale)?;

		let now = RCBlockNumberProviderOf::<T::Coretime>::current_block_number();
		ensure!(now > sale.sale_start, Error::<T>::TooEarly);
		let price = Self::sale_price(&sale, now);
		ensure!(price_limit >= price, Error::<T>::Overpriced);

		let core = Self::purchase_core(&who, price, &mut sale)?;
```

**File:** substrate/frame/broker/src/dispatchable_impls.rs (L180-228)
```rust
	pub(crate) fn do_renew(who: T::AccountId, core: CoreIndex) -> Result<CoreIndex, DispatchError> {
		let config = Configuration::<T>::get().ok_or(Error::<T>::Uninitialized)?;
		let status = Status::<T>::get().ok_or(Error::<T>::Uninitialized)?;
		let mut sale = SaleInfo::<T>::get().ok_or(Error::<T>::NoSales)?;
		Self::ensure_cores_for_sale(&status, &sale)?;

		let renewal_id = PotentialRenewalId { core, when: sale.region_begin };
		let record = PotentialRenewals::<T>::get(renewal_id).ok_or(Error::<T>::NotAllowed)?;
		let workload =
			record.completion.drain_complete().ok_or(Error::<T>::IncompleteAssignment)?;

		let old_core = core;

		let core = Self::purchase_core(&who, record.price, &mut sale)?;

		Self::deposit_event(Event::Renewed {
			who,
			old_core,
			core,
			price: record.price,
			begin: sale.region_begin,
			duration: sale.region_end.saturating_sub(sale.region_begin),
			workload: workload.clone(),
		});

		Workplan::<T>::insert((sale.region_begin, core), &workload);

		let begin = sale.region_end;
		let end_price = sale.end_price;
		// Renewals should never be priced lower than the current `end_price`:
		let price_cap = cmp::max(record.price + config.renewal_bump * record.price, end_price);
		let now = RCBlockNumberProviderOf::<T::Coretime>::current_block_number();
		let price = Self::sale_price(&sale, now).min(price_cap);
		log::debug!(
			"Renew with: sale price: {:?}, price cap: {:?}, old price: {:?}",
			price,
			price_cap,
			record.price
		);
		let new_record = PotentialRenewalRecord { price, completion: Complete(workload) };
		PotentialRenewals::<T>::remove(renewal_id);
		PotentialRenewals::<T>::insert(PotentialRenewalId { core, when: begin }, &new_record);
		SaleInfo::<T>::put(&sale);
		if let Some(workload) = new_record.completion.drain_complete() {
			log::debug!("Recording renewable price for next run: {:?}", price);
			Self::deposit_event(Event::Renewable { core, price, begin, workload });
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
