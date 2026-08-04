Based on my research, I found a strong local analog to the reported bug class in `pallet_treasury`'s use of `AssetRate` for spend-cap validation versus payout.

### Title
Treasury spend cap validated against a mutable `AssetRate` conversion that can diverge from the rate at payout time - (File: `substrate/frame/treasury/src/lib.rs`)

### Summary
The external report describes a core broken invariant: a value (collateral/borrow valuation) is computed once against a price source, but the *actual* settlement happens against a different/updated price source, and the code never re-validates the safety bound at settlement time. The same pattern exists in `pallet_treasury`: `Config::BalanceConverter` (implemented by `pallet_asset_rate`, see `substrate/frame/asset-rate/src/lib.rs`) converts an `AssetKind` amount to a native-currency equivalent **only at `spend()`-time** to check it against `SpendOrigin`'s `Success` cap (`MaxBalance` for the origin) [1](#0-0) . The actual transfer to the beneficiary later happens through `Paymaster::pay` using the raw, un-reconverted `AssetKind` amount, with no re-check against the current `AssetRate` at `payout()` time.

### Finding Description
`type BalanceConverter: ConversionFromAssetBalance<..>` is documented explicitly as "solely for the purpose of asserting the result against the maximum allowed spend amount of the `SpendOrigin`" [1](#0-0) . The `pallet_asset_rate::ConversionRateToNative` storage is a mutable, `update`-able map that governance/`UpdateOrigin` can change at any time via `Pallet::update` [2](#0-1) , and the from_asset_balance conversion is a simple `rate.saturating_mul_int(balance)` snapshot at the moment of `spend()` [3](#0-2) .

This mirrors the AAVE issue exactly: at "lever time" (spend approval) a value is checked against a safety bound using one snapshot of a price/rate; at "settlement time" (payout, which can occur up to `PayoutPeriod` later, e.g. 30 days in several runtimes — see `PayoutSpendPeriod` = `30 * DAYS` in `cumulus/parachains/runtimes/assets/asset-hub-westend/src/governance/mod.rs`) the actual asset movement uses the current, potentially very different, rate/amount, with the cap never re-enforced. The `Paymaster` implementations (`PayOverXcm`, `PayAssetFromAccount`, `LocalPay`) pay out the exact `AssetKind`/`Beneficiary` amount stored in the `SpendStatus`, not a re-derived, re-capped amount.

Because `AssetRate::update` is a normal, non-privileged-to-the-spend-flow admin action (any `UpdateOrigin`, which in many runtimes is a relatively low council/track origin, not the same actor approving the specific spend), a spend that was validated as "within cap" using the old rate can be settled as a payout whose *actual* value (at prevailing market/rate) vastly exceeds what `SpendOrigin` was ever authorized to approve — undermining the entire purpose of the cap check, just like `execution.unutilizedLeveragePercentage` being silently overridden in the original report.

### Impact Explanation
This breaks the "Balances, assets, ... treasury spends ... must conserve value and settle exactly once to the rightful beneficiary and amount" invariant from the required-impact list: the *amount* authorized by `SpendOrigin` is not what actually settles when `AssetRate` changes between `spend()` and `payout()`. A `SpendOrigin` with a modest native-currency authorization (e.g., capped at `X` DOT-equivalent) could have its spend balloon in effective value if the rate is later lowered (making the same asset amount look "cheaper" at approval time but the beneficiary still receives the full original `AssetKind` amount, whose value in native terms was never re-checked at payout). This is a spend/payout amount-mismatch that can result in beneficiaries receiving assets whose value materially exceeds the origin's true spending authority, or governance losing the ability to reason about aggregate treasury outflows.

### Likelihood Explanation
High compared to the original AAVE analog: `AssetRate::update` is a routine, expected, low-friction governance action (comment in `pallet_asset_rate` even states rates are only "estimates" and not meant to track real values closely) [4](#0-3) , and the payout window can be as long as 30 days, giving ample opportunity for the rate to be updated between approval and claim, with `payout()` never re-validating against the origin's cap.

### Recommendation
Re-validate (or re-derive) the native-equivalent value using the current `BalanceConverter` rate at `payout()` time and reject or require re-approval if it now exceeds the originally-authorized `SpendOrigin::Success` bound, rather than only checking it once at `spend()`-time using a rate snapshot that can be silently changed by a separate, less-privileged governance action before the funds are actually paid out.

### Proof of Concept
1. `SpendOrigin` X is capped at `MaxBalance` = 1,000 DOT-equivalent.
2. `AssetRate` for asset `A` is `1 A = 1 DOT`. Governance under `UpdateOrigin` (different track than treasury `SpendOrigin`) is functioning normally.
3. Actor with `SpendOrigin` authority calls `spend(A, 1000, beneficiary)` — passes the check since `1000 * 1 = 1000 <= 1000`.
4. Before `payout()` is claimed (within `PayoutPeriod`, e.g. 30 days), `AssetRate::update` changes the rate for `A` to `1 A = 0.001 DOT` for legitimate reasons (asset re-pricing) — this does not touch the already-approved `SpendStatus`.
5. Note: even if the rate moved the *other* direction (`1 A = 1000 DOT`), the same unrevalidated payout would occur, meaning a spend that would today be worth 1,000,000 DOT-equivalent and far exceeds the origin's cap is paid out untouched, because `payout()` in `substrate/frame/treasury/src/lib.rs` never re-invokes `BalanceConverter` — it only reads the stored `SpendStatus.amount`/`asset_kind` and hands them to `T::Paymaster::pay`. [1](#0-0) [2](#0-1)

### Citations

**File:** substrate/frame/treasury/src/lib.rs (L276-283)
```rust
		/// Type for converting the balance of an [Self::AssetKind] to the balance of the native
		/// asset, solely for the purpose of asserting the result against the maximum allowed spend
		/// amount of the [`Self::SpendOrigin`].
		type BalanceConverter: ConversionFromAssetBalance<
			<Self::Paymaster as Pay>::Balance,
			Self::AssetKind,
			BalanceOf<Self, I>,
		>;
```

**File:** substrate/frame/asset-rate/src/lib.rs (L54-57)
```rust
//! ### Assumptions
//!
//! * Conversion rates are only used as estimates, and are not designed to be precise or closely
//!   tracking real world values.
```

**File:** substrate/frame/asset-rate/src/lib.rs (L185-216)
```rust
		/// Update the conversion rate to native balance for the given asset.
		///
		/// ## Complexity
		/// - O(1)
		#[pallet::call_index(1)]
		#[pallet::weight(T::WeightInfo::update())]
		pub fn update(
			origin: OriginFor<T>,
			asset_kind: Box<T::AssetKind>,
			rate: FixedU128,
		) -> DispatchResult {
			T::UpdateOrigin::ensure_origin(origin)?;

			let mut old = FixedU128::zero();
			ConversionRateToNative::<T>::mutate(asset_kind.as_ref(), |maybe_rate| {
				if let Some(r) = maybe_rate {
					old = *r;
					*r = rate;

					Ok(())
				} else {
					Err(Error::<T>::UnknownAssetKind)
				}
			})?;

			Self::deposit_event(Event::AssetRateUpdated {
				asset_kind: *asset_kind,
				old,
				new: rate,
			});
			Ok(())
		}
```

**File:** substrate/frame/asset-rate/src/lib.rs (L246-253)
```rust
	fn from_asset_balance(
		balance: BalanceOf<T>,
		asset_kind: AssetKindOf<T>,
	) -> Result<BalanceOf<T>, pallet::Error<T>> {
		let rate = pallet::ConversionRateToNative::<T>::get(asset_kind)
			.ok_or(pallet::Error::<T>::UnknownAssetKind.into())?;
		Ok(rate.saturating_mul_int(balance))
	}
```
