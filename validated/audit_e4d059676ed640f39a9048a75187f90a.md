Based on the code I reviewed, the claim's core assertions are confirmed by the actual repository code:

1. `fractionalize()` checks only `nft_owner == who` via `T::Nfts::owner(...)` and never checks whether `NftToAsset::<T>::get((nft_collection_id, nft_id))` already contains an entry. [1](#0-0) 

2. `do_lock_nft` only calls `T::Nfts::disable_transfer`, which does not change the NFT's `owner` field — ownership transfer only happens in `do_unlock_nft` during `unify()`. [2](#0-1) 

3. `NftToAsset::<T>::insert` unconditionally overwrites any prior `Details` record for the same `(nft_collection_id, nft_id)` key, with no `contains_key` or similar guard beforehand. [3](#0-2) 

4. `unify()` retrieves and removes the *current* `Details` entry via `try_mutate_exists`, and checks `details.asset == asset_id`; since a second fractionalize overwrote the record, only the second asset's ID matches, permanently orphaning the first asset's holders and the first deposit (release only happens inside this same `unify()` call, keyed to whichever record survives). [4](#0-3) 

5. The `owner` lookup used for the ACL check comes from `Item::<T, I>::get(collection, item).map(|a| a.owner)`, confirming that `disable_transfer` (called by `do_lock_nft`) does not touch this field, so the same account remains "owner" after the first fractionalization and can pass the check a second time. [5](#0-4) 

This is a genuine, reachable bug exploitable by any unprivileged NFT owner using only public extrinsics (`fractionalize` called twice), resulting in a second competing claim silently displacing the first, permanently locking the first depositor's funds and orphaning the first fractional-asset holders' redemption rights — matching the "permanent user-fund lock" and "duplicate settlement" impact categories in the gate. The proposed fix (checking `NftToAsset::<T>::contains_key` before allowing fractionalization) is a correct and minimal remediation.

Audit Report

## Title
Re-fractionalizing an already-locked NFT overwrites `NftToAsset` and permanently orphans the first depositor's deposit and first asset holders' redemption rights - (File: substrate/frame/nft-fractionalization/src/lib.rs)

## Summary
`pallet-nft-fractionalization::fractionalize()` validates only that the caller currently equals `T::Nfts::owner(...)`, but `do_lock_nft` (called by `fractionalize`) never transfers NFT ownership — it merely calls `T::Nfts::disable_transfer`. Because ownership is untouched, the original owner can call `fractionalize()` a second time on the same `(nft_collection_id, nft_id)`, causing `NftToAsset::insert` to silently overwrite the first `Details` record with a new one, permanently orphaning the first minted asset's holders and locking the first deposit.

## Finding Description
`fractionalize()` checks `nft_owner == who` and then calls `do_lock_nft`, which only calls `T::Nfts::disable_transfer` — the `owner` field in `pallet_nfts::Item` storage is never modified by this call, as confirmed by the `owner()` implementation which reads `Item::<T,I>::get(collection, item).map(|a| a.owner)`. Consequently, after a first successful `fractionalize()` call, `T::Nfts::owner(&nft_collection_id, &nft_id)` still returns the same account, allowing that same account to call `fractionalize()` again on the identical NFT. The second call takes a new deposit hold, creates a new asset, and unconditionally overwrites `NftToAsset::<T>::insert((nft_collection_id, nft_id), Details{...})` with no prior existence check. The old `Details` record — including the original `asset` id and `deposit` — is discarded. `unify()` retrieves the *current* stored record via `try_mutate_exists` and requires `details.asset == asset_id`; holders of the first-minted asset can never satisfy this check because the stored record now points to the second asset. Since the deposit release also only occurs inside this same code path, keyed to whichever `Details` record survives, the first deposit is never released.

## Impact Explanation
This results in a permanent user-fund lock: the first depositor's held `Deposit` is unrecoverable since only `unify()` releases it, and `unify()` can only succeed against the surviving (second) record. It also causes duplicate/competing settlement over the same underlying NFT, permanently denying the first fractional asset's holders any path to redeem the NFT. No privileged actor is required — the same NFT owner account, using two ordinary signed extrinsics, causes the loss.

## Likelihood Explanation
High. The exploit requires no race condition, no other users' cooperation, and no privilege beyond being the (still-recorded) NFT owner — trivially true after the first fractionalization since `disable_transfer` does not change `owner`. It is fully reproducible by any account holding an NFT and repeating `fractionalize()` twice.

## Recommendation
Add a guard in `fractionalize()` before locking and inserting into `NftToAsset`:
```rust
ensure!(!NftToAsset::<T>::contains_key((nft_collection_id, nft_id)), Error::<T>::NftAlreadyFractionalized);
```
Add a corresponding `NftAlreadyFractionalized` variant to `Error<T>`.

## Proof of Concept
1. Alice owns NFT `(1, 1)`.
2. Alice calls `fractionalize(1, 1, asset_id=100, beneficiary=Bob, fractions=1000)`. `NftToAsset[(1,1)] = Details{asset:100, deposit:D, asset_creator: Alice, ...}`. Bob holds 1000 units of asset 100. `T::Nfts::owner(1,1)` is still Alice (`disable_transfer` doesn't reassign owner).
3. Alice calls `fractionalize(1, 1, asset_id=200, beneficiary=Carol, fractions=500)` again — the `nft_owner == who` check passes; a second deposit `D` is held from Alice; `NftToAsset[(1,1)]` is overwritten to `Details{asset:200, deposit:D, asset_creator: Alice, ...}`.
4. Bob calls `unify(1, 1, asset_id=100, beneficiary=Bob)` — `try_mutate_exists` finds the current record with `asset:200`; `ensure!(details.asset == asset_id, Error::<T>::IncorrectAssetId)` fails. Bob's 1000 units of asset 100 are permanently unredeemable, and the first deposit `D` is permanently stuck.

### Citations

**File:** substrate/frame/nft-fractionalization/src/lib.rs (L231-252)
```rust
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
```

**File:** substrate/frame/nft-fractionalization/src/lib.rs (L293-306)
```rust
			NftToAsset::<T>::try_mutate_exists((nft_collection_id, nft_id), |maybe_details| {
				let details = maybe_details.take().ok_or(Error::<T>::NftNotFractionalized)?;
				ensure!(details.asset == asset_id, Error::<T>::IncorrectAssetId);

				let deposit = details.deposit;
				let asset_creator = details.asset_creator;
				Self::do_burn_asset(asset_id.clone(), &who, details.fractions)?;
				Self::do_unlock_nft(nft_collection_id, nft_id, &beneficiary)?;
				T::Currency::release(
					&HoldReason::Fractionalized.into(),
					&asset_creator,
					deposit,
					BestEffort,
				)?;
```

**File:** substrate/frame/nft-fractionalization/src/lib.rs (L336-349)
```rust
		/// Prevent further transferring of NFT.
		fn do_lock_nft(nft_collection_id: T::NftCollectionId, nft_id: T::NftId) -> DispatchResult {
			T::Nfts::disable_transfer(&nft_collection_id, &nft_id)
		}

		/// Remove the transfer lock and transfer the NFT to the account returning the tokens.
		fn do_unlock_nft(
			nft_collection_id: T::NftCollectionId,
			nft_id: T::NftId,
			account: &T::AccountId,
		) -> DispatchResult {
			T::Nfts::enable_transfer(&nft_collection_id, &nft_id)?;
			T::Nfts::transfer(&nft_collection_id, &nft_id, account)
		}
```

**File:** substrate/frame/nfts/src/impl_nonfungibles.rs (L29-42)
```rust
impl<T: Config<I>, I: 'static> Inspect<<T as SystemConfig>::AccountId> for Pallet<T, I> {
	type ItemId = T::ItemId;
	type CollectionId = T::CollectionId;

	fn owner(
		collection: &Self::CollectionId,
		item: &Self::ItemId,
	) -> Option<<T as SystemConfig>::AccountId> {
		Item::<T, I>::get(collection, item).map(|a| a.owner)
	}

	fn collection_owner(collection: &Self::CollectionId) -> Option<<T as SystemConfig>::AccountId> {
		Collection::<T, I>::get(collection).map(|a| a.owner)
	}
```
