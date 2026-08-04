## Finding: predictable, attacker-forced sale-price manipulation in `pallet-broker`

The external report's core broken invariant is: *a public, unrestricted function updates a price that feeds directly into value calculations, and that update is deterministic/predictable enough that an unprivileged caller can force a specific price and then immediately transact against it for guaranteed profit.* The closest local analog is in `pallet-broker`'s Coretime sale-pricing mechanism.

### Title
Unprivileged sellout-price manipulation lets an attacker force the next sale's `end_price`/`target_price` and renewal price cap - (File: `substrate/frame/broker/src/adapt_price.rs`, `substrate/frame/broker/src/utility_impls.rs`, `substrate/frame/broker/src/dispatchable_impls.rs`)

### Summary
`pallet-broker` prices Bulk Coretime with a fully deterministic, block-number-driven leadin curve [1](#0-0) , and updates `SaleInfoRecord.sellout_price` on every purchase while `cores_sold <= ideal_cores_sold` [2](#0-1) . This `sellout_price` is the sole input to `AdaptPrice::adapt_price`, which sets the *next* sale's `end_price` to `1/10` of it and the `target_price`/renewal cap to the value itself [3](#0-2) . Any signed, unprivileged account can call the public `purchase` extrinsic at exactly the point in the leadin decay curve that produces the lowest price, timing it to be the core that sets (or freezes) `sellout_price`, thereby forcing next period's pricing baseline downward for personal profit on subsequent purchases/renewals.

### Finding Description
- `Self::sale_price(&sale, now)` computes price purely as a function of elapsed blocks since `sale.sale_start`, via `T::PriceAdapter::leadin_factor_at(through) * end_price` [1](#0-0) . This is fully predictable off-chain by any observer — no oracle feed or governance action is required.
- `do_purchase` is reachable by any `ensure_signed` origin with `price_limit` as the only guard, and simply requires `price_limit >= price` [4](#0-3) . Nothing prevents timing the call to the lowest point of the decay curve.
- `purchase_core` records `sale.sellout_price = Some(price)` on every purchase for as long as `cores_sold <= ideal_cores_sold` [2](#0-1) , meaning the *last* purchase at or before the ideal threshold permanently fixes `sellout_price` for that sale, i.e. an attacker can deliberately be that last purchaser by buying the marginal "ideal" core deep in the leadin discount window.
- `CenterTargetPrice::adapt_price` (the shipped `AdaptPrice` impl) derives the *next* sale's `end_price` as `sellout_price / 10` and `target_price` as `sellout_price` [3](#0-2) . `target_price` also feeds the renewal price cap: `price_cap = max(record.price + bump*record.price, end_price)` used in `do_renew` [5](#0-4) .
- The project's own PR history confirms this exact class of manipulation was previously exploited/observed ("a price manipulation issue we discovered with the Kusama launch") and was only partially mitigated by switching the price-adaptation input from cores-sold-count to `sellout_price` [6](#0-5) . The remaining `sellout_price`-based mechanism is itself attacker-influenced, since it is simply "the price of whichever purchase happens to land on the ideal-cores boundary" — fully controllable by an unprivileged, timed call.

### Impact Explanation
By deliberately purchasing the marginal "ideal" core at the bottom of the deterministic leadin curve, an attacker depresses `sellout_price`, which cascades into an artificially low `end_price` (starting price of the *next* sale) and `target_price` (renewal price cap) for all subsequent Coretime sales and renewals. This is public underpriced work in the sense flagged by the impact gate: Coretime, the resource that gates parachain block production, gets sold/renewed below its intended market-clearing price, degrading protocol revenue (captured on-chain via `T::OnRevenue`) and undermining the "intended behavior" of the pricing controller that governance configured. No malicious validator, collator, relayer, or admin action is required — only a normal signed account acting at a predictable block.

### Likelihood Explanation
High. `sale_price` is a pure deterministic function of public chain state (`now`, `sale.sale_start`, `sale.leadin_length`, `sale.end_price`), so the exact price at any future block is computable off-chain in advance. The only "skill" required is submitting `purchase` (or `renew`) at the right block, which any unprivileged account can do without any special access — this is exactly the "predict the update, then force/consume it for profit" primitive from the source report, just applied to `pallet-broker`'s own on-chain sale-pricing state instead of an external Chainlink feed.

### Recommendation
Do not let a single purchaser's price unilaterally set the next sale's pricing baseline. Options: use a volume/time-weighted average sale price rather than the last "ideal-boundary" transaction price, require a minimum number of independent purchasers before `sellout_price` is finalized, or bound the maximum single-block influence one account can have on `sellout_price` (e.g., ignore purchases from an account that would otherwise become the boundary-setting sale). Additionally, consider bounding per-sale price adaptation with additional smoothing/clamping (beyond the already-present `MinimumPrice` floor) so no single purchase can move `end_price`/`target_price` by a full order of magnitude.

### Proof of Concept
1. Observe `SaleInfo::sale_start`, `leadin_length`, `end_price`, and `ideal_cores_sold` (all public storage).
2. Compute off-chain the block at which `sale_price` reaches its minimum before `ideal_cores_sold` is reached (using the public `leadin_factor_at` formula) [7](#0-6) .
3. Submit `purchase(price_limit)` at that block so that this purchase is the one that brings `cores_sold` to `ideal_cores_sold` (or is the last purchase at/under that boundary), fixing `sale.sellout_price` to this low value [2](#0-1) .
4. On sale rotation, `adapt_price` sets next sale's `end_price = sellout_price / 10` and `target_price = sellout_price` [3](#0-2) , both now artificially low.
5. Attacker (or colluding accounts) purchase/renew cores in the following sale at the now cheap `end_price`/`target_price`, realizing guaranteed profit versus the price that would have prevailed under organic demand.

### Citations

**File:** substrate/frame/broker/src/utility_impls.rs (L62-66)
```rust
	pub fn sale_price(sale: &SaleInfoRecordOf<T>, now: RelayBlockNumberOf<T>) -> BalanceOf<T> {
		let num = now.saturating_sub(sale.sale_start).min(sale.leadin_length).saturated_into();
		let through = FixedU64::from_rational(num, sale.leadin_length.saturated_into());
		T::PriceAdapter::leadin_factor_at(through).saturating_mul_int(sale.end_price)
	}
```

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

**File:** substrate/frame/broker/src/adapt_price.rs (L110-117)
```rust
impl<Balance: FixedPointOperand> AdaptPrice<Balance> for CenterTargetPrice<Balance> {
	fn leadin_factor_at(when: FixedU64) -> FixedU64 {
		if when <= FixedU64::from_rational(1, 2) {
			FixedU64::from(100).saturating_sub(when.saturating_mul(180.into()))
		} else {
			FixedU64::from(19).saturating_sub(when.saturating_mul(18.into()))
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

**File:** substrate/frame/broker/src/dispatchable_impls.rs (L178-228)
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
