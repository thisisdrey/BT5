## Analysis

The FraxPoolV3 bug report's core pattern — a public entrypoint that dynamically computes a payment amount from live, mutable state, with no way for the caller to cap what they end up paying — has a direct, provable analog in `pallet-broker`'s Bulk Coretime renewal flow.

### Title
Coretime renewal (`Broker::renew`) has no user-supplied price cap, allowing the caller to be charged an unbounded, live-computed price - (File: `substrate/frame/broker/src/dispatchable_impls.rs`)

### Summary
`pallet-broker` sells "Bulk Coretime" via two public calls: `purchase` and `renew`. `purchase` correctly takes a `price_limit` parameter so the caller reverts rather than overpay if the market price moved. `renew`, however, takes **no price parameter at all** and pays whatever price is computed at execution time, which is derived in part from the *live* value of the current sale's `end_price` — a value that can jump between when the user submits the transaction and when it executes.

### Finding Description
`purchase` is protected: [1](#0-0) 
```
pub fn purchase(origin: OriginFor<T>, price_limit: BalanceOf<T>) -> DispatchResultWithPostInfo {
    ...
    Self::do_purchase(who, price_limit)?;
```
and in `do_purchase`, `ensure!(price_limit >= price, Error::<T>::Overpriced)` bounds the actual payment.

`renew` has no such parameter: [2](#0-1) 
```
pub fn renew(origin: OriginFor<T>, core: CoreIndex) -> DispatchResultWithPostInfo {
    let who = ensure_signed(origin)?;
    Self::do_renew(who, core)?;
```

The price paid on renewal is computed inside `do_renew`: [3](#0-2) 
```
let begin = sale.region_end;
let end_price = sale.end_price;
// Renewals should never be priced lower than the current `end_price`:
let price_cap = cmp::max(record.price + config.renewal_bump * record.price, end_price);
let now = RCBlockNumberProviderOf::<T::Coretime>::current_block_number();
let price = Self::sale_price(&sale, now).min(price_cap);
```

`sale.end_price` is read live from the `SaleInfo` storage item at the moment the extrinsic executes, not fixed when the `PotentialRenewals` record (and its `Event::Renewable` price hint) was created. `sale.end_price` is mutated every sale rotation by `rotate_sale`, and the `AdaptPrice` implementation (`CenterTargetPrice`/`MinimumPrice`) can raise `end_price` substantially between sales based on market demand: [4](#0-3) . The prdoc for the `MinimumPrice`/renewal-bump change explicitly states renewals are "either bumped by renewal bump or set to the `end_price` of the current sale — whatever number is higher": [5](#0-4) . So whenever `end_price` has risen since the user last observed the `Renewable` price, `price_cap` (and therefore the actual charged `price`) rises with it — with the user having no on-chain mechanism to bound the amount they are willing to pay for `renew`, unlike `purchase`.

`Self::charge(who, price)` then unconditionally withdraws exactly that live-computed amount: [6](#0-5) [7](#0-6) .

### Impact Explanation
An unprivileged user calling `renew` (a routine, expected action for anyone holding a renewable Coretime region) can be charged a price they never agreed to and cannot cap, purely because market conditions (sale rotations / demand-driven `end_price` adjustments) changed between transaction submission and inclusion. This is exactly the FraxPoolV3 "unbounded dynamically computed payment" pattern: the recipient (the Broker pallet's revenue account) is correctly credited, but the payer has no `maxPrice`-equivalent guard, so a user can lose funds relative to their intent, silently, with no revert path. This matches the "unbacked/uncapped economic loss via public dispatch wrapper without input limit" impact class.

### Likelihood Explanation
Likelihood is moderate-to-high: no privileged actor, malicious peer, or governance action is required — an ordinary, honest user simply submitting `renew` at a normal time when a sale rotation happens to occur (or when transaction inclusion is delayed) is sufficient to trigger the discrepancy. `purchase` was deliberately given a `price_limit` for exactly this reason, showing the pallet authors recognized the general risk class but did not apply the same fix to `renew`.

### Recommendation
Add a `price_limit: BalanceOf<T>` parameter to the `renew` extrinsic (mirroring `purchase`), thread it into `do_renew`, and `ensure!(price_limit >= price, Error::<T>::Overpriced)` before calling `purchase_core`, so users can bound the amount they are willing to pay for a renewal exactly as they already can for a fresh purchase.

### Proof of Concept
1. A region is renewable; `Event::Renewable` is emitted with `price = P0` (based on `record.price`/`renewal_bump` and the `end_price` at that time).
2. User observes `P0`, expects to pay ~`P0`, and submits `Broker::renew(core)`.
3. Before the extrinsic is included, a new sale rotates (`rotate_sale`/`do_tick`), and `T::PriceAdapter::adapt_price` raises `SaleInfo.end_price` substantially (e.g., due to high sellout demand under `CenterTargetPrice`/`MinimumPrice`).
4. `do_renew` executes: `end_price = sale.end_price` is now much higher; `price_cap = max(record.price + bump*record.price, end_price)` is dominated by the new, higher `end_price`; `price = min(sale_price(now), price_cap)` can equal this elevated cap.
5. `Self::charge(who, price)` withdraws the elevated amount with no way for the user to have prevented it, since `renew` accepts no price ceiling argument, unlike `purchase(origin, price_limit)`.

### Citations

**File:** substrate/frame/broker/src/lib.rs (L713-726)
```rust
		/// Purchase Bulk Coretime in the ongoing Sale.
		///
		/// - `origin`: Must be a Signed origin with at least enough funds to pay the current price
		///   of Bulk Coretime.
		/// - `price_limit`: An amount no more than which should be paid.
		#[pallet::call_index(5)]
		pub fn purchase(
			origin: OriginFor<T>,
			price_limit: BalanceOf<T>,
		) -> DispatchResultWithPostInfo {
			let who = ensure_signed(origin)?;
			Self::do_purchase(who, price_limit)?;
			Ok(Pays::No.into())
		}
```

**File:** substrate/frame/broker/src/lib.rs (L728-738)
```rust
		/// Renew Bulk Coretime in the ongoing Sale or its prior Interlude Period.
		///
		/// - `origin`: Must be a Signed origin with at least enough funds to pay the renewal price
		///   of the core.
		/// - `core`: The core which should be renewed.
		#[pallet::call_index(6)]
		pub fn renew(origin: OriginFor<T>, core: CoreIndex) -> DispatchResultWithPostInfo {
			let who = ensure_signed(origin)?;
			Self::do_renew(who, core)?;
			Ok(Pays::No.into())
		}
```

**File:** substrate/frame/broker/src/dispatchable_impls.rs (L205-219)
```rust
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

**File:** prdoc/stable2506/pr_8630.prdoc (L1-16)
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

**File:** substrate/frame/broker/src/utility_impls.rs (L68-72)
```rust
	pub(crate) fn charge(who: &T::AccountId, amount: BalanceOf<T>) -> DispatchResult {
		let credit = T::Currency::withdraw(&who, amount, Exact, Expendable, Polite)?;
		T::OnRevenue::on_unbalanced(credit);
		Ok(())
	}
```

**File:** substrate/frame/broker/src/utility_impls.rs (L74-91)
```rust
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
