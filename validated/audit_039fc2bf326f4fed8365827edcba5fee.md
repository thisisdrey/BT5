## Analog Found

The external report's core broken invariant is: **a state-transition function that must complete atomically (burn escrowed collateral to finalize a lifecycle event) can be permanently blocked by an independent, unrelated freeze/block flag on the token being burned, leaving the position stuck forever with no fallback path.**

The exact same pattern exists in `pallet-nft-fractionalization`'s `unify` extrinsic, which is the redemption path that returns a locked NFT to its owner in exchange for burning 100% of the corresponding fungible "fraction" asset.

### Title
Fractionalized NFT Can Be Permanently Locked If Its Backing Fraction-Asset Account Is Frozen or Blocked - (`substrate/frame/nft-fractionalization/src/lib.rs`)

### Summary
`fractionalize` locks an NFT via `T::Nfts::disable_transfer` and mints a corresponding `pallet-assets` fungible token representing fractional ownership. The only way to unlock the NFT is `unify`, which requires burning the full fraction supply from the caller's account via `T::Assets::burn_from`. If that account's `pallet-assets` `AccountStatus` is set to `Frozen` or `Blocked` (an independent administrative action, analogous to `RWA` pausing/blacklisting in the original report), `burn_from` reverts, `do_unlock_nft` never executes, and the NFT stays locked in `disable_transfer` state indefinitely, exactly mirroring the "trapped" pattern in the source report.

