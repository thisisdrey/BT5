## Title
Coretime sale pricing relies on a single purchase's price as the "observation" for the next sale/renewal price — analogous to OWAV/TWAV manipulation - (File: `substrate/frame/broker/src/utility_impls.rs`)

### Summary
The Nibbl bug reduces to: a price used to gate an economically significant decision (buyout accept/reject) is derived from a handful of attacker-controllable "observations" instead of a genuine time/volume-weighted average, letting an attacker cheaply steer that price within a short window. The same broken-invariant pattern exists in `pallet-broker`'s coretime sale pricing: the price that seeds the *entire next sale period's* `target_price`/`end_price` (and by extension every renewal price for that period) is set from a **single purchase transaction** (`sale.sellout_price`), not from a volume- or time-weighted average of the sale.

### Finding Description
In `purchase_core`, every time a core is purchased while `cores_sold <= ideal_cores_sold`, the sale's `sellout_price` is unconditionally overwritten with that single purchase's price: [1](#0-0) 

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

This means `sellout_price` is not an average over the sale's cores/duration — it's simply the price of whichever purchase happens to occur exactly when `cores_sold` transitions past `ideal_cores_sold` (or the last one while still under that threshold). At `rotate_sale`, this single value is fed directly into `AdaptPrice::adapt_price` to compute the entire next sale's `end_price` and `target_price`: [2](#0-1) 

`target_price` set here is also what gets locked into `PotentialRenewalRecord.price` for every lease renewal for the whole following region: [3](#0-2) 

And `CenterTargetPrice::adapt_price` amplifies a single sellout observation by 10x for the next `target_price`, and divides it by 10 for the next `end_price`: [4](#0-3) 

Just like the Nibbl TWAV, which was supposed to represent "average valuation over time" but actually reflected the state of a handful of manipulable observations, `sellout_price` is documented/used as "the price at which the market cleared" but is actually a single point sample that any unprivileged buyer controls by choosing *when* (which purchase, at what self-selected `price_limit`) to buy relative to `ideal_cores_sold`. `do_purchase` lets any signed account buy at the current declining leadin price as long as `price_limit >= price`, so an attacker can simply wait until `cores_sold` is one before `ideal_cores_sold` and then submit the purchase that pushes `cores_sold` to exactly `ideal_cores_sold`, at a self-chosen price (bounded only by the current leadin-decayed sale price, which they don't control but which they can time).

The project's own prdocs confirm this class of issue was already identified once (`prdoc/1.13.0/pr_4521.prdoc`, referencing `paritytech/polkadot-sdk#4360`, "fixes a price manipulation issue we discovered with the Kusama launch") and partially mitigated with `MinimumPrice`/`CenterTargetPrice`/renewal floor (`prdoc/stable2503-6/pr_8630.prdoc`). However, those mitigations only clamp the *floor* of the derived price — they do not change the fact that a single purchase transaction remains the sole "observation" driving `target_price` and therefore renewal pricing for an entire subsequent region.

### Impact Explanation
`target_price` computed from a single manipulable purchase feeds the renewal price cap (`price_cap = max(record.price + bump*record.price, end_price)`) used in `do_renew`, and the `end_price`/leadin curve used for every purchase in the following sale. A single well-timed purchase therefore can materially distort pricing (up or down) for an entire subsequent coretime sale/region, degrading intended price-discovery ("runtime bugs that compromise intended behavior" per the impact gate) and potentially causing systematically underpriced or overpriced coretime, which affects on-chain resource allocation and coretime chain economics broadly.

### Likelihood Explanation
Any signed, unprivileged account can call the public `purchase` extrinsic (`do_purchase`) with a self-chosen `price_limit` and simply times its purchase so that it lands as the transition purchase around `ideal_cores_sold`. No governance, validator, relayer, or malicious-peer assumption is required — this is a pure public-entrypoint interaction available to any account with funds for one core purchase.

### Recommendation
Compute `sellout_price`/the pricing signal fed to `AdaptPrice` as a volume- or time-weighted aggregate over multiple purchases within the sale (e.g., average price of the last N purchases around the `ideal_cores_sold` threshold, or a cumulative-price/time formula analogous to a real TWAP) rather than trusting a single transaction's price. Alternatively, bound how much a single sale's `sellout_price` can move `target_price`/`end_price` per rotation (a dampening/clamp factor across multiple sale periods) so one purchase cannot swing an entire subsequent region's pricing.

### Proof of Concept
1. Monitor `SaleInfo::cores_sold` approaching `ideal_cores_sold` for the current sale.
2. Call `Broker::purchase` (dispatches to `do_purchase` → `purchase_core`) with a `price_limit` set to the current leadin-decayed `sale_price`, timing the transaction to be the purchase that makes `cores_sold == ideal_cores_sold` (or the very next one, which the branch condition `cores_sold <= ideal_cores_sold` still allows to overwrite `sellout_price`).
3. This purchase's `price` becomes `sale.sellout_price` per [5](#0-4) , permanently recorded in `SaleInfo` for that sale.
4. At `rotate_sale`, `AdaptPrice::adapt_price(SalePerformance::from_sale(&old_sale))` derives `end_price`/`target_price` for the next sale directly from this single `sellout_price` (`substrate/frame/broker/src/tick_impls.rs:178`), and `target_price` is stamped into every `PotentialRenewalRecord` for cores expiring into the new region (`tick_impls.rs:228-241`), affecting renewal pricing for potentially many separate tasks/cores for the length of that region.

### Citations

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

**File:** substrate/frame/broker/src/tick_impls.rs (L176-184)
```rust

		// Calculate the start price for the upcoming sale.
		let new_prices = T::PriceAdapter::adapt_price(SalePerformance::from_sale(&old_sale));

		log::debug!(
			"Rotated sale, new prices: {:?}, {:?}",
			new_prices.end_price,
			new_prices.target_price
		);
```

**File:** substrate/frame/broker/src/tick_impls.rs (L228-241)
```rust
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
