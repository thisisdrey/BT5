### Title
`pallet-broker::configure()` Allows Live Sale-Critical Parameters to Be Changed Mid-Sale, Corrupting Renewal Pricing - (File: `substrate/frame/broker/src/dispatchable_impls.rs`)

### Summary
The Coretime `pallet-broker` has the same broken invariant as the external report: a privileged extrinsic can rewrite parameters that govern an *already-running* sale, and a downstream instruction recomputes cost dynamically off that mutable state at execution time, so a party interacting with the sale (here, a core renewer) can be charged a materially different price than the one implied by the sale state they observed.

### Finding Description
`configure()` is the pallet's `update_sale`-equivalent. It is guarded only by `T::AdminOrigin::ensure_origin_or_root` and unconditionally overwrites the entire `Configuration<T>` record with no check on whether a Bulk Coretime sale is currently active: [1](#0-0) 

```
pub(crate) fn do_configure(config: ConfigRecordOf<T>) -> DispatchResult {
    config.validate().map_err(|()| Error::<T>::InvalidConfig)?;
    Configuration::<T>::put(config);
    Ok(())
}
``` [2](#0-1) 

There is no equivalent of the fix the client applied in the report (`ensure!(!sale.is_start_time_reached())`) — `do_configure` never checks `SaleInfo<T>` or `Status<T>` before mutating `Configuration<T>`.

Most sale-critical pricing fields (`leadin_length`, `end_price`, `region_begin`/`region_end`) are safely snapshotted into `SaleInfoRecord` at `rotate_sale` time, so `do_purchase` is not directly affected. However, `do_renew` reads the mutable `Configuration<T>` **live**, at the moment of renewal execution, to compute the renewal price cap: [3](#0-2) 

```
pub(crate) fn do_renew(who: T::AccountId, core: CoreIndex) -> Result<CoreIndex, DispatchError> {
    let config = Configuration::<T>::get().ok_or(Error::<T>::Uninitialized)?;
    ...
    let price_cap = cmp::max(record.price + config.renewal_bump * record.price, end_price);
    let now = RCBlockNumberProviderOf::<T::Coretime>::current_block_number();
    let price = Self::sale_price(&sale, now).min(price_cap);
```

`config.renewal_bump` is read from `Configuration<T>` inline, not from an immutable snapshot captured when the renewal became eligible (i.e., when `PotentialRenewals` was populated in `do_assign`). Because `configure()` can be called at any block — including mid-sale, between the time a core owner is told "renew at up to X% bump" and the block their `renew` extrinsic executes — an `AdminOrigin` update to `renewal_bump` (or other config fields consumed live elsewhere) changes the economic terms of a renewal that a user believed were fixed by the sale/renewal record they observed.

This is the exact shape of the reported bug: a privileged actor can update parameters (`Configuration`) that a public, unprivileged instruction (`renew`) later reads dynamically to compute a cost, with no lock preventing the change while the sale/renewal window is open.

### Impact Explanation
A user relying on the `PotentialRenewalRecord.price` and the currently configured `renewal_bump` to plan a renewal can end up paying a price outside their expectations, since the cap (`price_cap`) is recomputed from live `Configuration` at execution time rather than pinned when the renewal opportunity was created. This degrades pricing predictability/fairness guarantees of the Coretime sale mechanism, the same class of harm the external report flags as unacceptable for buyers.

### Likelihood Explanation
`configure()` is callable at any time by `T::AdminOrigin` (which on production runtimes such as Coretime chains resolves to a governance-controlled origin, but is not gated on sale state at all) — there is no `ensure!` blocking it while `SaleInfo` indicates an active sale, unlike the analogous fix applied for the reported bug. Any legitimate governance configuration update timed during an active sale/renewal window will silently affect pending renewals, with no explicit signal to affected users.

### Recommendation
Mirror the fix applied to `update_sale`: add a check in `do_configure` (or in the `renewal_bump`/other live-read config fields) that either (a) rejects configuration changes to sale/renewal-affecting fields while a sale is active (`SaleInfo::<T>::get()` present and not yet ended), or (b) snapshots `renewal_bump` (and any other config value consumed live by `do_renew`/other purchase-path code) into the `PotentialRenewalRecord` at the time the renewal becomes eligible, so later configuration changes cannot retroactively alter already-quoted pricing terms.

### Proof of Concept
1. A core's workload completes; `do_assign` inserts a `PotentialRenewalRecord{ price, completion: Complete(..) }` in `PotentialRenewals`, computed using the `renewal_bump` in effect at that time.
2. Owner observes the sale/renewal state and expects a renewal price bounded by `record.price + renewal_bump_old * record.price`.
3. Before the owner submits `renew`, `T::AdminOrigin` calls `configure()` with a new `ConfigRecord` containing a higher `renewal_bump`.
4. Owner calls `renew(core)`; `do_renew` reads the now-updated `Configuration::<T>::get().renewal_bump`, producing a higher `price_cap` than what was implied when the renewal became available, and the owner is charged accordingly. [4](#0-3)

### Citations

**File:** substrate/frame/broker/src/lib.rs (L628-640)
```rust
		/// Configure the pallet.
		///
		/// - `origin`: Must be Root or pass `AdminOrigin`.
		/// - `config`: The configuration for this pallet.
		#[pallet::call_index(0)]
		pub fn configure(
			origin: OriginFor<T>,
			config: ConfigRecordOf<T>,
		) -> DispatchResultWithPostInfo {
			T::AdminOrigin::ensure_origin_or_root(origin)?;
			Self::do_configure(config)?;
			Ok(Pays::No.into())
		}
```

**File:** substrate/frame/broker/src/dispatchable_impls.rs (L30-34)
```rust
	pub(crate) fn do_configure(config: ConfigRecordOf<T>) -> DispatchResult {
		config.validate().map_err(|()| Error::<T>::InvalidConfig)?;
		Configuration::<T>::put(config);
		Ok(())
	}
```

**File:** substrate/frame/broker/src/dispatchable_impls.rs (L180-212)
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
```
