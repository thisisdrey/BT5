Based on my research, the closest local analog to the external report's pattern — an admin-settable parameter with no bounds check that is later used as a divisor and panics — is in `pallet-broker`'s coretime sale-price calculation.

### Title
Root-configurable `leadin_length` used as an unguarded divisor in `Pallet::sale_price` can panic and halt coretime sales - (File: `substrate/frame/broker/src/utility_impls.rs`)

### Summary
The external report's core invariant break is: an owner-controlled numeric setter has no minimum bound, and that value is later used as a raw divisor in settlement logic, causing a panic/revert that blocks all subsequent settlement (`settleAuction`). The closest structural analog in this repository is `pallet-broker`'s `sale_price` function, which divides by `sale.leadin_length` — a value that flows directly from the root-settable `ConfigRecord::leadin_length` field — using the panicking `FixedU64::from_rational` constructor rather than the checked/saturating variant.

### Finding Description
`Pallet::<T>::sale_price` computes the current price during a coretime sale as:
```rust
pub fn sale_price(sale: &SaleInfoRecordOf<T>, now: RelayBlockNumberOf<T>) -> BalanceOf<T> {
    let num = now.saturating_sub(sale.sale_start).min(sale.leadin_length).saturated_into();
    let through = FixedU64::from_rational(num, sale.leadin_length.saturated_into());
    T::PriceAdapter::leadin_factor_at(through).saturating_mul_int(sale.end_price)
}
``` [1](#0-0) 

`FixedU64::from_rational(a, b)` is the **panicking** constructor: it panics with `"attempt to divide by zero in from_rational"` if `b == 0`, unlike `checked_from_rational`/`EnsureFixedPointNumber::ensure_from_rational`, which return `None`/`Err` instead: [2](#0-1) 

`sale.leadin_length` is populated in `rotate_sale` directly from the pallet's `ConfigRecord::leadin_length`, which is set via the root-only `do_configure` call: [3](#0-2) [4](#0-3) 

`do_configure` only calls `config.validate()` before storing the value — I was unable to locate and confirm the body of `ConfigRecord::validate()` within the available index (it is not one of the files returned by my searches), so it is **unverified** whether that validation function rejects `leadin_length == 0`. This is the key open question for this finding: if `validate()` does not enforce `leadin_length > 0`, then setting it to zero via `configure` (root origin, but this is a legitimate parameter-configuration extrinsic rather than a "malicious governance" scenario — it is analogous to the `setAuctionDecrement` setter in the source report) will cause every subsequent `do_purchase` / `do_renew` / `current_price` call to panic inside `sale_price`, since the sale's `leadin_length` is copied verbatim from config at each `rotate_sale`.

### Impact Explanation
If `leadin_length` can be set to zero (unverified — needs confirmation of `ConfigRecord::validate()`), any call into `do_purchase`, `do_renew`, or the read-only `current_price` API — all of which invoke `sale_price` — would panic. Under Substrate's execution model, an unhandled panic inside a dispatchable aborts the transaction with a defensive/panic failure rather than a clean `DispatchError`, and worse, since `current_price` and `sale_price` are also used by scheduled/automatic paths (`do_renew` is invoked from `do_enable_auto_renew`), a stuck configuration could permanently block coretime purchases/renewals, i.e. stall bulk-coretime sale processing until a further runtime upgrade or config fix — matching the "public underpriced work that degrades block production or stalls bridge/chain processing" and "permanent... lock" impact categories in the gate.

### Likelihood Explanation
Likelihood is **conditional and unconfirmed**: it depends entirely on whether `ConfigRecord::validate()` bounds `leadin_length` to be `> 0`. I could not retrieve this function's source through the available index (the file `substrate/frame/broker/src/types.rs` — the likely home of `ConfigRecord` and its `validate()` — returned only partial content in my searches, not the validation logic itself). Because of this, I cannot assert with certainty that this is an *unguarded* path the way the original `setAuctionDecrement` report was; it may already be defended.

### Recommendation
- Verify (via full source access) whether `ConfigRecord::validate()` enforces `leadin_length > 0`. If it does not, add that check, returning `Error::<T>::InvalidConfig` for `leadin_length == 0`, mirroring the recommended fix in the source report.
- Regardless, replace the panicking `FixedU64::from_rational(num, sale.leadin_length...)` in `sale_price` with the checked/`ensure_from_rational` variant and fall back to a safe default (e.g. treat zero leadin as "leadin already complete") rather than relying solely on upstream config validation to prevent a panic in a balance/price computation path.

### Proof of Concept
Conceptual PoC (unverified end-to-end due to inability to confirm `ConfigRecord::validate()`):
1. Call `Broker::configure` (root origin) with a `ConfigRecord` where `leadin_length = 0`.
2. If accepted, on the next `rotate_sale` the new `SaleInfoRecord.leadin_length` becomes `0`.
3. Any subsequent `Broker::purchase` or `Broker::renew` call (or the runtime API `current_price`) invokes `sale_price`, which calls `FixedU64::from_rational(num, 0)` and panics, aborting the extrinsic/query and effectively halting coretime sales until the configuration is corrected.

**Caveat**: This finding is presented with an explicit unresolved dependency — confirmation that `ConfigRecord::validate()` does not already guard against `leadin_length == 0`. Due to index size limits, I was unable to retrieve that function's implementation. I recommend starting a full Devin session (with complete filesystem access) to inspect `substrate/frame/broker/src/types.rs` in full and confirm this before treating the finding as conclusively exploitable.

### Citations

**File:** substrate/frame/broker/src/utility_impls.rs (L62-66)
```rust
	pub fn sale_price(sale: &SaleInfoRecordOf<T>, now: RelayBlockNumberOf<T>) -> BalanceOf<T> {
		let num = now.saturating_sub(sale.sale_start).min(sale.leadin_length).saturated_into();
		let through = FixedU64::from_rational(num, sale.leadin_length.saturated_into());
		T::PriceAdapter::leadin_factor_at(through).saturating_mul_int(sale.end_price)
	}
```

**File:** substrate/primitives/arithmetic/src/fixed_point.rs (L700-719)
```rust
			pub const fn from_rational(a: u128, b: u128) -> Self {
				Self::from_rational_with_rounding(a, b, Rounding::NearestPrefDown)
			}

			/// Calculate an approximation of a rational with custom rounding.
			///
			/// WARNING: This is a `const` function designed for convenient use at build time and
			/// will panic on overflow. Ensure that any inputs are sensible.
			pub const fn from_rational_with_rounding(a: u128, b: u128, rounding: Rounding) -> Self {
				if b == 0 {
					panic!("attempt to divide by zero in from_rational")
				}
				match multiply_by_rational_with_rounding(Self::DIV as u128, a, b, rounding) {
					Some(value) => match Self::from_i129(I129 { value, negative: false }) {
						Some(x) => x,
						None => panic!("overflow in from_rational"),
					},
					None => panic!("overflow in from_rational"),
				}
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

**File:** substrate/frame/broker/src/dispatchable_impls.rs (L30-34)
```rust
	pub(crate) fn do_configure(config: ConfigRecordOf<T>) -> DispatchResult {
		config.validate().map_err(|()| Error::<T>::InvalidConfig)?;
		Configuration::<T>::put(config);
		Ok(())
	}
```
