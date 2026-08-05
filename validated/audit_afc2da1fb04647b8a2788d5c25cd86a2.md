Audit Report

## Title
Fractionalized NFT Can Be Permanently Locked If Its Backing Fraction-Asset Account Is Frozen or Blocked - (File: substrate/frame/nft-fractionalization/src/lib.rs)

## Summary
`fractionalize` locks an NFT via `T::Nfts::disable_transfer` and mints a corresponding `pallet-assets` fungible token representing fractional ownership [1](#0-0) . The only unlock path, `unify`, requires burning the full fraction balance via `T::Assets::burn_from` before `do_unlock_nft` is ever called [2](#0-1) [3](#0-2) . If the caller's `pallet-assets` account status for that asset is `Frozen` or `Blocked`, the burn permanently reverts and the NFT stays locked in `disable_transfer` state with no recovery path in the pallet.

## Finding Description
`unify` executes `Self::do_burn_asset(asset_id, &who, details.fractions)` before `Self::do_unlock_nft(...)`, inside a single atomic `try_mutate_exists` closure [4](#0-3) . `do_burn_asset` calls `T::Assets::burn_from(asset_id, account, amount, Expendable, Exact, Polite)` [5](#0-4) .

In `pallet-assets`, the withdrawal consequence check `can_decrease` explicitly returns `Frozen` when `account.status.is_frozen()` is true, and `AccountStatus::is_frozen()` matches both `Frozen` and `Blocked` variants [6](#0-5) . This check is unconditional in the current code path used by `burn_from`/`do_burn_asset` — there is no `Fortitude::Force` bypass wired through `pallet-nft-fractionalization`'s call (it uses `Fortitude::Polite`), so a frozen/blocked account can never successfully burn its fraction tokens.

Because `unify`'s `try_mutate_exists` closure returns early on the `do_burn_asset` error (via `?`), `NftToAsset` storage retains the original entry (the `take()` result is dropped since the closure returns `Err`, meaning the storage is *not* removed — `try_mutate_exists` restores state on error), and `do_unlock_nft` is never reached, leaving the NFT permanently in `disable_transfer` state. There is no alternate extrinsic in this pallet to re-enable transfer or reclaim the NFT independent of a successful burn.

## Impact Explanation
This matches the "permanent user-fund or bridge-state lock" impact class: an underlying NFT becomes permanently non-transferable via the pallet's own extrinsics once its fraction-asset holder account is frozen or blocked by the asset's `freezer`/`admin` role — a state entirely independent of, and not accounted for by, `pallet-nft-fractionalization`. The lock is total and has no governance-independent recovery path within the pallet's code.

## Likelihood Explanation
Freezing/blocking an account is a normal, documented `pallet-assets` capability available to any asset's configured `freezer`/`admin` (via `Assets::freeze`/`Assets::block`), and requires no privileged access to `pallet-nft-fractionalization` itself. Any runtime composing these two pallets as intended (fractional assets minted through the generic `fungibles::Mutate`/`Create` traits, typically backed by `pallet-assets`) is exposed whenever a fraction-holding account becomes frozen/blocked for any reason, whether administrative or otherwise, making the condition realistically reachable and repeatable.

## Recommendation
- Have `pallet-nft-fractionalization::do_burn_asset` use a privileged/force burn semantics (e.g., `Fortitude::Force`) so that reclaiming a locked NFT is not blocked by freezer-imposed restrictions on the caller's account, since burning to redeem does not disadvantage the account holder.
- Alternatively, add a root/governance-only rescue extrinsic to unlock the NFT independent of the frozen fraction-asset state.
- Add regression tests exercising `Assets::freeze`/`Assets::block` on a fraction-holding account followed by `unify`, verifying it either succeeds or fails with a documented, recoverable error path.

## Proof of Concept
1. Owner calls `Nfts::mint`, then `NftFractionalization::fractionalize(nft_collection_id, nft_id, asset_id, beneficiary, fractions)` [7](#0-6)  — NFT transfer is disabled and `beneficiary` receives the full fraction supply.
2. The asset's admin/freezer calls `Assets::freeze(origin, asset_id, beneficiary)` or `Assets::block(origin, asset_id, beneficiary)`, setting `beneficiary`'s `AccountStatus` to `Frozen`/`Blocked`.
3. `beneficiary` calls `NftFractionalization::unify(nft_collection_id, nft_id, asset_id, beneficiary)`.
4. `do_burn_asset` → `T::Assets::burn_from` fails because `can_decrease` returns `Frozen` for the account [6](#0-5) .
5. `do_unlock_nft` is never reached; `NftToAsset` storage entry persists, and the NFT remains permanently locked with no recovery extrinsic available in the pallet.

### Citations

**File:** substrate/frame/nft-fractionalization/src/lib.rs (L220-263)
```rust
		pub fn fractionalize(
			origin: OriginFor<T>,
			nft_collection_id: T::NftCollectionId,
			nft_id: T::NftId,
			asset_id: AssetIdOf<T>,
			beneficiary: AccountIdLookupOf<T>,
			fractions: AssetBalanceOf<T>,
		) -> DispatchResult {
			let who = ensure_signed(origin)?;
			let beneficiary = T::Lookup::lookup(beneficiary)?;

			let nft_owner =
				T::Nfts::owner(&nft_collection_id, &nft_id).ok_or(Error::<T>::NftNotFound)?;
			ensure!(nft_owner == who, Error::<T>::NoPermission);

			let pallet_account = Self::get_pallet_account();
			let deposit = T::Deposit::get();
			T::Currency::hold(&HoldReason::Fractionalized.into(), &nft_owner, deposit)?;
			Self::do_lock_nft(nft_collection_id, nft_id)?;
			Self::do_create_asset(asset_id.clone(), pallet_account.clone())?;
			Self::do_mint_asset(asset_id.clone(), &beneficiary, fractions)?;
			Self::do_set_metadata(
				asset_id.clone(),
				&who,
				&pallet_account,
				&nft_collection_id,
				&nft_id,
			)?;

			NftToAsset::<T>::insert(
				(nft_collection_id, nft_id),
				Details { asset: asset_id.clone(), fractions, asset_creator: nft_owner, deposit },
			);

			Self::deposit_event(Event::NftFractionalized {
				nft_collection: nft_collection_id,
				nft: nft_id,
				fractions,
				asset: asset_id,
				beneficiary,
			});

			Ok(())
		}
```

**File:** substrate/frame/nft-fractionalization/src/lib.rs (L293-300)
```rust
			NftToAsset::<T>::try_mutate_exists((nft_collection_id, nft_id), |maybe_details| {
				let details = maybe_details.take().ok_or(Error::<T>::NftNotFractionalized)?;
				ensure!(details.asset == asset_id, Error::<T>::IncorrectAssetId);

				let deposit = details.deposit;
				let asset_creator = details.asset_creator;
				Self::do_burn_asset(asset_id.clone(), &who, details.fractions)?;
				Self::do_unlock_nft(nft_collection_id, nft_id, &beneficiary)?;
```

**File:** substrate/frame/nft-fractionalization/src/lib.rs (L366-374)
```rust
		/// Burn tokens from the account.
		fn do_burn_asset(
			asset_id: AssetIdOf<T>,
			account: &T::AccountId,
			amount: AssetBalanceOf<T>,
		) -> DispatchResult {
			T::Assets::burn_from(asset_id.clone(), account, amount, Expendable, Exact, Polite)?;
			T::Assets::start_destroy(asset_id, None)
		}
```

**File:** substrate/frame/assets/src/functions.rs (L199-205)
```rust
		let account = match Account::<T, I>::get(&id, who) {
			Some(a) => a,
			None => return BalanceLow,
		};
		if account.status.is_frozen() {
			return Frozen;
		}
```
