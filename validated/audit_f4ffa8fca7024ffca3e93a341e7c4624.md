### Title
Re-fractionalizing an already-locked NFT overwrites `NftToAsset` and permanently orphans/locks the first depositor's deposit and asset holders' claim - ([File: substrate/frame/nft-fractionalization/src/lib.rs])

### Summary
`pallet-nft-fractionalization::fractionalize()` only checks that the caller is the current owner of the NFT (`T::Nfts::owner(...) == who`), but `do_lock_nft` merely calls `disable_transfer` — it does **not** change the NFT's owner. Because ownership never moves to the pallet, the original owner remains the recorded owner even after fractionalization. Nothing in `fractionalize()` checks whether `NftToAsset` already has an entry for `(nft_collection_id, nft_id)`. This mirrors the external report's core flaw exactly: a state-mutating entrypoint validates only "does the caller still satisfy a simple ownership/ACL check" and never checks "is there already an active claim (Lien / fractionalization) on this exact resource."

### Finding Description
`fractionalize()` in `substrate/frame/nft-fractionalization/src/lib.rs` (lines 220-263):
- Verifies `nft_owner == who` via `T::Nfts::owner`.
- Calls `Self::do_lock_nft(...)`, which only calls `T::Nfts::disable_transfer` (see `do_lock_nft`, lines 336-339) — the NFT's `owner` field is untouched.
- Unconditionally `NftToAsset::<T>::insert((nft_collection_id, nft_id), Details { asset: asset_id, ... })` (lines 249-252), overwriting any prior entry without checking `NftToAsset::<T>::get(...).is_none()`.

Because the original owner is still the "owner" of record after the first fractionalization, that same owner can call `fractionalize()` a second time on the same `(nft_collection_id, nft_id)`:
1. `T::Currency::hold(&HoldReason::Fractionalized.into(), &nft_owner, deposit)` is invoked again — a second deposit hold.
2. A brand-new `asset_id`/beneficiary/fractions is created and minted.
3. `NftToAsset::insert` silently overwrites the first `Details { asset: old_asset_id, asset_creator, deposit }` with the new one.

The old `Details` record — the only pointer that lets holders of the *first* fractional asset redeem the NFT via `unify()` — is gone. `unify()` (lines 283-317) does `NftToAsset::try_mutate_exists(...).ok_or(Error::<T>::NftNotFractionalized)?` and then `ensure!(details.asset == asset_id, Error::<T>::IncorrectAssetId)`; since the stored `details.asset` is now the *second* asset, holders of the *first* fractional token can never satisfy this check. The deposit held for the first fractionalization is never released (release only happens inside `unify()`, keyed to the surviving record), and the NFT can only ever be returned to whichever party matches the second (surviving) `Details` record.

This is a direct structural analog to the `withdrawNftWithInterest()` bug: the resource (NFT) is treated as available for a fresh claim ("Lien"/fractionalization) purely because a shallow, unrelated check (ownership/transferability) passes, while a second competing claim on the exact same resource silently displaces the first, causing the first claimant's counterpart-holders to lose access to both the underlying asset and their locked deposit.

### Impact Explanation
- Permanent user-fund lock: the first depositor's `Deposit` (reserved via `HoldReason::Fractionalized`) is never released because the only code path that releases it (`unify()`) requires the — now overwritten — original `Details` record.
- Loss of redemption rights: holders of the first minted fractional asset (`asset_id` #1) can never redeem their tokens for the NFT; `unify()` will only succeed for the second asset's holders.
- No privileged actor, governance, relayer, or malicious node is required — the double `fractionalize()` call can be made by the same account that owns/controls the NFT (which, since ownership is unaffected by `disable_transfer`, is trivially still allowed).

### Likelihood Explanation
High: `disable_transfer` (used by `do_lock_nft`) does not transfer ownership, so the `nft_owner == who` check in `fractionalize()` continues to pass for the same account after the NFT has already been fractionalized. No additional privilege or race condition against other users is needed — the NFT owner alone can trigger the double-fractionalize sequence in two ordinary signed extrinsics.

### Recommendation
In `fractionalize()`, before locking and inserting into `NftToAsset`, add a check that the NFT is not already fractionalized:
```rust
ensure!(!NftToAsset::<T>::contains_key((nft_collection_id, nft_id)), Error::<T>::NftAlreadyFractionalized);
```
Add a corresponding `NftAlreadyFractionalized` variant to `Error<T>`. This mirrors the recommended fix in the external report (checking for an existing active claim before allowing a new one on the same resource) rather than only checking the shallow ownership state.

### Proof of Concept
1. Alice owns NFT `(collection=1, item=1)`.
2. Alice calls `fractionalize(1, 1, asset_id=100, beneficiary=Bob, fractions=1000)`.
   - `NftToAsset[(1,1)] = Details { asset: 100, fractions: 1000, asset_creator: Alice, deposit: D }`.
   - Bob now holds 1000 units of asset `100`, entitled to redeem NFT `(1,1)` via `unify`.
   - `do_lock_nft` calls `disable_transfer` only; `T::Nfts::owner(1,1)` is still `Alice`.
3. Alice calls `fractionalize(1, 1, asset_id=200, beneficiary=Carol, fractions=500)` again.
   - The `nft_owner == who` check passes (owner is still Alice).
   - A second deposit hold `D` is taken from Alice.
   - `NftToAsset[(1,1)]` is overwritten: `Details { asset: 200, fractions: 500, asset_creator: Alice, deposit: D }`.
4. Bob calls `unify(1, 1, asset_id=100, beneficiary=Bob)`.
   - `NftToAsset::try_mutate_exists` finds the current (second) record with `asset: 200`.
   - `ensure!(details.asset == asset_id, Error::<T>::IncorrectAssetId)` fails with `IncorrectAssetId`.
   - Bob's 1000 units of asset `100` are now permanently unredeemable for the NFT; the first deposit is permanently stuck (never released). [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** substrate/frame/nft-fractionalization/src/lib.rs (L149-157)
```rust
	/// Keeps track of the corresponding NFT ID, asset ID and amount minted.
	#[pallet::storage]
	pub type NftToAsset<T: Config> = StorageMap<
		_,
		Blake2_128Concat,
		(T::NftCollectionId, T::NftId),
		Details<AssetIdOf<T>, AssetBalanceOf<T>, DepositOf<T>, T::AccountId>,
		OptionQuery,
	>;
```

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

**File:** substrate/frame/nft-fractionalization/src/lib.rs (L283-317)
```rust
		pub fn unify(
			origin: OriginFor<T>,
			nft_collection_id: T::NftCollectionId,
			nft_id: T::NftId,
			asset_id: AssetIdOf<T>,
			beneficiary: AccountIdLookupOf<T>,
		) -> DispatchResult {
			let who = ensure_signed(origin)?;
			let beneficiary = T::Lookup::lookup(beneficiary)?;

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

				Self::deposit_event(Event::NftUnified {
					nft_collection: nft_collection_id,
					nft: nft_id,
					asset: asset_id,
					beneficiary,
				});

				Ok(())
			})
		}
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
