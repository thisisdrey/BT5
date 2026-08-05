## Title
`renew()` in `pallet-broker` charges the caller with no user-supplied price ceiling, unlike `purchase()` — (File: `substrate/frame/broker/src/dispatchable_impls.rs`)

## Summary
`pallet-broker::purchase` requires the caller to supply a `price_limit` and enforces `ensure!(price_limit >= price, Error::<T>::Overpriced)` before charging [1](#0-0) . `renew`, the sibling entry point that re-purchases a core for an existing workload, takes only a `core` index and computes/charges `record.price` with **no caller-supplied bound at all** [2](#0-1) . The charged amount, `record.price`, is itself the *previous* renewal's cached price and is only capped after the fact by a formula derived from live, block-height-dependent state (`sale_price`, `end_price`, `renewal_bump`) [3](#0-2) . This is the same broken invariant as the `compensate()` finding: a public, unprivileged, fee-paying call whose charged amount is derived from state that can shift between when the user decides to call and when the call executes, with no “do not charge more than X” parameter to bound it.

## Finding Description
`do_renew` reads `PotentialRenewals::<T>::get(renewal_id)` to obtain `record.price` — the amount to charge *for this renewal* [4](#0-3) . It calls `Self::purchase_core(&who, record.price, &mut sale)`, which unconditionally withdraws `record.price` from the caller via `Self::charge` [5](#0-4) . Unlike `purchase`, there is no parameter through which the extrinsic caller can express "the maximum I am willing to pay."

`record.price` is not a static, user-agreed number: it is set at the *end* of the previous call to `do_renew` (or initial `do_purchase`) using:
```
price_cap = max(record.price + renewal_bump * record.price, end_price)
price = sale_price(&sale, now).min(price_cap)
``` [6](#0-5) 

Both `end_price` and `sale_price(&sale, now)` are mutable global state: `end_price` is set by the price-adaptation mechanism at the start of each sale rotation (`rotate_sale`/`AdaptPrice`), and `sale_price` depends on the current relay-chain block number relative to `sale.sale_start`/`leadin_length` [7](#0-6) . Consequently the exact `record.price` that will be charged in a *future* `renew` call is not knowable with certainty by the caller ahead of time — it depends on what other market participants do (their own `purchase`/`renew` calls affect `sellout_price`, which feeds `AdaptPrice`) and on the block at which the extrinsic actually executes relative to the leadin window.

Any ordinary user flow in which:
1. the caller queries the current `PotentialRenewals` price off-chain,
2. submits `renew(core)` expecting to pay that price,
3. but the transaction is included one or more sale rotations later than expected, or after another cheaper/renewal-boosting purchase moved `sellout_price`/`end_price`,

results in the caller being charged up to `price_cap` — a higher amount than anticipated — with zero opportunity in the extrinsic itself to revert instead of overpaying. `Self::sale_price(&sale, now)` explicitly grows with elapsed blocks in the leadin period, so simple confirmation delay alone changes the charge.

This mirrors the core-4-06 `compensate()` bug exactly: `purchase` has the "slippage protection" (`price_limit`), but the closely related `renew` extrinsic — which triggers exactly the same value-transfer/fee-charging code path (`purchase_core`) — omits it entirely, even though the amount charged by `renew` is computed from the same kind of time/market-dependent state that `purchase`'s `price_limit` was designed to guard against.

## Impact Explanation
This falls under "public underpriced work that degrades block production" adjacent territory but more precisely under "runtime bugs that compromise intended behavior" / unauthorized loss of value without user consent: a normal, unprivileged user calling a documented public extrinsic (`renew`) can be charged an amount they never agreed to, up to `price_cap`, which can be materially larger than the price they observed off-chain when constructing the call. Because `purchase` already implements a `price_limit` for exactly this class of state-dependent charge, its absence on `renew` is an inconsistency that directly reproduces the acknowledged bug class from the external report: no way to bound/refuse an unexpectedly larger charge in a call whose price is derived from state that mutates between submission and execution.

## Likelihood Explanation
This requires no malicious actor, governance, or admin — it occurs under normal operation any time:
- a `renew` transaction is delayed in the mempool/block inclusion past assumptions made when it was crafted, or
- other market participants purchase/renew cores in the interim (changing `sellout_price` and therefore `end_price` at the next `rotate_sale`), or
- the call lands later in the leadin window than expected, increasing `sale_price`.

All of these are ordinary chain-usage conditions (congestion, market activity, timing), matching the report's characterization of "can occur during normal interactions" (Medium severity) rather than requiring adversarial front-running.

## Recommendation
Add a `price_limit: BalanceOf<T>` parameter to the `renew` extrinsic (mirroring `purchase`), and enforce `ensure!(price_limit >= price, Error::<T>::Overpriced)` in `do_renew` before calling `purchase_core`, so the caller can bound the maximum they are willing to pay for a renewal, exactly as they can for a purchase.

## Proof of Concept
Conceptual trace based on `substrate/frame/broker/src/dispatchable_impls.rs`:
1. User calls `Broker::do_renew` earlier in the sale cycle and observes off-chain a projected `record.price` for the next renewal cycle (stored in `PotentialRenewals`).
2. Before the user's next `renew(core)` extrinsic is included, other participants purchase cores at higher prices (raising `sellout_price`), and `rotate_sale` runs `AdaptPrice`, raising `end_price` [8](#0-7) .
3. The user's `renew(core)` extrinsic (no price field, per `substrate/frame/broker/src/lib.rs:733-738`) executes; `do_renew` computes `price = sale_price(&sale, now).min(price_cap)` where `price_cap = max(record.price*(1+renewal_bump), end_price)`. Because `end_price` rose, `price` is charged at a level higher than what the user expected when they decided to call `renew`.
4. `purchase_core` unconditionally withdraws this higher `price` from the user (`substrate/frame/broker/src/utility_impls.rs:68-91`) — there is no extrinsic parameter that could have caused the call to fail/revert instead of overcharging, unlike `purchase`'s `Error::<T>::Overpriced` guard.

This demonstrates the same "no minimum/maximum bound parameter → unexpected charge in normal flow" defect described in the external report, localized to `pallet-broker`'s `renew` call path.

### Citations

**File:** substrate/frame/broker/src/dispatchable_impls.rs (L149-160)
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

**File:** substrate/frame/broker/src/dispatchable_impls.rs (L178-219)
```rust
	/// Must be called on a core in `PotentialRenewals` whose value is a timeslice equal to the
	/// current sale status's `region_end`.
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

**File:** substrate/frame/broker/src/utility_impls.rs (L62-66)
```rust
	pub fn sale_price(sale: &SaleInfoRecordOf<T>, now: RelayBlockNumberOf<T>) -> BalanceOf<T> {
		let num = now.saturating_sub(sale.sale_start).min(sale.leadin_length).saturated_into();
		let through = FixedU64::from_rational(num, sale.leadin_length.saturated_into());
		T::PriceAdapter::leadin_factor_at(through).saturating_mul_int(sale.end_price)
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
