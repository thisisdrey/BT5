Audit Report

## Title
Incorrect minting in PSM: `mint()` credits internal stablecoin based on the requested transfer amount instead of the external asset actually received by the reserve - ([File: substrate/frame/psm/src/lib.rs])

## Summary
`pallet-psm`'s `mint()` extrinsic transfers `effective_external` of `external_asset` to the PSM reserve account via `T::Fungibles::transfer(...)` and then mints `internal_to_user` + `fee` of internal stablecoin, and increases `PsmDebt`, all computed purely from the requested nominal amount rather than any observed balance delta on the reserve account. [1](#0-0)  The generic `fungibles::Mutate::transfer` default implementation decreases the source with `Exact`/`Polite` but credits the destination with `BestEffort`, and unconditionally returns `Ok(amount)` regardless of what was actually credited. [2](#0-1)  By contrast, `redeem()` explicitly re-reads `Self::get_reserve(...)` and hard-fails if the actual reserve balance is less than the expected `external_out` before paying out, showing the PSM pallet's own author was aware such a check is necessary — yet no equivalent check protects the mint (deposit) side. [3](#0-2) 

## Finding Description
In `mint()`, the flow is: compute `internal_equivalent` from the user-supplied `external_amount`, derive `effective_external` from that, then call `T::Fungibles::transfer(external_asset, &who, &psm_account, effective_external, Preservation::Expendable)?`, discarding only failure via `?` and never inspecting the `Ok` value or comparing pre/post reserve balances, before minting `internal_to_user` (+`fee`) and bumping `PsmDebt` by `new_debt` computed from `internal_equivalent`. [4](#0-3) 

The generic `transfer()` implementation used by `fungibles::Mutate` performs `decrease_balance(..., BestEffort, preservation, Polite)` on the source, then a *best-effort* `increase_balance(asset, dest, amount, BestEffort)` on the destination whose result is explicitly discarded (`let _ = ...`), and finally always returns `Ok(amount)` — the *requested* amount, not the amount actually credited to `dest`. [5](#0-4)  This means `mint()` has no mechanism to detect a shortfall between the nominal transfer amount and what the reserve account actually received, in contrast to `redeem()`, which explicitly guards the withdrawal side with a `get_reserve` check and fails with `Error::Unexpected` if the reserve is insufficient. [6](#0-5) 

However, in the currently configured/testable backend (`pallet-assets` via `type Fungibles = Assets`), the destination-side `increase_balance` under `Extant`/typical configurations does not typically produce a silent shortfall on a normal transfer between two already-existing, non-frozen accounts with sufficient existential deposit — the scenario the claim relies on (ED dust destruction on first touch, issuance caps, deposit/withdraw amount divergence) requires either an atypical `Fungibles` backend or specific edge-case account states (e.g., reserve account not yet in existence and ED rounding). I was not able to fully verify, within the available tool budget, a concrete reachable path in `pallet-assets`'s `increase_balance`/`deposit` logic where a normal transfer to the `psm_account` (which is provider-referenced at PSM creation via `inc_providers`, per `install_test_psm`) would legitimately deliver less than the requested amount under `BestEffort`. [7](#0-6) 

## Impact Explanation
If the underlying `Fungibles` backend can, under any circumstance, credit the `psm_account` with less than the nominal `effective_external` while `transfer()` still reports `Ok`, then `PsmDebt` and freshly minted internal-asset supply would no longer be fully backed by the reserve's actual external-asset balance, undermining the 1:1 backing invariant the pallet documents and that `redeem()`'s reserve check relies on for solvency. This matches the "theft or unbacked mint" impact category in principle, but the severity is conditional on whether such a shortfall is actually reachable through the concrete `Fungibles` implementation(s) intended to back real PSM instances (e.g., `pallet-assets`), which is asset-implementation-dependent and not conclusively demonstrated against the pallet's actual configuration in this repository.

## Likelihood Explanation
Exploitability requires no privileged actor — any signed user calling the public `mint()` extrinsic could trigger this if the configured external asset's `Fungibles::increase_balance` can legitimately deposit less than requested under `BestEffort` (e.g., ED-related dust loss on a reserve account's first credit, or a backend with asymmetric deposit/withdraw semantics). Whether this condition is reachable in the pallet's real deployment configuration (with `pallet-assets`, whose accounts are typically pre-existing via `inc_providers` at PSM creation) is not established here with a concrete reproduction; the report itself concedes the trigger conditions are backend-implementation-dependent ("any legitimate circumstance") rather than demonstrated against a specific configured asset.

## Recommendation
In `mint()`, read the PSM reserve account's actual external-asset balance before and after the `transfer` call (or use the returned amount if `Fungibles::transfer` is changed to report the real credited delta), and use that observed delta — not the nominal `effective_external`/`internal_equivalent` — to determine `internal_to_user`, `fee`, and the `PsmDebt` increment, mirroring the `get_reserve` guard already present in `redeem()`.

## Proof of Concept
1. Configure or identify a `Fungibles` backend for `external_asset` where a transfer to a fresh/edge-case `psm_account` under `BestEffort` credits less than the requested `amount` (e.g., ED dust loss on first deposit, issuance-capped mint-adjacent semantics, or asymmetric deposit/withdraw accounting).
2. Call `Psm::mint(origin, internal_asset, external_asset, external_amount, max_fee)` with an amount landing in that edge case.
3. Confirm `T::Fungibles::transfer` returns `Ok(effective_external)` at `substrate/frame/psm/src/lib.rs:744-750` while the `psm_account`'s actual external-asset balance increases by less than `effective_external`.
4. Confirm `mint_into` and `PsmDebt::insert` at lines 751-756 still use the full nominal `internal_equivalent`/`internal_to_user` figures, producing internal supply not fully backed by the reserve.
5. A full reproduction requires demonstrating this shortfall concretely against the pallet's actual configured `Fungibles` implementation (e.g., `pallet-assets`), which was not completed within the available investigation — this is the main outstanding gap for confirming exploitability at high confidence.

### Citations

**File:** substrate/frame/psm/src/lib.rs (L719-756)
```rust
			let internal_equivalent =
				Self::external_to_internal(external_amount, ext_decimals, internal_decimals)?;
			ensure!(!internal_equivalent.is_zero(), Error::<T>::AmountTooSmallAfterConversion);
			ensure!(internal_equivalent >= info.min_swap_amount, Error::<T>::BelowMinimumSwap);

			let effective_external =
				Self::internal_to_external(internal_equivalent, ext_decimals, internal_decimals)?;

			let fee_rate = MintingFee::<T>::get(&internal_asset, &external_asset);
			ensure!(fee_rate <= max_fee, Error::<T>::FeeTooHigh);
			let fee = fee_rate.mul_ceil(internal_equivalent);
			let internal_to_user = internal_equivalent.saturating_sub(fee);

			let current_total_psm_debt = Self::total_psm_debt(&internal_asset);
			ensure!(
				current_total_psm_debt.saturating_add(internal_equivalent) <= info.max_debt,
				Error::<T>::ExceedsMaxPsmDebt
			);

			let current_debt = PsmDebt::<T>::get(&internal_asset, &external_asset);
			let max_debt = Self::max_asset_debt(&internal_asset, &external_asset, &info);
			let new_debt = current_debt.saturating_add(internal_equivalent);
			ensure!(new_debt <= max_debt, Error::<T>::ExceedsMaxPsmDebt);

			let psm_account = Self::psm_account(&internal_asset);
			T::Fungibles::transfer(
				external_asset.clone(),
				&who,
				&psm_account,
				effective_external,
				Preservation::Expendable,
			)?;
			T::Fungibles::mint_into(internal_asset.clone(), &who, internal_to_user)?;
			if !fee.is_zero() {
				T::Fungibles::mint_into(internal_asset.clone(), &info.fee_destination, fee)?;
			}

			PsmDebt::<T>::insert(&internal_asset, &external_asset, new_debt);
```

**File:** substrate/frame/psm/src/lib.rs (L848-855)
```rust
			let current_debt = PsmDebt::<T>::get(&internal_asset, &external_asset);
			ensure!(current_debt >= effective_internal_net, Error::<T>::InsufficientReserve);

			let reserve = Self::get_reserve(&internal_asset, &external_asset);
			if reserve < external_out {
				defensive!("PSM reserve is less than expected output amount");
				return Err(Error::<T>::Unexpected.into());
			}
```

**File:** substrate/frame/support/src/traits/tokens/fungibles/regular.rs (L373-386)
```rust
		let _extra = Self::can_withdraw(asset.clone(), source, amount)
			.into_result(preservation != Expendable)?;
		Self::can_deposit(asset.clone(), dest, amount, Extant).into_result()?;
		if source == dest {
			return Ok(amount);
		}

		Self::decrease_balance(asset.clone(), source, amount, BestEffort, preservation, Polite)?;
		// This should never fail as we checked `can_deposit` earlier. But we do a best-effort
		// anyway.
		let _ = Self::increase_balance(asset.clone(), dest, amount, BestEffort);
		Self::done_transfer(asset, source, dest, amount);
		Ok(amount)
	}
```

**File:** substrate/frame/psm/src/mock.rs (L256-260)
```rust
	// Acquire provider refs like `create_psm` does, so the test PSM mirrors a real one.
	frame_system::Pallet::<Test>::inc_providers(&crate::Pallet::<Test>::psm_account(
		&INTERNAL_ASSET_ID,
	));
	frame_system::Pallet::<Test>::inc_providers(&INSURANCE_FUND);
```
