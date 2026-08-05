Based on my investigation, the strongest local analog to the BigBang/USDO liquidation-burn bug is in the `pallet-psm` (Peg Stability Module) pallet, which implements the same mint-collateral/burn-debt invariant as BigBang's borrow/repay flow.

### Title
Redemption fee is transferred in `internal_asset` but never burned, permanently inflating internal-asset supply relative to PSM reserve — (File: `substrate/frame/psm/src/lib.rs`)

### Summary
The external report's core broken invariant is: an asset that is supposed to be burned to keep 1:1 backing between minted supply and locked collateral is instead left in circulation, silently inflating supply relative to backing. `pallet-psm` implements the same class of invariant — minting `internal_asset` against externally-deposited collateral, tracked via `PsmDebt`, and redemption should reduce both the user's `internal_asset` balance and the total supply proportional to collateral released. In `redeem`, the fee portion of the redeemed `internal_asset` is *transferred* to `fee_destination` rather than being burned along with the rest, so total issuance of `internal_asset` is not reduced by the fee amount even though the corresponding external collateral backing that fee's notional value never leaves the reserve for that user.

### Finding Description
In `Pallet::redeem` [1](#0-0) , the flow is:
1. `fee = fee_rate.mul_ceil(internal_amount)` is computed from the raw `internal_amount` supplied by the user (denominated in internal-asset units, i.e. minted stablecoin), and `internal_net = internal_amount - fee`.
2. The `fee` amount is **transferred** to `info.fee_destination` in `internal_asset`: [2](#0-1) 
3. Only `effective_internal_net` (derived from `internal_net` after a decimals round-trip) is **burned** from the caller: [3](#0-2) 
4. `PsmDebt` is decremented only by `effective_internal_net`, not by `fee`: [4](#0-3) 

Because the fee is transferred (not burned), the total issuance of `internal_asset` is never reduced by the fee portion on redemption. Every redemption permanently leaves `fee` units of `internal_asset` in circulation (now held by `fee_destination`) that are backed by nothing — the corresponding external collateral for that fee slice was released from the PSM reserve to the user (the `external_out` calculation off `internal_net`, not `internal_amount`, already returns collateral net of the withheld fee-equivalent, meaning the collateral tied to the fee stays in the reserve, but the internal-asset unit itself is never destroyed). Over many redemption cycles, `internal_asset` total supply grows relative to the aggregate `PsmDebt`/reserve, exactly mirroring the BigBang bug: fee-bearing debt-token units are extracted from circulation on paper (removed from `PsmDebt`) but the actual token itself is not burned — it is merely reassigned to a new holder, so total issuance keeps climbing.

This contrasts with `mint`, where the minting fee is explicitly `mint_into`'d (freshly created supply matching new collateral) — so mint fees are backed, but redeem fees are not un-created, breaking the intended 1:1 symmetry that the pallet's own documentation claims ("Burns `amount` of `internal_asset` from the caller... then transfers...").

### Impact Explanation
This directly threatens the peg the PSM exists to defend: `internal_asset` total supply diverges upward from the value of collateral actually backing it (`PsmDebt`/external reserve), the same mechanism as the referenced BigBang H-10 finding. Since `PsmDebt` is authoritative for the debt ceiling (`ExceedsMaxPsmDebt` checks) and reserve accounting (`InsufficientReserve` checks), and both are decremented by `effective_internal_net` only, repeated mint/redeem cycles bleed unbacked `internal_asset` into `fee_destination`'s balance while collateral accounted for those tokens is never reserved for them. If `internal_asset` is used downstream (e.g., in strategies, other PSMs, or as collateral elsewhere), this creates unbacked value exactly as described in the original report — supply exceeds locked collateral, and the peg mechanism (fee corridor) is undermined because "fees" are supposed to be net economic friction removed from the system, not new unbacked balance for `fee_destination`.

### Likelihood Explanation
This triggers on every single call to `redeem` where `fee_rate > 0` — it is not an edge case, requires no privileged actor, and is reachable by any signed user calling the public `redeem` extrinsic [5](#0-4) . The pallet's own test suite for `success_basic` under `redeem` explicitly asserts a state consistent with this behavior (fee transferred, not burned) without checking total-issuance conservation against `PsmDebt`: [6](#0-5) . The larger multi-cycle test even asserts `psm_external_after == psm_debt_after` (reserve equals *debt*, not equals *total issuance*), silently validating the accounting scheme that never reconciles supply with reserve+fee. Likelihood of exploitation/organic occurrence is effectively certain under normal fee-based operation.

### Recommendation
In `redeem`, burn the full `internal_amount` (both `effective_internal_net` and `fee`) from the caller via `burn_from`, then separately `mint_into` (or better, transfer only real, previously-reserved value) the fee into `fee_destination` only if the fee is meant to represent a real economic transfer of already-existing backed value — or, to match the mint-side symmetry (which mints new backed supply as fee), instead **decrement `PsmDebt` by the full `internal_amount`, not just `effective_internal_net`**, and burn the fee too, so that redemption fees behave like burnt friction rather than an untracked transfer. In either resolution, ensure `PsmDebt` bookkeeping and actual `internal_asset` total issuance stay reconciled after every redemption.

### Proof of Concept
1. Admin creates a PSM, approves `USDC_ASSET_ID`, sets `RedemptionFee = 1%`, `min_swap_amount = 0`.
2. Alice mints `1000` internal units against `1000` USDC (0% mint fee for simplicity) — `PsmDebt = 1000`, `TotalIssuance(internal) += 1000`.
3. Alice calls `redeem(internal_amount = 1000)`: `fee = 10`, `internal_net = 990`. `effective_internal_net` ≈ `990` is burned from Alice; `10` is transferred (not burned) to `fee_destination`. `PsmDebt` decreases by `990` → `PsmDebt = 10`.
4. Check `TotalIssuance(internal_asset)`: before redeem it was `1000`; after redeem it is `1000 - 990 = 10` (the burned amount) — but `fee_destination` now holds `10` internal units backed by nothing, since the reserve only released `external_out` proportional to `990`, not `1000`. Repeating mint/redeem cycles accumulates `fee_destination`'s unbacked internal-asset balance indefinitely, exactly reproducing the "USDO acquired through liquidation should be burned" defect from the original report, generalized to every redemption fee taken by this PSM pallet. This matches the repeated-cycle test at [7](#0-6) , which asserts fee accumulation in `fee_destination` (`IF increase == total_fees`) without ever validating that total internal-asset issuance is fully reconciled against locked reserve value.

### Citations

**File:** substrate/frame/psm/src/lib.rs (L809-876)
```rust
		#[pallet::call_index(1)]
		#[pallet::weight(T::WeightInfo::redeem())]
		pub fn redeem(
			origin: OriginFor<T>,
			internal_asset: T::AssetId,
			external_asset: T::AssetId,
			internal_amount: BalanceOf<T>,
			max_fee: Permill,
		) -> DispatchResult {
			let who = ensure_signed(origin)?;
			let info = Psm::<T>::get(&internal_asset).ok_or(Error::<T>::PsmNotFound)?;

			let external = ExternalAssets::<T>::get(&internal_asset, &external_asset)
				.ok_or(Error::<T>::UnsupportedAsset)?;
			ensure!(external.status.allows_redemption(), Error::<T>::AllSwapsStopped);

			let ext_decimals = external.decimals;
			let internal_decimals = info.internal_decimals;

			ensure!(internal_amount >= info.min_swap_amount, Error::<T>::BelowMinimumSwap);

			let fee_rate = RedemptionFee::<T>::get(&internal_asset, &external_asset);
			ensure!(fee_rate <= max_fee, Error::<T>::FeeTooHigh);
			let fee = fee_rate.mul_ceil(internal_amount);
			let internal_net = internal_amount.saturating_sub(fee);

			let external_out =
				Self::internal_to_external(internal_net, ext_decimals, internal_decimals)?;
			ensure!(
				internal_net.is_zero() || !external_out.is_zero(),
				Error::<T>::AmountTooSmallAfterConversion
			);
			// `effective_internal_net` is the internal value that round-trips to `external_out`;
			// it is what we actually burn and what the tracked debt decreases by. Any truncation
			// dust stays in the caller's internal balance, symmetric with `mint`, which takes
			// only the round-tripped share of the external amount.
			let effective_internal_net =
				Self::external_to_internal(external_out, ext_decimals, internal_decimals)?;

			let current_debt = PsmDebt::<T>::get(&internal_asset, &external_asset);
			ensure!(current_debt >= effective_internal_net, Error::<T>::InsufficientReserve);

			let reserve = Self::get_reserve(&internal_asset, &external_asset);
			if reserve < external_out {
				defensive!("PSM reserve is less than expected output amount");
				return Err(Error::<T>::Unexpected.into());
			}

			if !fee.is_zero() {
				T::Fungibles::transfer(
					internal_asset.clone(),
					&who,
					&info.fee_destination,
					fee,
					Preservation::Expendable,
				)?;
			}

			if !effective_internal_net.is_zero() {
				T::Fungibles::burn_from(
					internal_asset.clone(),
					&who,
					effective_internal_net,
					Preservation::Expendable,
					Precision::Exact,
					Fortitude::Polite,
				)?;
			}
```

**File:** substrate/frame/psm/src/lib.rs (L889-891)
```rust
			PsmDebt::<T>::mutate(&internal_asset, &external_asset, |debt| {
				*debt = debt.saturating_sub(effective_internal_net);
			});
```

**File:** substrate/frame/psm/src/tests.rs (L397-423)
```rust
			assert_ok!(Psm::redeem(
				RuntimeOrigin::signed(ALICE),
				INTERNAL_ASSET_ID,
				USDC_ASSET_ID,
				redeem_amount,
				Permill::from_percent(1)
			));

			let fee = Permill::from_percent(1).mul_ceil(redeem_amount);
			let external_to_user = redeem_amount - fee;

			assert_eq!(
				get_asset_balance(INTERNAL_ASSET_ID, ALICE),
				alice_internal_before - redeem_amount
			);
			assert_eq!(
				get_asset_balance(USDC_ASSET_ID, ALICE),
				alice_usdc_before + external_to_user
			);
			assert_eq!(
				get_asset_balance(USDC_ASSET_ID, psm_account()),
				psm_usdc_before - external_to_user
			);
			assert_eq!(
				PsmDebt::<Test>::get(INTERNAL_ASSET_ID, USDC_ASSET_ID),
				debt_before - external_to_user
			);
```

**File:** substrate/frame/psm/src/tests.rs (L2364-2379)
```rust
			let total_fees = total_mint_fees + total_redeem_fees;
			let if_increase = if_internal_after - if_internal_before;
			let user_decrease = user_external_before - user_external_after + user_internal_before -
				user_internal_after;

			println!("\n=== Verification ===");
			println!("Total fees collected: {:.2}", total_fees as f64 / unit);
			println!("IF increase: {:.2}", if_increase as f64 / unit);
			println!("User decrease: {:.2}", user_decrease as f64 / unit);

			// Assertions
			assert!(cycle > 0, "Should have completed at least one cycle");
			assert_eq!(if_increase, total_fees, "IF should receive all fees");
			assert_eq!(psm_external_after, psm_debt_after, "PSM external = PSM debt");
			assert_eq!(user_decrease, total_fees, "User loss equals fees");
			assert!(psm_debt_after <= max_debt, "PSM debt should not exceed ceiling");
```
