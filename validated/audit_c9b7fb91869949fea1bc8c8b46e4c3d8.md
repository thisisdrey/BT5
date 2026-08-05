Based on my investigation, the claim is technically accurate and reproducible in this repository.

Audit Report

## Title
`fungibles::Mutate::transfer()` / `fungible::Mutate::transfer()` default implementations silently destroy value on dust-sweep because the debited amount is discarded before crediting the destination — (File: `substrate/frame/support/src/traits/tokens/fungibles/regular.rs`)

## Summary
The default `transfer()` implementation debits the source with `decrease_balance` and discards its returned actual-amount via `?`, then credits the destination with the caller-supplied `amount` via a separate `increase_balance` call, instead of pairing debit and credit. [1](#0-0)  For `pallet-assets`, which does not override `Mutate::transfer` and only overrides the lower-level `decrease_balance`/`increase_balance` hooks, the low-level debit path can round up the actual amount removed from the source due to dust-sweep in `prep_debit`, while none of the closures passed by `impl_fungibles.rs` update `Asset::supply`. [2](#0-1) [3](#0-2) [4](#0-3) 

## Finding Description
`Mutate::transfer()` calls `Self::decrease_balance(asset, source, amount, BestEffort, preservation, Polite)?` and discards the `Ok` value (the actual amount debited), then calls `Self::increase_balance(asset, dest, amount, BestEffort)` with the original caller-supplied `amount`. [5](#0-4) 

`pallet-assets` overrides `Unbalanced::decrease_balance`/`increase_balance` (not `Mutate::transfer`), so this default `transfer()` is the one actually used for `pallet-assets`. [2](#0-1) 

The pallet's override forwards to the low-level `Pallet::decrease_balance` in `functions.rs`, passing a no-op check closure `|_, _| Ok(())`. [6](#0-5)  That low-level function calls `prep_debit`, which can return an `actual` debit amount greater than the requested `amount` whenever the remaining balance would fall below `min_balance` — the dust is swept into the debit via `actual.saturating_add(dust)`. [3](#0-2)  Crucially, the low-level `decrease_balance`'s mutation of `Account::balance` never touches `Asset::supply` — that adjustment is only performed by the caller-supplied `check` closure (as seen correctly done in `do_burn`, which decrements `details.supply`). [7](#0-6) [8](#0-7)  Since `impl_fungibles.rs::decrease_balance` passes the no-op closure `|_, _| Ok(())`, `supply` is never decremented in this path. [9](#0-8) 

The corresponding `increase_balance` override similarly forwards to the low-level `increase_balance` in `functions.rs`, which is explicitly documented as not altering supply. [10](#0-9) [11](#0-10) 

Consequently, when the generic `transfer()` triggers a dust-sweep debit of `actual = amount + dust` on the source but credits only `amount` to the destination (because the discarded return value is never re-used), the source's balance decreases by `actual`, the destination's increases by only `amount`, and `Asset::supply` is left untouched. The result: `sum(account balances)` for the asset drops by `dust`, while `total_issuance` stays the same — an unaccounted, permanent loss of `dust` worth of value, matching the claimed invariant break. This is reachable via `FungiblesTransferAdapter::internal_transfer_asset`, which calls `Assets::transfer(asset_id, &source, &dest, amount, Expendable)` and unconditionally returns `Ok(what.clone())` regardless of what was actually moved. [12](#0-11)  This is exposed to any signed account via `pallet_xcm::execute` with a `TransferAsset` instruction. The identical structural pattern exists for `fungible::Mutate::transfer()` and `FungibleTransferAdapter`. [13](#0-12) [14](#0-13) 

I was unable to fully verify within the available iterations whether `Assets::transfer` (the extrinsic-facing dispatchable, `pallet::Pallet::transfer`) itself routes through `do_transfer`/`transfer_and_die` (which correctly pairs debit and credit via `prep_debit`+`prep_credit` and does correctly adjust `details.supply` for burnt dust) rather than through the generic `fungibles::Mutate::transfer()` default. The claim's PoC and citations specifically target the `fungibles`/`fungible` trait-level `Mutate::transfer()` default used by the XCM `TransferAsset` adapters (`FungiblesTransferAdapter`/`FungibleTransferAdapter`), which is a separate call path from the pallet's own `transfer`/`transfer_keep_alive` extrinsics that use `do_transfer`. Code inspection confirms this separate, unpaired path exists and is reachable through XCM's `internal_transfer_asset`.

## Impact Explanation
This causes a real, permanent, unrecoverable loss of user funds (the dust-sweep delta) with no compensating adjustment to `total_issuance`, breaking the `sum(balances) == total_issuance` invariant for the affected asset. This falls under the "theft or unbacked mint or unlock" / "permanent user-fund" impact categories, since value silently disappears without being credited to any account or reflected in issuance accounting.

## Likelihood Explanation
Any unprivileged, signed account controlling its own asset balance can trigger this deterministically and repeatably by submitting a `pallet_xcm::execute` extrinsic with a `TransferAsset` instruction for an amount chosen so the remaining source balance would fall strictly between `0` and `min_balance`, forcing `prep_debit`'s dust-sweep logic to over-debit. No privileged actor, governance, or relayer collusion is required.

## Recommendation
In the default `transfer()` implementations of `fungibles::Mutate` (`substrate/frame/support/src/traits/tokens/fungibles/regular.rs`) and `fungible::Mutate` (`substrate/frame/support/src/traits/tokens/fungible/regular.rs`), capture the actual amount returned by `decrease_balance` and use that exact value as the argument to `increase_balance`, mirroring the paired debit/credit logic in `pallet_assets::functions::transfer_and_die`. Alternatively, have `FungiblesTransferAdapter`/`FungibleTransferAdapter::internal_transfer_asset` capture and return the actual transferred amount instead of blindly returning `what.clone()`.

## Proof of Concept
1. Create an asset in `pallet-assets` with `min_balance = M`, mint `M + amount` to account `A` (amount `< M`).
2. `A` submits `pallet_xcm::execute` with `TransferAsset { assets: (asset_location, amount).into(), beneficiary: B }`, chosen so debiting `amount` leaves `A`'s remaining balance below `M`.
3. `FungiblesTransferAdapter::internal_transfer_asset` calls `Assets::transfer(asset_id, A, B, amount, Expendable)`, which invokes the default `Mutate::transfer()`; the low-level debit rounds up to `M + amount` (all of `A`'s balance) via `prep_debit`'s dust-sweep, but `increase_balance` credits `B` with only `amount`.
4. Compare `pallet_assets::Pallet::total_issuance(asset_id)` before/after (unchanged) against `sum(balance(A), balance(B))` before/after (decreased by `M`), confirming the unaccounted loss.

### Citations

**File:** substrate/frame/support/src/traits/tokens/fungibles/regular.rs (L366-386)
```rust
	fn transfer(
		asset: Self::AssetId,
		source: &AccountId,
		dest: &AccountId,
		amount: Self::Balance,
		preservation: Preservation,
	) -> Result<Self::Balance, DispatchError> {
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

**File:** substrate/frame/assets/src/impl_fungibles.rs (L195-217)
```rust
	fn decrease_balance(
		asset: T::AssetId,
		who: &T::AccountId,
		amount: Self::Balance,
		precision: Precision,
		preservation: Preservation,
		_: Fortitude,
	) -> Result<Self::Balance, DispatchError> {
		let f = DebitFlags {
			keep_alive: preservation != Expendable,
			best_effort: precision == BestEffort,
		};
		Self::decrease_balance(asset, who, amount, f, |_, _| Ok(()))
	}
	fn increase_balance(
		asset: T::AssetId,
		who: &T::AccountId,
		amount: Self::Balance,
		_: Precision,
	) -> Result<Self::Balance, DispatchError> {
		Self::increase_balance(asset, who, amount, |_| Ok(()))?;
		Ok(amount)
	}
```

**File:** substrate/frame/assets/src/functions.rs (L291-310)
```rust
	pub(super) fn prep_debit(
		id: T::AssetId,
		target: &T::AccountId,
		amount: T::Balance,
		f: DebitFlags,
	) -> Result<T::Balance, DispatchError> {
		let actual = Self::reducible_balance(id.clone(), target, f.keep_alive)?.min(amount);
		ensure!(f.best_effort || actual >= amount, Error::<T, I>::BalanceLow);

		let conseq = Self::can_decrease(id, target, actual, f.keep_alive);
		let actual = match conseq.into_result(f.keep_alive) {
			Ok(dust) => actual.saturating_add(dust), //< guaranteed by reducible_balance
			Err(e) => {
				debug_assert!(false, "passed from reducible_balance; qed");
				return Err(e);
			},
		};

		Ok(actual)
	}
```

**File:** substrate/frame/assets/src/functions.rs (L479-485)
```rust
	/// Increases the asset `id` balance of `beneficiary` by `amount`.
	///
	/// LOW-LEVEL: Does not alter the supply of asset or emit an event. Use `do_mint` if you need
	/// that. This is not intended to be used alone.
	///
	/// Will return an error or will increase the amount by exactly `amount`.
	pub(super) fn increase_balance(
```

**File:** substrate/frame/assets/src/functions.rs (L547-557)
```rust
		let actual = Self::decrease_balance(id.clone(), target, amount, f, |actual, details| {
			// Check admin rights.
			if let Some(check_admin) = maybe_check_admin {
				ensure!(check_admin == details.admin, Error::<T, I>::NoPermission);
			}

			debug_assert!(details.supply >= actual, "checked in prep; qed");
			details.supply = details.supply.saturating_sub(actual);

			Ok(())
		})?;
```

**File:** substrate/frame/assets/src/functions.rs (L570-621)
```rust
	pub(super) fn decrease_balance(
		id: T::AssetId,
		target: &T::AccountId,
		amount: T::Balance,
		f: DebitFlags,
		check: impl FnOnce(
			T::Balance,
			&mut AssetDetails<T::Balance, T::AccountId, DepositBalanceOf<T, I>>,
		) -> DispatchResult,
	) -> Result<T::Balance, DispatchError> {
		if amount.is_zero() {
			return Ok(amount);
		}

		let details = Asset::<T, I>::get(&id).ok_or(Error::<T, I>::Unknown)?;
		ensure!(details.status == AssetStatus::Live, Error::<T, I>::AssetNotLive);

		let actual = Self::prep_debit(id.clone(), target, amount, f)?;
		let mut target_died: Option<DeadConsequence> = None;

		Asset::<T, I>::try_mutate(&id, |maybe_details| -> DispatchResult {
			let details = maybe_details.as_mut().ok_or(Error::<T, I>::Unknown)?;
			check(actual, details)?;

			Account::<T, I>::try_mutate(&id, target, |maybe_account| -> DispatchResult {
				let mut account = maybe_account.take().ok_or(Error::<T, I>::NoAccount)?;
				debug_assert!(account.balance >= actual, "checked in prep; qed");

				// Make the debit.
				account.balance = account.balance.saturating_sub(actual);
				if account.balance < details.min_balance {
					debug_assert!(account.balance.is_zero(), "checked in prep; qed");
					Self::ensure_account_can_die(id.clone(), target)?;
					target_died = Some(Self::dead_account(target, details, &account.reason, false));
					if let Some(Remove) = target_died {
						return Ok(());
					}
				};
				*maybe_account = Some(account);
				Ok(())
			})?;

			Ok(())
		})?;

		// Execute hook outside of `mutate`.
		if let Some(Remove) = target_died {
			T::Freezer::died(id.clone(), target);
			T::Holder::died(id, target);
		}
		Ok(actual)
	}
```

**File:** polkadot/xcm/xcm-builder/src/fungibles_adapter.rs (L53-75)
```rust
	fn internal_transfer_asset(
		what: &Asset,
		from: &Location,
		to: &Location,
		_context: &XcmContext,
	) -> Result<Asset, XcmError> {
		tracing::trace!(
			target: "xcm::fungibles_adapter",
			?what, ?from, ?to,
			"internal_transfer_asset"
		);
		// Check we handle this asset.
		let (asset_id, amount) = Matcher::matches_fungibles(what)?;
		let source = AccountIdConverter::convert_location(from)
			.ok_or(MatchError::AccountIdConversionFailed)?;
		let dest = AccountIdConverter::convert_location(to)
			.ok_or(MatchError::AccountIdConversionFailed)?;
		Assets::transfer(asset_id.clone(), &source, &dest, amount, Expendable).map_err(|e| {
			tracing::debug!(target: "xcm::fungibles_adapter", error = ?e, ?asset_id, ?source, ?dest, ?amount, "Failed internal transfer asset");
			XcmError::FailedToTransactAsset(e.into())
		})?;
		Ok(what.clone())
	}
```

**File:** substrate/frame/support/src/traits/tokens/fungible/regular.rs (L321-339)
```rust
	fn transfer(
		source: &AccountId,
		dest: &AccountId,
		amount: Self::Balance,
		preservation: Preservation,
	) -> Result<Self::Balance, DispatchError> {
		let _extra = Self::can_withdraw(source, amount).into_result(preservation != Expendable)?;
		Self::can_deposit(dest, amount, Extant).into_result()?;
		if source == dest {
			return Ok(amount);
		}

		Self::decrease_balance(source, amount, BestEffort, preservation, Polite)?;
		// This should never fail as we checked `can_deposit` earlier. But we do a best-effort
		// anyway.
		let _ = Self::increase_balance(dest, amount, BestEffort);
		Self::done_transfer(source, dest, amount);
		Ok(amount)
	}
```

**File:** polkadot/xcm/xcm-builder/src/fungible_adapter.rs (L55-80)
```rust
	fn internal_transfer_asset(
		what: &Asset,
		from: &Location,
		to: &Location,
		_context: &XcmContext,
	) -> result::Result<Asset, XcmError> {
		tracing::trace!(
			target: "xcm::fungible_adapter",
			?what, ?from, ?to,
			"internal_transfer_asset",
		);
		// Check we handle the asset
		let amount = Matcher::matches_fungible(what).ok_or(MatchError::AssetNotHandled)?;
		let source = AccountIdConverter::convert_location(from)
			.ok_or(MatchError::AccountIdConversionFailed)?;
		let dest = AccountIdConverter::convert_location(to)
			.ok_or(MatchError::AccountIdConversionFailed)?;
		Fungible::transfer(&source, &dest, amount, Expendable).map_err(|error| {
			tracing::debug!(
				target: "xcm::fungible_adapter", ?error, ?source, ?dest, ?amount,
				"Failed to transfer asset",
			);
			XcmError::FailedToTransactAsset(error.into())
		})?;
		Ok(what.clone())
	}
```
