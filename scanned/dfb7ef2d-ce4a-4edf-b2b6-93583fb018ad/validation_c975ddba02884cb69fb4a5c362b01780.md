### Title
Coretime sale `sellout_price` can be permanently pinned to a low value by an unprivileged buyer, causing bulk-coretime to be structurally underpriced — ([File: substrate/frame/broker/src/utility_impls.rs])

### Summary
`pallet-broker`'s bulk‑coretime sale pricing controller (`AdaptPrice`, e.g. `CenterTargetPrice`) feeds the *next* sale's price entirely off the current sale's recorded `sellout_price`. That value is not the highest price paid, nor an average — it is simply overwritten by **every purchase** made while `cores_sold <= ideal_cores_sold`. Any unprivileged, signed account can call `purchase` late in the leadin period (when the price has decayed close to `end_price`) to overwrite `sellout_price` with an artificially low number, which then permanently depresses the pricing controller's output for future sales — a direct on-chain analog of the reported auction bug where "no reserve/floor is enforced on the settlement price," letting a single cheap fill push the reference price down with no economic mechanism forcing it back up.

### Finding Description
The core of the pricing feedback loop is in `purchase_core`: [1](#0-0) 

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
```

`sale.sellout_price` is **unconditionally overwritten** by whichever purchase happens to occur while `cores_sold <= ideal_cores_sold` — not the maximum, not a volume-weighted average, just "last write wins." `sale_price` decays continuously through the leadin period: [2](#0-1) 

Any signed account can call the public, unprivileged extrinsic `purchase`/`do_purchase` at any point, paying whatever the currently decayed price is, as long as `price_limit >= price`: [3](#0-2) [4](#0-3) 

At the end of the sale, `rotate_sale` feeds `SalePerformance{ sellout_price, end_price, ... }` straight into `T::PriceAdapter::adapt_price` to compute the *next* sale's `end_price`/`target_price`: [5](#0-4) [6](#0-5) 

`CenterTargetPrice::adapt_price` (the pallet's baseline/default price controller, still used e.g. in `mock.rs` and any runtime not opting into the newer `MinimumPrice` wrapper) computes the next `end_price` as `sellout_price / 10`, with only a "never literally hit zero" guard — no floor tied to actual market value: [7](#0-6) 

Because `sellout_price` is attacker‑controllable (any purchase while `cores_sold <= ideal_cores_sold` overwrites it, regardless of what earlier, higher-value purchases in the same sale paid), an attacker can:
1. Wait for the ideal number of cores to be *almost* sold (`cores_sold == ideal_cores_sold - 1`), which can happen after legitimate high-value buyers have already purchased at a high leadin price.
2. Submit one final unprivileged `purchase` right as `cores_sold` reaches `ideal_cores_sold`, paying only the (much lower) decayed leadin price at that moment.
3. This overwrites `sale.sellout_price` with the attacker's low price, discarding the true, higher market-clearing price that legitimate buyers already paid.
4. `rotate_sale` then divides this manipulated price by 10 to compute the next sale's `end_price`, and the process compounds sale after sale since `CenterTargetPrice` has no mechanism to recover from a persistently low `sellout_price` other than organic high-price purchases, which now start from an artificially crushed floor.

Nothing analogous to an English/Dutch-auction "reserve price" exists here: the settlement price used to drive the entire pricing controller is decided by whichever purchase happens to land at the `ideal_cores_sold` boundary, not by aggregate demand or the highest price achieved — exactly the "best-effort mechanism not guaranteed to reflect true value" flaw called out in the external report.

### Impact Explanation
This directly matches the "public underpriced work that degrades block production" impact bucket: Bulk Coretime *is* block-production capacity being sold by the Coretime chain on behalf of the Polkadot relay chain/system chains. An attacker who can cheaply and repeatedly crash `sellout_price` (and hence `end_price`/`target_price` for all subsequent sales and renewals, since `do_renew`'s `price_cap` is also anchored to `end_price`) causes Coretime — and therefore relay-chain blockspace — to be sold and renewed for a fraction of its intended value indefinitely. This is a protocol-level, unbacked economic loss to the chain's revenue stream (analogous to LP fund loss in the original report), achievable by any unprivileged, signed account with no governance/admin/relayer compromise required.

### Likelihood Explanation
Likelihood is high for chains that still use the plain `CenterTargetPrice` adapter (the pallet's original/default implementation, still shipped and used in tests/mocks) rather than the newer opt-in `MinimumPrice` wrapper. The attack requires only:
- A signed account with funds to buy exactly one core at the current decayed leadin price.
- Timing the purchase to land when `cores_sold` is at or just under `ideal_cores_sold` (observable on-chain via `SaleInfo`).

No collusion, governance action, or privileged origin is needed — it is a pure public-entrypoint manipulation of `do_purchase`.

### Recommendation
- Do not let `sellout_price` be overwritten by low-value purchases once a higher price has already been recorded within the same `cores_sold <= ideal_cores_sold` window; track the maximum (or a volume-weighted) price paid instead of "last write wins."
- Alternatively, always adopt `MinimumPrice` (or an equivalent floor) as the default/mandatory `AdaptPrice` implementation instead of leaving `CenterTargetPrice` as an unguarded option, and additionally bound how far `end_price`/`target_price` can move in a single sale-to-sale transition regardless of a single manipulated sample.

### Proof of Concept
1. Runtime configures `type PriceAdapter = CenterTargetPrice<Balance>` (as in `substrate/frame/broker/src/mock.rs`).
2. Sale starts; several legitimate buyers purchase cores at high leadin prices, bringing `cores_sold` to `ideal_cores_sold - 1`.
3. Attacker (any signed, unprivileged account) waits until later in the leadin period so `sale_price` has decayed to a low value, then calls `Broker::purchase(origin, price_limit = current_low_price)`, incrementing `cores_sold` to `ideal_cores_sold`.
4. Per `purchase_core`, `sale.sellout_price` is overwritten to the attacker's low price (condition `cores_sold <= ideal_cores_sold` still true at this exact purchase).
5. At `rotate_sale`, `CenterTargetPrice::adapt_price` computes `next.end_price = sellout_price / 10` using the attacker's low value, discarding the legitimately higher prices paid by earlier buyers in the same sale.
6. Next sale (and subsequent renewals capped by `end_price`) start from this artificially depressed price, and the attacker can repeat the same timing trick each cycle to keep ratcheting the price down.

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

**File:** substrate/frame/broker/src/lib.rs (L718-726)
```rust
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

**File:** substrate/frame/broker/src/tick_impls.rs (L177-184)
```rust
		// Calculate the start price for the upcoming sale.
		let new_prices = T::PriceAdapter::adapt_price(SalePerformance::from_sale(&old_sale));

		log::debug!(
			"Rotated sale, new prices: {:?}, {:?}",
			new_prices.end_price,
			new_prices.target_price
		);
```

**File:** substrate/frame/broker/src/tick_impls.rs (L253-270)
```rust
		let sale_start = now.saturating_add(config.interlude_length);
		let leadin_length = config.leadin_length;
		let ideal_cores_sold = (config.ideal_bulk_proportion * cores_offered as u32) as u16;
		let sellout_price = if cores_offered > 0 {
			// No core sold -> price was too high -> we have to adjust downwards.
			Some(new_prices.end_price)
		} else {
			None
		};

		let sale_index = old_sale.sale_index.saturating_add(1);

		// Update SaleInfo
		let new_sale = SaleInfoRecord {
			sale_start,
			leadin_length,
			end_price: new_prices.end_price,
			sellout_price,
```

**File:** substrate/frame/broker/src/adapt_price.rs (L119-137)
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
}
```
