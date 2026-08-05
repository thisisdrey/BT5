Audit Report

## Title
`renew()` in `pallet-broker` charges the caller with no user-supplied price ceiling, unlike `purchase()` - (File: `substrate/frame/broker/src/dispatchable_impls.rs`)

## Summary
`do_purchase` requires the caller to supply `price_limit` and enforces `ensure!(price_limit >= price, Error::<T>::Overpriced)` before charging the caller [1](#0-0) . `do_renew`, which triggers the same `purchase_core`/`charge` value-transfer path, takes only a `core` index and unconditionally charges `record.price` with no caller-supplied bound and no `Overpriced`-style guard [2](#0-1) . The charged `record.price` is itself derived from mutable, block-height-dependent state (`sale_price`, `end_price`, `renewal_bump`) computed after the transfer, so the amount a caller pays for a renewal cannot be capped by the caller at call time [3](#0-2) .

## Finding Description
`do_renew` reads `PotentialRenewals::<T>::get(renewal_id)` to obtain `record.price`, then calls `Self::purchase_core(&who, record.price, &mut sale)`, which unconditionally withdraws `record.price` from the caller via `Self::charge` [4](#0-3) . Unlike `do_purchase`, there is no parameter through which the caller can express a maximum acceptable price, and no `ensure!` check comparing the charge to any caller input.

`record.price` for the *next* renewal is computed at the end of the current `do_renew` call from `price_cap = max(record.price + renewal_bump * record.price, end_price)` and `price = sale_price(&sale, now).min(price_cap)` [3](#0-2) . Both `end_price` (set by `AdaptPrice`/`rotate_sale`) and `sale_price(&sale, now)` (dependent on relay-chain block number relative to `sale_start`/`leadin_length`) are global mutable state that can change between when a user observes the cached price off-chain and when their `renew` extrinsic actually executes. `do_purchase`'s `price_limit` mechanism exists specifically to guard against this class of state drift for `purchase`, but the mirror-image `renew` entry point omits it entirely.

The `lib.rs` extrinsic signature for `renew` takes only `core: CoreIndex` with no price-related argument, confirming there is no user-facing way to bound the renewal charge at the call level.

## Impact Explanation
This matches the "runtime bugs that compromise intended behavior" impact category: a normal, unprivileged, signed caller of the public `renew` extrinsic can be charged up to `price_cap`, an amount not agreed to or boundable at call time, whereas the closely related `purchase` extrinsic explicitly protects callers from this exact scenario via `price_limit`/`Error::<T>::Overpriced`. This is an inconsistency in cost correctness for the `renew` fee-charging path.

## Likelihood Explanation
No malicious actor, governance, or admin action is required. It occurs during ordinary chain operation whenever a `renew` transaction is delayed relative to assumptions made off-chain, or when other market participants' `purchase`/`renew` calls shift `sellout_price` and thus `end_price` at the next `rotate_sale`, or when the call lands later in the leadin window and `sale_price` rises. These are all routine conditions (congestion, market activity, timing), not front-running or adversarial manipulation.

## Recommendation
Add a `price_limit: BalanceOf<T>` parameter to the `renew` extrinsic mirroring `purchase`, and enforce `ensure!(price_limit >= price, Error::<T>::Overpriced)` in `do_renew` before calling `purchase_core`, so callers can bound the maximum amount charged for a renewal.

## Proof of Concept
1. A user calls `do_renew` in one sale cycle; the resulting `record.price` for the next cycle is stored in `PotentialRenewals` per `do_renew`'s trailer logic [5](#0-4) .
2. Before the user's next `renew(core)` call executes, other participants' purchases raise `sellout_price`, and a subsequent `rotate_sale`/`AdaptPrice` raises `end_price`.
3. The user submits `renew(core)`; `do_renew` charges `record.price` via `purchase_core` unconditionally [6](#0-5) , and the caller has no `price_limit` argument to cause the call to fail instead of overpaying, unlike `do_purchase`'s `Overpriced` guard [7](#0-6) .

This confirms the missing cost-bound guard on `renew` relative to `purchase` within the scoped `pallet-broker` code.

### Citations

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

**File:** substrate/frame/broker/src/dispatchable_impls.rs (L180-193)
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
```

**File:** substrate/frame/broker/src/dispatchable_impls.rs (L207-221)
```rust
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
```
