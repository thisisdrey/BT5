Confirmed: the `renew` extrinsic in `pallet-broker` has no user-supplied price limit parameter, unlike `purchase`, which explicitly takes `price_limit` and enforces `Error::<T>::Overpriced`.

### Title
`renew()` extrinsic lacks user-specified slippage protection, unlike `purchase()` - (File: `substrate/frame/broker/src/lib.rs`)

### Summary
`pallet-broker`'s `purchase` extrinsic correctly implements slippage protection by accepting a caller-supplied `price_limit` and rejecting execution if the computed sale price exceeds it [1](#0-0) . However, the `renew` extrinsic — which purchases a core at a dynamically calculated price — takes no such parameter, only `core` [2](#0-1) . The caller has no way to bound the price they are willing to pay for a renewal, mirroring the exact bug class in the external report: a bonding-curve/dynamic price is computed at execution time and charged without allowing the caller to set a maximum acceptable price.

### Finding Description
`do_renew()` computes the renewal price as `min(sale_price(&sale, now), price_cap)`, where `price_cap` is derived from the previous renewal price plus a configured `renewal_bump`, or the sale's `end_price`, whichever is greater [3](#0-2) . This price is entirely determined by protocol state (`sale.end_price`, current `sale_price` via the leadin curve, and `record.price` from the last renewal) — none of it is bounded by the caller's own limit. The function then calls `purchase_core`, which unconditionally withdraws that computed price from the caller via `charge()` [4](#0-3) .

Contrast this with `do_purchase`, which explicitly enforces `ensure!(price_limit >= price, Error::<T>::Overpriced)` before charging [5](#0-4) . The dispatchable-level docs for `purchase` even state "`price_limit`: An amount no more than which should be paid" [6](#0-5) , while `renew`'s docs make no such promise [7](#0-6) .

Between the time a user submits `renew` and the time it executes (i.e., is included in a block), other legitimate market activity — concurrent `purchase` calls or other `renew` calls that affect `sellout_price`/`end_price` used in `rotate_sale`'s price adaptation, or simply the natural leadin-curve time decay within `sale_price` — can change the effective price. The `price_cap` only bounds the price relative to the *user's own previous renewal price* and the current sale's `end_price`; it provides no mechanism for the user to reject a renewal because the price moved unfavorably beyond what they are willing to accept right now.

### Impact Explanation
A user calling `renew` can be charged an unexpected — potentially much higher — amount of currency than they anticipated when they submitted the transaction, with no recourse to cap it. This is a direct instance of "public underpriced/overpriced work" and unpredictable settlement affecting core coretime purchase economics, which can materially affect the coretime market's price discovery and cause unintended fund loss for renewal callers (auto-renewal via `do_renew` is also used internally by `rotate_sale`'s auto-renewal processing, amplifying exposure) [8](#0-7) .

### Likelihood Explanation
Any signed account holding a core eligible for renewal can trigger this any time renewal is possible; no privileged actor, governance, or malicious peer/validator is required. The price computation depends only on public on-chain sale state (`SaleInfo`, `Status`, `Configuration`) that changes via ordinary `purchase`/`renew` calls from any other user, and the leadin curve's time-dependence, both of which are within reach of an unprivileged attacker or simply normal market conditions.

### Recommendation
Add a `price_limit: BalanceOf<T>` parameter to the `renew` extrinsic, thread it through `do_renew`, and enforce `ensure!(price_limit >= price, Error::<T>::Overpriced)` before calling `purchase_core`, mirroring the protection already present in `do_purchase`.

### Proof of Concept
1. User A holds a core with a `PotentialRenewalRecord` at `record.price = P`.
2. User A submits `renew(core)`, expecting to pay roughly `P` (or the bumped `price + renewal_bump * price`).
3. Before A's transaction is included, other market participants call `purchase`/`renew` extensively, driving up `sale.end_price` (via `rotate_sale`'s price adapter reacting to high demand) so that when A's transaction lands, `sale_price(&sale, now)` and therefore `price_cap = max(record.price + bump*record.price, end_price)` yields a much higher final price than `record.price + bump*record.price` alone.
4. `do_renew` charges A this higher price with no way for A to have rejected it, unlike `do_purchase` where an equivalent price spike would return `Error::<T>::Overpriced` if `price_limit` were set below the new price [9](#0-8) .

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

**File:** substrate/frame/broker/src/benchmarking.rs (L899-926)
```rust
		// Assume max auto renewals for worst case. This is between 1 and the value of
		// MaxAutoRenewals.
		let n_renewable = T::MaxAutoRenewals::get()
			.min(n.saturating_sub(n_leases).saturating_sub(n_reservations));

		let timeslice_period: u32 = T::TimeslicePeriod::get().try_into().ok().unwrap();
		let sale = SaleInfo::<T>::get().expect("Sale has started.");

		let now = RCBlockNumberProviderOf::<T::Coretime>::current_block_number();
		let price = Broker::<T>::sale_price(&sale, now);
		(0..n_renewable.into()).try_for_each(|indx| -> Result<(), BenchmarkError> {
			let task = 1000 + indx;
			let caller: T::AccountId = T::SovereignAccountOf::maybe_convert(task)
				.expect("Failed to get sovereign account");
			T::Currency::set_balance(
				&caller.clone(),
				T::Currency::minimum_balance()
					.saturating_add(start_price)
					.saturating_add(start_price),
			);

			let region = Broker::<T>::do_purchase(caller.clone(), start_price)
				.expect("Offer not high enough for configuration.");

			Broker::<T>::do_assign(region, None, task, Final)
				.map_err(|_| BenchmarkError::Weightless)?;

			Broker::<T>::do_enable_auto_renew(caller, region.core, task, Some(sale.region_end))?;
```
