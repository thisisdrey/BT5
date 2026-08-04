## Analysis

The external report's core broken invariant is: **a configuration-setting entry point accepts a struct of timing/percentage parameters and only partially validates them, allowing a single missing bound (a zero-length duration, in particular) to silently corrupt downstream accounting** (sequence timing math / royalty math). The closest verifiable local analog in this repository is `pallet-broker`'s `ConfigRecord::validate`, which is the single gate protecting the Coretime sale configuration used by `configure`, `start_sales`, and `rotate_sale`.

### Title
Incomplete `ConfigRecord::validate` allows a zero `region_length` to produce zero-duration sold Coretime regions - (File: `substrate/frame/broker/src/types.rs`)

### Summary
`ConfigRecord::validate` only rejects a zero `leadin_length`, but performs no check on `region_length`, `interlude_length`, `advance_notice`, or `contribution_timeout`. [1](#0-0) 
`region_length` (a `Timeslice`, a plain integer, not `Perbill`/`NonZero`) flows directly, unchecked, into `region_end = region_begin + config.region_length` used both when bootstrapping the first sale in `do_start_sales` and on every subsequent `rotate_sale`. [2](#0-1) [3](#0-2) 

### Finding Description
`do_configure` stores whatever `ConfigRecord` passes `validate()` with no further sanity checks: [4](#0-3) 

If `region_length == 0` is accepted (it passes `validate` because only `leadin_length` is checked), then in `rotate_sale`:
- `region_begin = old_sale.region_end`
- `region_end = region_begin + config.region_length` → `region_end == region_begin` [3](#0-2) 

The resulting `SaleInfoRecord` is then stored and used by `do_purchase`, which issues a Coretime `Region` with `begin == end` after collecting full payment from the buyer: [5](#0-4) 

The buyer is charged via `Self::charge(who, price)` inside `purchase_core` before the zero-length region is issued, meaning the purchaser pays for a Coretime NFT that grants zero timeslices of assignable core time — `duration = sale.region_end.saturating_sub(sale.region_begin) == 0`. [6](#0-5) [7](#0-6) 

This is the direct structural analog of the external report's missing-timestamp-window checks: a single unvalidated timing field (`region_length`/`sealedAfterTimestamp`-`sealedBeforeTimestamp` window) is used to compute a duration that downstream code assumes is always sane (non-zero, bounded), and its absence silently breaks the accounting of what was paid for versus what was delivered.

### Impact Explanation
A misconfigured or zero `region_length` causes every subsequent sale rotation to offer core Regions with zero effective duration while still charging the full sale price, resulting in permanent fund loss for purchasers relative to what they receive, and desynchronizing `Workplan`/`InstaPoolIo` timeslice bookkeeping that assumes `region_end > region_begin`. This matches the report's "insufficient input validation on a configuration-setting call causing incorrect payout/DoS" class.

### Likelihood Explanation
Low-to-moderate: `configure` requires `T::AdminOrigin::ensure_origin_or_root`, so triggering it needs the same privileged/config-error precondition as the original report ("configuration error… requires a configuration error"), not an unprivileged attacker. The exposure is a genuine gap in `validate()` rather than any malicious relayer/validator assumption.

### Recommendation
Extend `ConfigRecord::validate` to reject `region_length == 0`, `interlude_length` values that would make `sale_start` non-monotonic, and add sane upper/lower bounds for `advance_notice` and `contribution_timeout`, mirroring the recommendation in the source report to bound all user/admin-supplied timing fields, not just one.

### Proof of Concept
1. Call `Broker::configure` with a `ConfigRecord` where `region_length = 0` (and any valid non-zero `leadin_length`); `validate()` passes because only `leadin_length` is checked. [1](#0-0) 
2. Call `Broker::start_sales`; `do_start_sales` computes `region_end = commit_timeslice.saturating_add(config.region_length)`, which equals `commit_timeslice` (`region_begin`) since `region_length == 0`. [8](#0-7) 
3. A user calls `Broker::purchase`; `do_purchase` charges the buyer at `sale_price`, then issues a `Region` with `begin == end`, producing `duration = 0` in the emitted `Purchased` event. [5](#0-4) 

Note: verifying whether this zero-duration region additionally causes a panic/DoS deeper in `Workplan`/`InstaPoolIo` bookkeeping (vs. only fund-loss) would require tracing further call sites and running the pallet's test suite, which was not fully exhaustible within this review; the fund-loss path above is directly supported by the cited code.

### Citations

**File:** substrate/frame/broker/src/types.rs (L285-292)
```rust
	/// Check the config for basic validity constraints.
	pub(crate) fn validate(&self) -> Result<(), ()> {
		if self.leadin_length.is_zero() {
			return Err(());
		}

		Ok(())
	}
```

**File:** substrate/frame/broker/src/dispatchable_impls.rs (L29-34)
```rust
impl<T: Config> Pallet<T> {
	pub(crate) fn do_configure(config: ConfigRecordOf<T>) -> DispatchResult {
		config.validate().map_err(|()| Error::<T>::InvalidConfig)?;
		Configuration::<T>::put(config);
		Ok(())
	}
```

**File:** substrate/frame/broker/src/dispatchable_impls.rs (L120-146)
```rust
		let commit_timeslice = Self::latest_timeslice_ready_to_commit(&config);
		let status = StatusRecord {
			core_count,
			private_pool_size: 0,
			system_pool_size: 0,
			last_committed_timeslice: commit_timeslice.saturating_sub(1),
			last_timeslice: Self::current_timeslice(),
		};
		let now = RCBlockNumberProviderOf::<T::Coretime>::current_block_number();
		// Imaginary old sale for bootstrapping the first actual sale:
		let old_sale = SaleInfoRecord {
			sale_start: now,
			leadin_length: Zero::zero(),
			end_price,
			sellout_price: None,
			region_begin: commit_timeslice,
			region_end: commit_timeslice.saturating_add(config.region_length),
			first_core: 0,
			ideal_cores_sold: 0,
			cores_offered: 0,
			cores_sold: 0,
			sale_index: 0,
		};
		Self::deposit_event(Event::<T>::SalesStarted { price: end_price, core_count });
		Self::rotate_sale(old_sale, &config, &status);
		Status::<T>::put(&status);
		Ok(())
```

**File:** substrate/frame/broker/src/dispatchable_impls.rs (L149-176)
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

		SaleInfo::<T>::put(&sale);
		let id = Self::issue(
			core,
			sale.region_begin,
			CoreMask::complete(),
			sale.region_end,
			Some(who.clone()),
			Some(price),
		);
		let duration = sale.region_end.saturating_sub(sale.region_begin);
		Self::deposit_event(Event::Purchased { who, region_id: id, price, duration });
		Ok(id)
	}
```

**File:** substrate/frame/broker/src/tick_impls.rs (L186-188)
```rust
		// Set workload for the reserved (system, probably) workloads.
		let region_begin = old_sale.region_end;
		let region_end = region_begin + config.region_length;
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
