## Title
`renew` extrinsic in pallet-broker charges the actual current-block core price with no caller-supplied price cap - ([File: substrate/frame/broker/src/dispatchable_impls.rs])

### Summary
`pallet-broker`'s `purchase` extrinsic requires the caller to pass a `price_limit` so the buyer's own transaction can never overpay beyond what they authorized [1](#0-0) . The sibling `renew` extrinsic, which also settles at a price computed from the *current* on-chain sale state at execution time, takes **no price parameter at all** [2](#0-1) . The actual amount charged is computed inside `do_renew` from live storage (`sale_price(&sale, now)`) capped only by a pallet-computed `price_cap`, not by anything the signer specified [3](#0-2) .

### Finding Description
`do_purchase` explicitly protects the buyer: it reads the current `sale_price`, and rejects the call if it exceeds the caller-provided `price_limit`:
```
let price = Self::sale_price(&sale, now);
ensure!(price_limit >= price, Error::<T>::Overpriced);
``` [4](#0-3) 

`do_renew`, however, computes the amount to charge from two moving, chain-state-dependent quantities and never checks it against anything the caller specified:
```
let price_cap = cmp::max(record.price + config.renewal_bump * record.price, end_price);
let now = RCBlockNumberProviderOf::<T::Coretime>::current_block_number();
let price = Self::sale_price(&sale, now).min(price_cap);
``` [5](#0-4) 

`price_cap` is derived from `record.price` (the previous cycle's renewal price) and `config.renewal_bump`/`end_price`, both of which are governance/administratively controlled and can legitimately shift between the time a user observes the expected renewal price off-chain and the time their `renew(core)` transaction actually executes (e.g., due to network congestion, mempool delay, or a new sale being `rotate_sale`d in the interim, which updates `SaleInfo`, `end_price`, and `sale_start`). Because `renew` supplies zero user-chosen ceiling, the signer has no way to bound what they will actually pay for this dispatch call — exactly the "price may be unpredictable" defect described in the external report, but here it is a structural gap in the public extrinsic's parameter surface rather than a pure front-running issue: the `purchase` call in the very same pallet demonstrates that the intended design is to let the caller supply a hard limit, and `renew` simply omits it.

### Impact Explanation
A user who calls `renew` can be charged up to `price_cap` (which can be substantially higher than the price at broadcast time, bounded only by `record.price * (1 + renewal_bump)` or `end_price`, whichever is larger) with `T::Currency::withdraw(...)` executed unconditionally inside `purchase_core`/`charge` [6](#0-5) . Since renewals are commonly automated/pre-signed (parachain teams pre-authorize renewal transactions to keep their core), a delayed inclusion or a sale rotation between signing and inclusion can cause the payer to be debited a materially larger amount than expected, with no on-chain mechanism to reject the overcharge. This directly affects fund conservation/settlement correctness for a public, unprivileged entry point (`renew` is callable by any signed account holding the core's renewal record).

### Likelihood Explanation
The conditions needed are ordinary: transaction propagation delay, block congestion, or a sale rotation (`rotate_sale`, triggered automatically on `on_initialize`) occurring between when a user (or their tooling) computes an expected renewal price and when the `renew` extrinsic is actually included. No malicious peer, validator, collator, or governance actor is required — an ordinary user's own transaction can settle unpredictably higher than intended purely due to normal chain timing, unlike `purchase`, which is explicitly guarded against this by design.

### Recommendation
Add a `price_limit: BalanceOf<T>` parameter to the `renew` extrinsic (mirroring `purchase`), thread it into `do_renew`, and add `ensure!(price_limit >= price, Error::<T>::Overpriced)` before charging, consistent with the existing pattern in `do_purchase`.

### Proof of Concept
1. Pre-authorize/sign a `renew(core)` transaction while `SaleInfo::sale_price(&sale, now)` is low (e.g., just after `rotate_sale`, still within/near leadin).
2. Delay inclusion (congestion, or wait until near `sale.region_end`/next `rotate_sale`) so that, by the time the transaction executes, `sale_price` has risen toward `price_cap = max(record.price*(1+renewal_bump), end_price)`.
3. Observe `do_renew` charges the higher `price` with no rejection, since `renew` never receives or checks any caller-supplied bound (`substrate/frame/broker/src/dispatchable_impls.rs:207-219`), whereas the same scenario for `purchase` would be rejected via `Error::<T>::Overpriced` if `price_limit` were exceeded.

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

**File:** substrate/frame/broker/src/dispatchable_impls.rs (L157-160)
```rust
		let now = RCBlockNumberProviderOf::<T::Coretime>::current_block_number();
		ensure!(now > sale.sale_start, Error::<T>::TooEarly);
		let price = Self::sale_price(&sale, now);
		ensure!(price_limit >= price, Error::<T>::Overpriced);
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

**File:** substrate/frame/broker/src/utility_impls.rs (L68-91)
```rust
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
