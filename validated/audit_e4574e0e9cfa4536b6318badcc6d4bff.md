## Analysis

The report's core broken invariant is: **a settlement-critical price is derived from a mutable, publicly-writable state variable, and any unprivileged account can move that variable to their own advantage right before it gets "locked in" for later use.** In the DAO auction case, `float_` (circulating NFT supply) is that variable; in `pallet-broker` (Coretime sale pallet) the analogous variable is `SaleInfoRecord::sellout_price`, which is written on every `purchase` call and becomes the sole input to both the next sale's pricing curve and all pending lease-renewal prices.

### Title
Unprivileged core purchase can poison `sellout_price`, mispricing the next Coretime sale and all pending lease renewals - (File: `substrate/frame/broker/src/utility_impls.rs`)

### Summary
`pallet-broker`'s `purchase_core` records the price of *any* purchase that keeps `cores_sold <= ideal_cores_sold` as `sale.sellout_price`, overwriting it on every such purchase. Because the in-sale price follows a public, monotonically-decreasing leadin curve, any unpermissioned buyer can choose *when* within the leadin period to make the purchase that lands on (or under) the `ideal_cores_sold` boundary, thereby dictating the recorded `sellout_price` almost at will. That single number is later fed unmodified into `AdaptPrice::adapt_price` (`end_price`/`target_price` for the next sale) and into every `PotentialRenewals` entry created in `rotate_sale`, so one cheap transaction can durably mis-price an entire subsequent sale epoch and all renewals rolling over in it.

### Finding Description
`purchase_core` in [1](#0-0)  increments `cores_sold` and then unconditionally overwrites `sale.sellout_price` with the *current transaction's* price whenever the post-increment `cores_sold` is still `<= ideal_cores_sold` (or no price has ever been recorded). There is no floor/ceiling check, no averaging, and no protection against the value being set by whichever account happens to submit the purchase that reaches (or stays under) the ideal boundary.

`sale_price` in [2](#0-1)  shows price decreases monotonically over the leadin period via `T::PriceAdapter::leadin_factor_at`, and `CenterTargetPrice::leadin_factor_at` in [3](#0-2)  confirms the factor goes from a high multiple (100x `end_price`) down to 1x by the end of leadin — this curve, and the current `cores_sold` count, are both fully public/observable on-chain state.

At `rotate_sale`, `SalePerformance::from_sale` captures whatever `sellout_price` was last recorded, and `T::PriceAdapter::adapt_price` in [4](#0-3)  derives both `end_price` (next sale's floor) and `target_price` directly and only from that single value. `rotate_sale` then uses `new_prices.target_price` as the fixed price written into every `PotentialRenewalRecord` for leases expiring at that rotation, as seen in [5](#0-4) .

There is no mechanism preventing an attacker from:
- Waiting until near the end of the leadin period (price near `end_price`, i.e. the cheapest point) to be the buyer whose purchase brings `cores_sold` to (or keeps it under) `ideal_cores_sold`, thereby locking in an artificially **low** `sellout_price` — depressing the entire next sale's pricing and undercutting renewal fees owed by lease holders (protocol/treasury revenue loss, "public underpriced work"), or
- Buying immediately at sale start (highest leadin multiplier) to lock in an artificially **high** `sellout_price` — inflating the next sale's `end_price`/`target_price` and forcing unrelated lease holders into inflated, non-market renewal costs (griefing other users, mirroring the "NFT owner receives less/more than expected" impact in the report).

This is the direct structural analog of the reported bug: a value meant to reflect genuine market clearing (`realFloorValue` / `sellout_price`) is derived from state that an ordinary unprivileged caller can unilaterally set moments before it is locked in and used for downstream settlement (auction start price / redeem value ↔ next sale price / renewal price).

### Impact Explanation
`sellout_price` feeds directly into economically consequential, protocol-wide settlement values: the price floor and target for an entire subsequent Coretime sale, and the exact price charged to every account whose lease renews at that rotation. A single cheaply-timed `purchase` transaction can therefore misprice public coretime sales chain-wide (depressing collected revenue / treasury inflow) or force other, unrelated accounts (lease holders with no say in the timing) to pay inflated renewal prices they did not agree to. This fits the "public underpriced work that degrades... stalls... processing" and "wrong beneficiary or amount" impact categories for value settlement.

### Likelihood Explanation
The attack requires only a signed account and enough balance to buy one core — no privileged role, governance action, validator/collator collusion, or malicious relayer is needed. All the state used to decide timing (`cores_sold`, `ideal_cores_sold`, `leadin_length`, `sale_start`, current block) is public. It is trivially executable on every sale rotation, making likelihood high wherever `ideal_cores_sold` isn't reached immediately by organic demand (a common situation, since `ideal_cores_sold` is deliberately set below `cores_offered` to keep an "ideal" market signal).

### Recommendation
Do not let a single purchaser's transaction unilaterally set the reference price used for the next epoch. Consider averaging/weighting `sellout_price` over the purchases occurring within the ideal-sold window (e.g., a running average or median rather than last-write-wins), or basing the metric on a time-weighted price rather than "whoever happens to complete the ideal-th purchase." Alternatively, apply bounds/clamping and a minimum number of samples before trusting the recorded value for `AdaptPrice`, and consider decoupling the renewal price fed into `PotentialRenewals` from an easily-gamed single-transaction price.

### Proof of Concept
1. Wait for `start_sales`/`rotate_sale` to open a new sale with `ideal_cores_sold = N` and `cores_offered > N`.
2. Monitor `SaleInfo::cores_sold`; if it has not yet reached `N`, wait until close to the end of the `leadin_length` window (price close to `end_price`, i.e., lowest possible per `sale_price`/`leadin_factor_at`).
3. Call `purchase(price_limit)` with a `price_limit` matching the now-low current price, ensuring this purchase brings `cores_sold` to exactly `N` (or leaves it `<= N` if it's the last such purchase before sellout/rotation).
4. `purchase_core` records this low price as `sale.sellout_price` (per `substrate/frame/broker/src/utility_impls.rs:78-91`).
5. At the next `rotate_sale`, `AdaptPrice::adapt_price` (`substrate/frame/broker/src/adapt_price.rs:119-136`) computes the new sale's `end_price`/`target_price` from this attacker-chosen low value, and any `PotentialRenewals` created in that rotation (`substrate/frame/broker/src/tick_impls.rs:228-241`) use the same depressed `target_price` — all future buyers and unrelated lease renewers are now priced off a value the attacker chose with one cheap transaction.

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

**File:** substrate/frame/broker/src/adapt_price.rs (L111-117)
```rust
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
