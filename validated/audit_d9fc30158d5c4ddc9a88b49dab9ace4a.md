## Title
`pallet-broker::renew` charges an uncapped, execution-time-computed price with no caller-supplied price ceiling, unlike `purchase` - (File: `substrate/frame/broker/src/dispatchable_impls.rs`)

### Summary
`Pallet::renew` (via `Call::renew`, dispatched to `Pallet::do_renew`) charges the caller a coretime renewal price that is **recomputed at execution time** from mutable chain state (`SaleInfo`, current block/timeslice), with **no user-supplied maximum price parameter**. This mirrors the core broken invariant in the referenced report: an amount a caller expects to pay is computed against a value that can shift between the time the caller decides to act and the time the extrinsic actually executes, and the caller has no on-chain mechanism to cap what they end up paying. `Call::purchase`, which performs the analogous "buy a core" operation, explicitly protects the caller with a `price_limit` parameter (`Error::Overpriced` guard); `renew` has no equivalent guard.

### Finding Description
`Call::purchase` is defined with an explicit slippage/price-cap parameter: [1](#0-0) 

and `do_purchase` enforces it: [2](#0-1) 

`Call::renew`, by contrast, takes only `core` — no price bound at all: [3](#0-2) 

`do_renew` computes the price to charge **at the moment the extrinsic executes**, based on the current sale state and current block number, then immediately withdraws that amount from the caller: [4](#0-3) 

The price is bounded above only by `price_cap = max(record.price + renewal_bump * record.price, end_price)` — a protocol-level bound, not a caller-specified one. `record.price` is the price that was fixed and advertised to the (future) renewer when their slot became `Renewable` (e.g. via the `Event::Renewable` emission, or by querying `PotentialRenewals`). Between that point and the actual submission/inclusion of the `renew` extrinsic, `sale.end_price`/`sale_price()` can rise due to normal market activity (other purchases, `rotate_sale` re-pricing via `AdaptPrice`), so the caller can be charged materially more than the price they observed and budgeted for — up to `price_cap`, i.e. up to `record.price * (1 + renewal_bump)`.

This is the direct structural analog of the ECG finding: in both cases a "settle now, pay a previously-quoted amount" operation instead recomputes the amount to be pulled from the caller using **live mutable state at execution time**, and the caller has no way to bound the recomputed amount. In the ECG case the mutable state was `creditMultiplier`; here it is `SaleInfo`/`sale_price()`. In the ECG case the mitigation proposed and implicitly acknowledged by the analogous, already-fixed sibling function (`purchase`, which has `price_limit`) shows the pattern the protocol itself considers correct — `renew` simply omits it.

### Impact Explanation
An unprivileged, ordinary signed caller invoking `renew` for their own core can be forced to pay up to `renewal_bump` (a configurable percentage, e.g. 10% in tests) more than the price advertised when their renewal became due, with zero on-chain recourse: the call either succeeds at whatever price is computed (silently overcharging relative to expectation) or, if the caller's balance is insufficient for the higher price, the withdrawal fails and the extrinsic errors (forcing wasted fees and re-submission, at which point the price may have risen again). There is no `Error::Overpriced`-style rejection and no way to express "abort if price > X" for `renew`, unlike `purchase`. This directly reproduces "repayer/beneficiary is forced to pay more than the true/expected value because the value is recomputed against drifted mutable state, with no cap" — the exact broken invariant flagged in the source report, now expressed as coretime renewers being overcharged relative to their expectations, with real ROC/DOT value at stake and no user-side protection mechanism, unlike the sibling `purchase` call.

### Likelihood Explanation
High likelihood of occurrence in normal operation (no attacker or privileged actor required): any renewer submitting a transaction during a period of price movement (leadin ramp-up, congestion causing delayed inclusion, or another buyer's `purchase`/`renew` shifting `SaleInfo`) will experience this. It requires no malicious peer, validator, collator, or governance action — purely a consequence of the public `renew` entry point lacking the price-limit parameter that its sibling `purchase` entry point has.

### Recommendation
Add a `price_limit: BalanceOf<T>` parameter to `Call::renew` / `do_renew`, mirroring `do_purchase`, and enforce `ensure!(price_limit >= price, Error::<T>::Overpriced)` before calling `Self::purchase_core` in `do_renew`. This lets renewers cap the amount they are willing to pay, restoring parity between `purchase` and `renew` and eliminating the forced-overpayment path.

### Proof of Concept
1. A core's renewal becomes due; `PotentialRenewals` stores `PotentialRenewalRecord { price: record.price, .. }`, and `Event::Renewable { price: record.price, .. }` is emitted — this is the price the owner sees and plans to pay. [5](#0-4) 
2. Owner prepares a `renew(core)` call expecting to pay `record.price`.
3. Before the transaction is included, other market activity (a `purchase`, or `rotate_sale` re-pricing via `AdaptPrice`) raises `sale.end_price` / `sale_price(&sale, now)`.
4. `renew(core)` executes; `do_renew` computes `price = Self::sale_price(&sale, now).min(price_cap)` where `price_cap = max(record.price + renewal_bump * record.price, end_price)`, which can be up to `record.price * (1 + renewal_bump)` — strictly higher than what the owner expected — and unconditionally withdraws that amount via `purchase_core -> charge(who, price)`: [6](#0-5) [7](#0-6) 
5. Unlike `purchase`, which would reject with `Error::Overpriced` if the price exceeds the caller's `price_limit`, `renew` has no such parameter or check, so the extra charge is unconditionally applied. The test `renewals_affect_price` confirms renewal price is expected to increase due to `renewal_bump`/market state between calls, illustrating the exact price drift the caller has no way to bound: [8](#0-7)

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

**File:** substrate/frame/broker/src/dispatchable_impls.rs (L149-161)
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

```

**File:** substrate/frame/broker/src/dispatchable_impls.rs (L205-222)
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
		PotentialRenewals::<T>::remove(renewal_id);
		PotentialRenewals::<T>::insert(PotentialRenewalId { core, when: begin }, &new_record);
		SaleInfo::<T>::put(&sale);
```

**File:** substrate/frame/broker/src/tick_impls.rs (L226-241)
```rust
			let expire = until < region_end;
			if expire {
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

**File:** substrate/frame/broker/src/utility_impls.rs (L68-72)
```rust
	pub(crate) fn charge(who: &T::AccountId, amount: BalanceOf<T>) -> DispatchResult {
		let credit = T::Currency::withdraw(&who, amount, Exact, Expendable, Polite)?;
		T::OnRevenue::on_unbalanced(credit);
		Ok(())
	}
```

**File:** substrate/frame/broker/src/tests.rs (L582-600)
```rust
		let core = Broker::do_renew(1, region.core).unwrap();
		// First renewal has same price as initial purchase.
		let b = b - price;
		assert_eq!(balance(1), b);
		advance_to(51);
		assert_noop!(Broker::do_purchase(1, u64::max_value()), Error::<Test>::SoldOut);
		advance_to(81);
		assert_ok!(Broker::do_renew(1, core));
		// Renewal bump in effect
		let price = price + Perbill::from_percent(10) * price;
		let b = b - price;
		assert_eq!(balance(1), b);

		// Move after interlude and leadin - should reduce price.
		advance_to(159);
		Broker::do_renew(1, region.core).unwrap();
		let price = price + Perbill::from_percent(10) * price;
		let b = b - price;
		assert_eq!(balance(1), b);
```
