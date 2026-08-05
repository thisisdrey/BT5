## Finding: Renewals pollute `sellout_price`/`cores_sold` used for next-period price discovery in `pallet-broker` [1](#0-0) 

### Title
Coretime renewals corrupt `sellout_price` fed into `AdaptPrice`, letting a caller manipulate the next sale's start/target price - (File: `substrate/frame/broker/src/utility_impls.rs`)

### Summary
`pallet-broker` derives the price for the *next* sale period from the recorded `sellout_price`/`cores_sold` of the *current* sale (`SalePerformance::from_sale`). Both genuine market purchases (`do_purchase`) and lease renewals (`do_renew`) funnel through the same `purchase_core` routine, which unconditionally advances `cores_sold` and overwrites `sale.sellout_price` whenever `cores_sold <= ideal_cores_sold`. Renewal prices are *not* market-discovered prices — they are computed from a capped bump formula (`price_cap`) tied to the renewal's historic price — yet they are recorded as if they were the genuine sellout price for the period. This is directly analogous to the reported NextGen bug: a non-market event (there, an airdrop; here, a renewal) is folded into the "circulating"/"sold" counter that a pricing formula treats as organic demand, skewing the price for subsequent legitimate buyers.

### Finding Description
- `do_purchase` computes `price` from `Self::sale_price(&sale, now)` (true leadin-decayed market price) and calls `Self::purchase_core(&who, price, &mut sale)`. [2](#0-1) 
- `do_renew` computes an entirely different, non-market `price` (capped bump on the historical renewal price, or the current `end_price`, whichever is larger) and *also* calls `Self::purchase_core(&who, record.price, &mut sale)` for the **previous** period's renewal (using the price recorded at the prior rotation), then separately computes and stores a new capped price for the *next* renewal cycle. [3](#0-2) 
- `purchase_core` — shared by both paths — is where the corruption happens: it increments `sale.cores_sold` and overwrites `sale.sellout_price` with whatever `price` it was given, as long as `cores_sold <= ideal_cores_sold` (or no price has been recorded yet):
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
``` [1](#0-0) 
- At the end of the sale period, `rotate_sale` builds `SalePerformance::from_sale(&old_sale)` purely from these polluted `sellout_price`/`cores_sold`/`ideal_cores_sold` fields and feeds it straight into `T::PriceAdapter::adapt_price(...)` to compute the *next* sale's `end_price`/`target_price`: [4](#0-3) [5](#0-4) 
- `CenterTargetPrice::adapt_price` (and its `MinimumPrice` wrapper) reacts solely to `performance.sellout_price`: [6](#0-5) 

Because renewals can be submitted the moment a new sale period opens (they don't wait for market price discovery — `ensure_cores_for_sale` is the only gate, no ordering constraint versus `do_purchase`), a participant holding renewable cores can call `do_renew` for several cores immediately, before any real market purchase lands, and drive `cores_sold` past `ideal_cores_sold` while `sellout_price` is still set to the (stale, non-market) renewal price. Because the `cores_sold <= ideal_cores_sold` gate then blocks further updates from genuine buyers, later market purchases at the true (potentially much higher, or much lower) leadin price never get recorded into `sellout_price`. Note also that `PR 4521` ("AdaptPrice trait is now price controlled") already acknowledged and partially mitigated one price-manipulation vector by switching `AdaptPrice` from raw `cores_sold` counts to `sellout_price` semantics — but it did not separate renewal-derived prices from market-derived prices at the source (`purchase_core`), so the underlying contamination channel remains. [7](#0-6) 

### Impact Explanation
This corrupts the price signal used to open the *next* Coretime sale for every participant on the chain (Rococo/Westend/production Coretime chains configured with `pallet-broker`). An attacker (or even an unintentional actor) with renewable cores can:
- Force `sellout_price` artificially low, causing `adapt_price` to under-price the next sale's cores (`end_price`/`target_price` too low) — enabling the attacker or colluders to acquire coretime cheaply in the next sale, i.e. "public underpriced work" that degrades the network's ability to correctly monetize block space/coretime.
- Or force `sellout_price` artificially high, inflating the next sale's opening price for unrelated public buyers who had no relation to the renewal, unfairly harming ordinary purchasers — mirroring the exact NextGen scenario where whitelisted buyers paid inflated prices due to an unrelated airdrop polluting the price basis.

This does not require any privileged role — any account holding a core eligible for renewal (a normal, permissionless action available to `do_renew` callers) can trigger it, satisfying the "public underpriced work / incorrect price acceptance" impact category without needing an admin, governance, validator, or malicious peer.

### Likelihood Explanation
Renewals are a completely normal, expected user action (`Call::renew`), reachable by anyone owning a renewable core, and nothing in the code enforces that renewal-derived prices be excluded from `sellout_price`/`cores_sold` accounting, nor that renewals occur only after market price discovery. Because `ideal_cores_sold` is often a small fraction of `cores_offered` (`config.ideal_bulk_proportion`), only a handful of renewals at the start of a sale period are needed to saturate the gate and lock in a non-market `sellout_price` before organic purchases occur.

### Recommendation
Track renewal-driven `sellout_price`/`cores_sold` contributions separately from market-purchase-driven ones (e.g., add a distinct counter/field for renewals, or have `do_renew` bypass the `sellout_price` write entirely), and feed `AdaptPrice` only with data derived from genuine `do_purchase` transactions, mirroring the report's suggested mitigation of excluding airdropped/non-market supply from the pricing input.

### Proof of Concept
1. Configure a sale with `ideal_bulk_proportion` small enough that `ideal_cores_sold = 2` for a period offering `cores_offered = 50`.
2. Ensure two cores have pending `PotentialRenewals` entries with a stale/low `record.price` (from a prior low-demand period).
3. As soon as the new sale opens, call `Call::renew` twice for those two cores before any `Call::purchase` transaction is included. Each call routes through `do_renew` → `purchase_core`, which sets `sale.cores_sold = 1`, then `2`, and `sale.sellout_price = Some(record.price)` (the stale renewal price) both times, since `cores_sold <= ideal_cores_sold` (2 <= 2).
4. Subsequent genuine `Call::purchase` calls at the true leadin/market price no longer update `sellout_price` because `cores_sold (>2)` now exceeds `ideal_cores_sold`.
5. At `rotate_sale`, `SalePerformance::from_sale(&old_sale).sellout_price` equals the stale renewal price, not the true market clearing price, and `T::PriceAdapter::adapt_price` computes the next sale's `end_price`/`target_price` from this corrupted value — reproducing the "price skew from a pre-sale event" pattern described in the source report.

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

**File:** substrate/frame/broker/src/dispatchable_impls.rs (L149-163)
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

**File:** substrate/frame/broker/src/dispatchable_impls.rs (L180-227)
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
