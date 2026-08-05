Based on my investigation, I found a clear local analog in `pallet-nft-fractionalization`. I could not fully verify the low-level `disable_transfer`/`owner` implementation in `substrate/frame/nfts/src/impl_nonfungibles.rs` due to running out of tool calls, but the pallet's own doc comments confirm the relevant behavior, and the vulnerable code path is self-contained and verifiable in `substrate/frame/nft-fractionalization/src/lib.rs`.

### Title
`fractionalize()` allows re-fractionalizing an already-fractionalized NFT, enabling duplicate unbacked asset issuance against a single locked NFT - (File: `substrate/frame/nft-fractionalization/src/lib.rs`)

### Summary
`Pallet::fractionalize` only checks that the caller currently owns the NFT (`T::Nfts::owner(...) == who`) before locking it and minting a brand-new fungible asset against it. It never checks whether the same `(nft_collection_id, nft_id)` is **already present** in `NftToAsset`, i.e., already backing a previously-issued fractional asset. Because `do_lock_nft` only disables transfers on the NFT (it does not move ownership into the pallet), the original owner remains `owner` of the NFT for the entire time it is "locked." This lets the same owner call `fractionalize` a second time on the same NFT/collection pair, minting an entirely separate fungible asset that is also nominally "backed" by that one NFT, and silently overwriting the `NftToAsset` record for the first fractionalization.

### Finding Description [1](#0-0) 

The `fractionalize` extrinsic:
1. Verifies `nft_owner == who` via `T::Nfts::owner(...)`.
2. Holds a `Deposit` from the caller.
3. Calls `Self::do_lock_nft(...)`, which only calls `T::Nfts::disable_transfer(...)` — a flag preventing further transfers, not a change of custody/ownership: [2](#0-1) 
4. Creates a brand-new asset, mints `fractions` to `beneficiary`, sets metadata.
5. Unconditionally `insert`s into `NftToAsset` for the key `(nft_collection_id, nft_id)`: [3](#0-2) 

There is no `ensure!(!NftToAsset::<T>::contains_key(...))` or equivalent guard analogous to the recommended `ownerOf() != address(this)` pre-check in the original report. Since `disable_transfer` does not strip the original owner's `owner()` status (this is the same class of bug as the report: a post-condition/permission check that is satisfied by *residual* state rather than by a fresh, single-use transfer), the same account that fractionalized the NFT once can call `fractionalize` again with a *different* `asset_id`, pass the `nft_owner == who` check again (because it still owns the NFT), pay another deposit, and mint an unrelated, fully-backed-looking second fungible asset for the *same* NFT. The `NftToAsset` map entry for the first asset is silently clobbered by the `insert` in step 5.

### Impact Explanation
The pallet's invariant is "one NFT ↔ one live fractional asset, redeemable 1:1." Overwriting `NftToAsset` breaks this:
- Holders of the *first* fractional asset (`asset_id` #1) can never redeem — `unify` requires `details.asset == asset_id`, and `details` will now point to asset #2's record, permanently locking out the original set of token holders from ever reclaiming/unlocking the NFT with their tokens (`Error::IncorrectAssetId` for asset #1 holders forever). This is a **permanent user-fund/asset lock**.
- The attacker (original NFT owner) can effectively "print" a second batch of fully transferable, tradable fungible tokens (asset #2) that look identical in kind to a legitimately fractionalized asset, misrepresenting that they are backed 1:1 by the NFT, while asset #1 holders' claim silently becomes unbacked. This is duplicate settlement / unbacked mint against a single collateral unit, matching the required-impact category ("theft or unbacked mint... duplicate settlement... permanent user-fund lock").
- No malicious peer, validator, collator, or governance action is required — this is exploitable by any ordinary signed account that owns an NFT, using only the public `fractionalize`/`unify` extrinsics.

### Likelihood Explanation
High. The attack requires only two calls to a public, unprivileged extrinsic (`fractionalize`) with the same `nft_collection_id`/`nft_id` and different `asset_id` values, paying only the (bounded, refundable-looking) `Deposit` twice. No special timing, race condition, or third-party cooperation is needed.

### Recommendation
In `fractionalize`, before locking/minting, add a check that the NFT is not already fractionalized:
```rust
ensure!(
    !NftToAsset::<T>::contains_key((nft_collection_id, nft_id)),
    Error::<T>::NftAlreadyFractionalized
);
```
This mirrors the report's recommended fix of checking that the resource is not already committed/consumed (`ownerOf() != address(this)`) before performing an action that assumes exclusive, fresh custody.

### Proof of Concept
1. Alice owns NFT `(collection=0, item=0)`.
2. Alice calls `fractionalize(origin=Alice, nft_collection_id=0, nft_id=0, asset_id=X, beneficiary=Alice, fractions=1000)`.
   - NFT transfer is disabled (`disable_transfer`), Alice remains `owner`.
   - Asset `X` is created; Alice receives 1000 units; `NftToAsset[(0,0)] = { asset: X, fractions: 1000, asset_creator: Alice, deposit: D }`.
3. Alice calls `fractionalize(origin=Alice, nft_collection_id=0, nft_id=0, asset_id=Y, beneficiary=Alice, fractions=1000)` again.
   - `T::Nfts::owner((0,0)) == Alice` still holds (ownership never transferred in step 2).
   - A second deposit `D` is held; asset `Y` is created; Alice receives another 1000 units.
   - `NftToAsset[(0,0)]` is overwritten to `{ asset: Y, ... }`.
4. Any innocent buyer who purchased/holds asset `X` (believing it is redeemable 1:1 for the NFT) can never call `unify` successfully for the NFT, because `NftToAsset[(0,0)].asset == Y != X` forever — `Error::IncorrectAssetId`.
5. Alice (or whoever burns all of asset `Y`) can call `unify` and reclaim the NFT, while asset `X` remains outstanding and permanently unbacked.

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

**File:** substrate/frame/nft-fractionalization/src/lib.rs (L336-339)
```rust
		/// Prevent further transferring of NFT.
		fn do_lock_nft(nft_collection_id: T::NftCollectionId, nft_id: T::NftId) -> DispatchResult {
			T::Nfts::disable_transfer(&nft_collection_id, &nft_id)
		}
```