### Finding Description
`unify` is defined in [1](#0-0) . It calls `Self::do_burn_asset`, which invokes `T::Assets::burn_from(asset_id, account, amount, Expendable, Exact, Polite)` as shown in [2](#0-1) . Only after this burn succeeds does the code call `Self::do_unlock_nft(nft_collection_id, nft_id, &beneficiary)`, which re-enables transfer and moves the NFT back to the beneficiary — see [3](#0-2) .

The burn path in `pallet-assets` checks the account's `AccountStatus` before allowing any debit. `can_decrease` returns `WithdrawConsequence::Frozen` when the asset itself is frozen, and the account-level check (`AccountStatus::Frozen` or `AccountStatus::Blocked`, both of which `is_frozen()` returns true for) similarly blocks debits, as shown by the `AccountStatus` definition and its `is_frozen`/`is_blocked` helpers at [4](#0-3)  and exercised by tests such as `transferring_from_frozen_account_should_not_work` and `transferring_from_blocked_account_should_not_work` at [5](#0-4)  and [6](#0-5) . The asset-level freeze check in `can_decrease` is at [7](#0-6) .

Because the `pallet-assets` freezer/admin role is entirely independent from `pallet-nft-fractionalization`, any account that ends up frozen or blocked on the fraction asset (whether by the asset's own `freezer`/`admin`, by a `Freezer` implementation the runtime plugs in, or by the account simply never being un-frozen) can never again call `unify` successfully for that NFT. There is no alternate unlock path in the pallet: `NftToAsset` storage keeps the NFT permanently disabled for transfer, and `do_burn` in `pallet-nfts` also independently checks `T::Locker::is_locked` and refuses to burn a locked item, as shown by `Error::<T, I>::ItemLocked` in [8](#0-7)  and reproduced in the test where a fractionalized NFT cannot be burned by its owner: [9](#0-8) .

### Impact Explanation
The underlying NFT — potentially a high-value RWA-style or unique asset — becomes permanently non-transferable and non-burnable once its fraction-asset holder account is frozen/blocked, with no governance-independent recovery route inside the pallet. This is a permanent user-fund/asset lock consistent with the "permanent user-fund or bridge-state lock" impact class, arising purely from the interaction of two otherwise-independent pallets' public entry points (`fractionalize`/`unify` and `pallet-assets` freeze/block state), not from any admin action being the "root cause" exploited maliciously — the freeze is a normal, expected `pallet-assets` state that this pallet fails to account for.

### Likelihood Explanation
Freezing/blocking an account is a standard, low-privilege-adjacent operation available to any asset's configured `freezer`/`admin` role in `pallet-assets` deployments (e.g. via `freeze`/`block` calls), and does not require any interaction with `pallet-nft-fractionalization` itself. Any runtime that composes these two pallets (as intended by design, since `nft-fractionalization` mints its assets through the generic `fungibles::Mutate`/`Create` traits typically backed by `pallet-assets`) is exposed. No malicious peer, collator, validator, or leaked key is required — only ordinary use of a documented `pallet-assets` administrative feature on an account that also happens to hold fractionalized-NFT tokens.

### Recommendation
- Decouple `unify`'s success from being fully gated on a strict, non-`Force` burn: allow `pallet-nft-fractionalization` to use a `Force`/privileged burn path (e.g. `Fortitude::Force`) that ignores freezer-imposed restrictions, since burning to reclaim the NFT does not disadvantage the account holder.
- Alternatively, provide a governance/root-only rescue extrinsic to re-enable NFT transfer/unlock in this permanently-stuck scenario, decoupled from the frozen asset state.
- Document and test the interaction where `Assets::freeze`/`Assets::block` is applied to a fraction-holding account to explicitly verify `unify` still succeeds or fails gracefully with a recoverable path.

### Proof of Concept
1. Owner calls `Nfts::mint` then `NftFractionalization::fractionalize(nft_collection_id, nft_id, asset_id, beneficiary, fractions)` — NFT transfer becomes disabled, `beneficiary` receives 100% of `fractions` as a `pallet-assets` asset (see [10](#0-9) ).
2. The asset's `freezer`/admin (any account with that role for `asset_id`) calls `Assets::freeze(origin, asset_id, beneficiary)` or `Assets::block(origin, asset_id, beneficiary)`, setting `beneficiary`'s `AccountStatus` to `Frozen`/`Blocked` (behavior demonstrated in [5](#0-4) ).
3. `beneficiary` calls `NftFractionalization::unify(nft_collection_id, nft_id, asset_id, beneficiary)`.
4. `do_burn_asset` → `T::Assets::burn_from` reverts with `Error::<T, I>::Frozen`/`TokenError::Blocked` because `can_decrease` detects the frozen/blocked account status.
5. `do_unlock_nft` is never reached; the NFT remains locked (`disable_transfer`) forever, and the fraction tokens remain non-burnable from that account, with no other path in the pallet to recover the NFT.

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

**File:** substrate/frame/nft-fractionalization/src/lib.rs (L341-349)
```rust
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

**File:** substrate/frame/assets/src/types.rs (L154-173)
```rust
/// The status of an asset account.
#[derive(Clone, Encode, Decode, Eq, PartialEq, Debug, MaxEncodedLen, TypeInfo)]
pub enum AccountStatus {
	/// Asset account can receive and transfer the assets.
	Liquid,
	/// Asset account cannot transfer the assets.
	Frozen,
	/// Asset account cannot receive and transfer the assets.
	Blocked,
}
impl AccountStatus {
	/// Returns `true` if frozen or blocked.
	pub fn is_frozen(&self) -> bool {
		matches!(self, AccountStatus::Frozen | AccountStatus::Blocked)
	}
	/// Returns `true` if blocked.
	pub fn is_blocked(&self) -> bool {
		matches!(self, AccountStatus::Blocked)
	}
}
```

**File:** substrate/frame/assets/src/tests.rs (L851-864)
```rust
#[test]
fn transferring_from_blocked_account_should_not_work() {
	build_and_execute(|| {
		assert_ok!(Assets::force_create(RuntimeOrigin::root(), 0, 1, true, 1));
		assert_ok!(Assets::mint(RuntimeOrigin::signed(1), 0, 1, 100));
		assert_eq!(Assets::balance(0, 1), 100);
		assert_ok!(Assets::block(RuntimeOrigin::signed(1), 0, 1));
		// behaves as frozen when transferring from blocked
		assert_noop!(Assets::transfer(RuntimeOrigin::signed(1), 0, 2, 50), Error::<Test>::Frozen);
		assert_ok!(Assets::thaw(RuntimeOrigin::signed(1), 0, 1));
		assert_ok!(Assets::transfer(RuntimeOrigin::signed(1), 0, 2, 50));
		assert_ok!(Assets::transfer(RuntimeOrigin::signed(2), 0, 1, 50));
	});
}
```

**File:** substrate/frame/assets/src/tests.rs (L1037-1051)
```rust
fn transferring_from_frozen_account_should_not_work() {
	build_and_execute(|| {
		assert_ok!(Assets::force_create(RuntimeOrigin::root(), 0, 1, true, 1));
		assert_ok!(Assets::mint(RuntimeOrigin::signed(1), 0, 1, 100));
		assert_ok!(Assets::mint(RuntimeOrigin::signed(1), 0, 2, 100));
		assert_eq!(Assets::balance(0, 1), 100);
		assert_eq!(Assets::balance(0, 2), 100);
		assert_ok!(Assets::freeze(RuntimeOrigin::signed(1), 0, 2));
		// can transfer to `2`
		assert_ok!(Assets::transfer(RuntimeOrigin::signed(1), 0, 2, 50));
		// cannot transfer from `2`
		assert_noop!(Assets::transfer(RuntimeOrigin::signed(2), 0, 1, 25), Error::<Test>::Frozen);
		assert_eq!(Assets::balance(0, 1), 50);
		assert_eq!(Assets::balance(0, 2), 150);
	});
```

**File:** substrate/frame/assets/src/functions.rs (L176-192)
```rust
	pub(super) fn can_decrease(
		id: T::AssetId,
		who: &T::AccountId,
		amount: T::Balance,
		keep_alive: bool,
	) -> WithdrawConsequence<T::Balance> {
		use WithdrawConsequence::*;
		let details = match Asset::<T, I>::get(&id) {
			Some(details) => details,
			None => return UnknownAsset,
		};
		if details.supply.checked_sub(&amount).is_none() {
			return Underflow;
		}
		if details.status == AssetStatus::Frozen {
			return Frozen;
		}
```

**File:** substrate/frame/nfts/src/features/create_delete_item.rs (L201-217)
```rust
	/// Burns the specified item with the given `collection`, `item`, and `with_details`.
	///
	/// # Errors
	///
	/// This function returns a dispatch error in the following cases:
	/// - If the collection ID is invalid ([`UnknownCollection`](crate::Error::UnknownCollection)).
	/// - If the item is locked ([`ItemLocked`](crate::Error::ItemLocked)).
	pub fn do_burn(
		collection: T::CollectionId,
		item: T::ItemId,
		with_details: impl FnOnce(&ItemDetailsFor<T, I>) -> DispatchResult,
	) -> DispatchResult {
		ensure!(!T::Locker::is_locked(collection, item), Error::<T, I>::ItemLocked);
		ensure!(
			!Self::has_system_attribute(&collection, &item, PalletAttributes::TransferDisabled)?,
			Error::<T, I>::ItemLocked
		);
```

**File:** substrate/frame/nft-fractionalization/src/tests.rs (L129-137)
```rust
		// owner can't burn an already fractionalized NFT
		assert_noop!(
			Nfts::burn(RuntimeOrigin::signed(account(1)), nft_collection_id, nft_id),
			DispatchError::Module(ModuleError {
				index: 4,
				error: [12, 0, 0, 0],
				message: Some("ItemLocked")
			})
		);
```
