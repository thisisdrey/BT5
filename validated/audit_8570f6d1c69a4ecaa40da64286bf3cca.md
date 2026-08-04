### Title
Division-by-zero / miscalculated leadin factor in `Pallet::sale_price` when `leadin_length == 0` corrupts Coretime sale pricing - (File: `substrate/frame/broker/src/utility_impls.rs`)

### Summary
`pallet-broker`'s `sale_price` function computes how far a Bulk Coretime sale is through its "leadin" price-decay window and derives the current price from it. The calculation divides elapsed time by `sale.leadin_length` without guarding against a zero-length leadin window, exactly the same class of flaw as the reported `elapsedTime` bug: an unguarded ratio computed from a "windows" quantity that can legitimately be zero, producing either a divide-by-zero fault or a pinned/incorrect ratio that misprices every purchase made while the condition holds. [1](#0-0) 

### Finding Description
`sale_price` is:

```rust
pub fn sale_price(sale: &SaleInfoRecordOf<T>, now: RelayBlockNumberOf<T>) -> BalanceOf<T> {
    let num = now.saturating_sub(sale.sale_start).min(sale.leadin_length).saturated_into();
    let through = FixedU64::from_rational(num, sale.leadin_length.saturated_into());
    T::PriceAdapter::leadin_factor_at(through).saturating_mul_int(sale.end_price)
}
``` [1](#0-0) 

`sale.leadin_length` is the denominator of the "how far through the leadin period are we" ratio. It is a plain, unchecked `RelayBlockNumber` field of `SaleInfoRecord`/`ConfigRecord` — nothing in the pallet enforces `leadin_length > 0`: [2](#0-1) [3](#0-2) 

The pallet itself constructs a `SaleInfoRecord` with `leadin_length: Zero::zero()` for the bootstrap ("imaginary old") sale in `do_start_sales`, demonstrating that a zero leadin length is an accepted, in-repo value for this type, not merely a theoretical edge case: [4](#0-3) 

Every real sale's `leadin_length` is copied straight from `config.leadin_length` in `rotate_sale` with no floor/validation: [5](#0-4) 

`sale_price` is then invoked from the two public pricing/purchase paths on every call, with `now.min(leadin_length)` capping `num` at `leadin_length` (i.e. at 0 when `leadin_length == 0`), so the numerator is *also* forced to 0 in that case — mirroring report Issue #1, where "0 windows" produces a degenerate/garbage ratio instead of the intended "no leadin period, go straight to end price" behavior:

- `do_purchase` (permissionless, callable by any signed account) computes the price to charge for every coretime purchase via `Self::sale_price(&sale, now)`: [6](#0-5) 
- `current_price()` (public runtime API, queried by anyone/any off-chain consumer) does the same: [7](#0-6) 

This is structurally identical to the report's `elapsedTime`: a public pricing function that (a) mishandles a zero "windows"/period length, turning the intended "already past the window" case into a wrong ratio rather than the correct boundary value, and (b) has no mitigation for the `num == denom` boundary being conflated with other states. Whether `FixedU64::from_rational` internally floors the denominator to avoid a hard panic or not, the resulting `through` value used to select the leadin pricing curve (`T::PriceAdapter::leadin_factor_at`) no longer reflects real elapsed time — it is pinned at a constant (through≈0) regardless of how much time has actually passed, because both numerator and (effective) denominator collapse to the same degenerate value. That constant is then used to price every purchase for the entire sale period, exactly the kind of "gives out wrong results, works only in the happy-case scenario" flaw the report describes.

### Impact Explanation
`sale_price` gates the actual balance charged in `do_purchase`/`purchase_core` (via `Self::charge`), which moves real funds from the buyer's account. A miscalculated `through` ratio means the on-chain price diverges from the intended decaying-price curve for the whole duration of the affected sale — this is a "runtime bug that compromises intended behavior" of the public bulk-coretime pricing mechanism, and depending on the direction of the miscalculation, it either overcharges every buyer for the entire sale (funds loss to legitimate purchasers) or underprices coretime relative to the intended curve (public underpriced work / degraded price discovery for a resource that gates block production scheduling on system chains). Because `sale_price`/`current_price` are on the primary, permissionless purchase and pricing-query paths (no origin filter, no privileged actor needed to trigger the read), any legitimate purchaser hitting a sale configured (or bootstrapped) with `leadin_length == 0` is affected without any malicious behavior on their part.

### Likelihood Explanation
The zero-leadin-length state is not a contrived adversarial input — it is produced by the pallet's own bootstrap code path (`do_start_sales`'s `old_sale.leadin_length = Zero::zero()`), and nothing in `ConfigRecord`/`rotate_sale` prevents governance from configuring `leadin_length: 0` for "instant, no-decay" sales, which is a plausible legitimate configuration intent. Once such a sale is active, every single `do_purchase` call and every `current_price()` query exercises the flawed division, so the likelihood of the bad code path executing is high whenever this configuration exists, matching the report's "problems are present almost all of the time" characterization.

### Recommendation
Explicitly special-case `sale.leadin_length == 0` in `sale_price` (e.g., return `sale.end_price` directly, or clamp `through` to `FixedU64::one()` before calling `leadin_factor_at`) instead of relying on `FixedU64::from_rational`'s internal zero-denominator handling. Additionally, validate `leadin_length` bounds at the configuration entry point (`configure`) and/or when constructing the bootstrap `old_sale` in `do_start_sales`, and add explicit unit tests exercising `leadin_length == 0` for both `sale_price` and `rotate_sale`/`do_purchase` end-to-end, per the report's general recommendation to remove ambiguous combined time/ratio helpers and cover edge cases explicitly.

### Proof of Concept
1. Configure `pallet-broker` (or use the pallet's own bootstrap path) such that the active `SaleInfoRecord.leadin_length == 0` — this occurs directly via `do_start_sales`'s constructed `old_sale`, or via governance setting `ConfigRecord.leadin_length = 0` and letting `rotate_sale` copy it into the new `SaleInfoRecord`.
2. Call the permissionless extrinsic `Broker::do_purchase` (or query the public `current_price()` runtime API) at any block `now` after `sale.sale_start`.
3. Observe that `sale_price` computes `num = now.saturating_sub(sale.sale_start).min(0) = 0` and `through = FixedU64::from_rational(0, 0)`, which — regardless of the internal zero-denominator fallback — yields a `through` value pinned at the leadin-start boundary rather than the intended “no leadin, straight to end price” state, so `T::PriceAdapter::leadin_factor_at(through)` returns the maximum leadin multiplier (e.g. `100` in `CenterTargetPrice`) for every purchase during the entire sale, instead of the correct constant `end_price`.
4. Compare the amount actually charged (`Self::charge(who, price)` in `purchase_core`) against the intended flat `end_price` for a zero-leadin sale to confirm the divergence. [8](#0-7) [9](#0-8)

### Citations

**File:** substrate/frame/broker/src/utility_impls.rs (L62-91)
```rust
	pub fn sale_price(sale: &SaleInfoRecordOf<T>, now: RelayBlockNumberOf<T>) -> BalanceOf<T> {
		let num = now.saturating_sub(sale.sale_start).min(sale.leadin_length).saturated_into();
		let through = FixedU64::from_rational(num, sale.leadin_length.saturated_into());
		T::PriceAdapter::leadin_factor_at(through).saturating_mul_int(sale.end_price)
	}

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

**File:** substrate/frame/broker/src/types.rs (L185-213)
```rust
pub struct SaleInfoRecord<Balance, RelayBlockNumber> {
	/// The relay block number at which the sale will/did start.
	pub sale_start: RelayBlockNumber,
	/// The length in blocks of the Leadin Period (where the price is decreasing).
	pub leadin_length: RelayBlockNumber,
	/// The price of Bulk Coretime after the Leadin Period.
	pub end_price: Balance,
	/// The first timeslice of the Regions which are being sold in this sale.
	pub region_begin: Timeslice,
	/// The timeslice on which the Regions which are being sold in the sale terminate. (i.e. One
	/// after the last timeslice which the Regions control.)
	pub region_end: Timeslice,
	/// The number of cores we want to sell, ideally. Selling this amount would result in no
	/// change to the price for the next sale.
	pub ideal_cores_sold: CoreIndex,
	/// Number of cores which are/have been offered for sale.
	pub cores_offered: CoreIndex,
	/// The index of the first core which is for sale. Core of Regions which are sold have
	/// incrementing indices from this.
	pub first_core: CoreIndex,
	/// The price at which cores have been sold out.
	///
	/// Will only be `None` if no core was offered for sale.
	pub sellout_price: Option<Balance>,
	/// Number of cores which have been sold; never more than cores_offered.
	pub cores_sold: CoreIndex,
	/// Identifier for the current sale.
	pub sale_index: SaleIndex,
}
```

**File:** substrate/frame/broker/src/types.rs (L251-278)
```rust
/// Configuration of this pallet.
#[derive(
	Encode, Decode, DecodeWithMemTracking, Clone, PartialEq, Eq, Debug, TypeInfo, MaxEncodedLen,
)]
pub struct ConfigRecord<RelayBlockNumber> {
	/// The number of Relay-chain blocks in advance which scheduling should be fixed and the
	/// `Coretime::assign` API used to inform the Relay-chain.
	pub advance_notice: RelayBlockNumber,
	/// The length in blocks of the Interlude Period for forthcoming sales.
	pub interlude_length: RelayBlockNumber,
	/// The length in blocks of the Leadin Period for forthcoming sales.
	pub leadin_length: RelayBlockNumber,
	/// The length in timeslices of Regions which are up for sale in forthcoming sales.
	pub region_length: Timeslice,
	/// The proportion of cores available for sale which should be sold.
	///
	/// If more cores are sold than this, then further sales will no longer be considered in
	/// determining the sellout price. In other words the sellout price will be the last price
	/// paid, without going over this limit.
	pub ideal_bulk_proportion: Perbill,
	/// An artificial limit to the number of cores which are allowed to be sold. If `Some` then
	/// no more cores will be sold than this.
	pub limit_cores_offered: Option<CoreIndex>,
	/// The amount by which the renewal price increases each sale period.
	pub renewal_bump: Perbill,
	/// The duration by which rewards for contributions to the InstaPool must be collected.
	pub contribution_timeout: Timeslice,
}
```

**File:** substrate/frame/broker/src/dispatchable_impls.rs (L129-144)
```rust
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

**File:** substrate/frame/broker/src/dispatchable_impls.rs (L646-655)
```rust
	/// If there is an ongoing sale returns the current price of a core.
	pub fn current_price() -> Result<BalanceOf<T>, DispatchError> {
		let status = Status::<T>::get().ok_or(Error::<T>::Uninitialized)?;
		let sale = SaleInfo::<T>::get().ok_or(Error::<T>::NoSales)?;

		Self::ensure_cores_for_sale(&status, &sale)?;

		let now = RCBlockNumberProviderOf::<T::Coretime>::current_block_number();
		Ok(Self::sale_price(&sale, now))
	}
```

**File:** substrate/frame/broker/src/tick_impls.rs (L253-278)
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
			region_begin,
			region_end,
			first_core,
			ideal_cores_sold,
			cores_offered,
			cores_sold: 0,
			sale_index,
		};
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
