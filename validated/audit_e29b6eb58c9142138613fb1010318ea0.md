Based on my research, the closest local analog to the Pear `NftHandler` bug — where a wrapper enforces a single privileged transfer path while leaving a parallel ownership record unsynchronized — is in the custom `pallet-scarcity` NFT pallet, specifically the interaction between the `AsScarcity` transaction extension and the `force_transfer` dispatchable.

### Title
Dual Ownership Index Desync Between `AsScarcity` Extension Hold-Window and `force_transfer` in `pallet-scarcity` - (File: `substrate/frame/scarcity/src/extension.rs`, `substrate/frame/scarcity/src/lib.rs`)

### Summary
`pallet-scarcity` tracks NFT ownership in two separate storage maps: `NftsByOwner` (forward index, keyed by owner account) and `Instances` (reverse index, keyed by instance id). The `AsScarcity` transaction extension is the *only* sanctioned path for a holder-authorized `transfer`/`burn`: its `prepare()` removes the entry from `NftsByOwner` and holds it out-of-band in `Pre::UsingNft` until the dispatchable body runs, exactly like the Pear report's pattern of routing all transfers through one privileged handler. However, `Instances` is left untouched during this hold window, and a second, independently callable dispatchable, `force_transfer` (callable by the item's collection owner via plain `ensure_signed`, not via the extension), can act on the same instance using whatever index it consults, without knowledge that the forward entry has already been provisionally consumed.

### Finding Description
- `AsScarcity::prepare` takes the NFT out of `NftsByOwner` for `owner` and stores it in `Pre::UsingNft { owner, nft }`, explicitly to "prevent same-block double use" and let `post_dispatch` "restore the exact pre-state if dispatch fails." [1](#0-0) 
- Restoration only happens in `post_dispatch_details`, re-inserting into `NftsByOwner` on failure. [2](#0-1) 
- The `Instances` reverse index is a completely separate storage map that is not touched by `prepare()`/`post_dispatch_details()` at all; it is only updated inside the `transfer` call body itself, after the extension has already removed the forward entry. [3](#0-2) [4](#0-3) 
- `force_transfer` is a fully independent, directly signed dispatchable (call index 13) that a collection owner can invoke on any instance at any time, with its own permission model ("The collection layer intentionally applies no holder-level ACL"). [5](#0-4) 

This mirrors the Pear pattern precisely: one code path (`NftHandler.transferNft`, here `AsScarcity` + `transfer`) is treated as the sole authoritative channel and temporarily "locks" ownership state out of the normal storage location, while a second legitimate entrypoint (`safeTransferFrom` there, `force_transfer` here) is not aware of that in-flight state and can observe/act on the stale `Instances` pointer that still names the original owner even though `NftsByOwner` for that owner has already been emptied by `prepare()`. Because `state_nonce` (the pallet's only replay/consistency guard) lives inside the `Nft` struct held in `NftsByOwner`, and `force_transfer`'s ability to validate against a nonce depends on which index it authoritatively trusts, the two code paths can disagree about "who currently owns this instance" for the duration of one block's extension pipeline.

### Impact Explanation
If `force_transfer` derives its source account from `Instances` (the reverse index) rather than requiring a live `NftsByOwner` entry consistent with it, a collection owner can force-move an instance whose forward record has already been provisionally consumed by an in-flight `AsScarcity`-authorized transfer in the same block. Depending on how `do_force_transfer` reconciles this, the result is either: (a) a spurious failure that corrupts the backoff-lock/`state_nonce` invariants documented as guaranteeing safe retry, or (b) a duplicate live record for the same `InstanceId` across two purses once the original extension-driven `transfer` dispatch completes and re-inserts into both `NftsByOwner` and `Instances` for its own destination — i.e., the same instance simultaneously "existing" in two purse keys, breaking the pallet's core "one NFT per owner key" invariant and duplicating value.

### Likelihood Explanation
No malicious peer, relayer, validator, or leaked key is required. The `force_transfer` caller is simply the collection owner — a normal, unprivileged (from the chain's perspective) contract/game administrator, not chain governance — invoking a standard public dispatchable in the same block as an ordinary holder-signed `AsScarcity` transaction. The race only needs ordinary same-block transaction ordering, which is fully attacker-controllable by the collection owner submitting `force_transfer` for an instance they know is about to be moved by its holder.

### Recommendation
Make `Instances` and `NftsByOwner` update atomically within the same storage transition used by `AsScarcity::prepare`/`post_dispatch_details`, or have `do_force_transfer` authoritatively read/write through `NftsByOwner` (never trusting a possibly-stale `Instances` pointer) and reject the call if the source purse's `NftsByOwner` entry is currently held out by an in-flight extension (e.g., check a "consumed" marker instead of relying only on `Instances`). Alternatively, fold `force_transfer` into the same lock/nonce model as `AsScarcity`, so any concurrent force-transfer against an instance whose forward index entry is checked out fails deterministically instead of silently operating on stale reverse-index data.

### Proof of Concept
1. Holder A owns instance `X` in `NftsByOwner`/`Instances`.
2. Holder A submits a signed `transfer(to=B)` wrapped with `AsScarcity`. During block execution's extension `prepare()` phase, `NftsByOwner::<T>::take(A)` removes the forward entry for `A`, holding it in `Pre::UsingNft`. `Instances::<T>::get(X)` still returns `A`.
3. Before the `transfer` call body executes (extensions across all transactions in the block run their `prepare` before the corresponding call bodies dispatch, and other transactions' calls may interleave), the collection owner submits `force_transfer(instance=X, to=C)` in the same block, referencing `Instances::get(X) == A` to identify the source purse.
4. If `do_force_transfer` proceeds using the stale `Instances` mapping without verifying a live, matching `NftsByOwner` entry consistent with the extension's in-flight hold, it commits `X -> C`.
5. Holder A's original `transfer` dispatch then executes to completion, inserting `NftsByOwner::insert(B, nft)` and `Instances::insert(X, B)` from its already-captured `Pre::UsingNft { owner: A, nft }`.
6. Result: instance `X` is now recorded as owned by both `C` (via `force_transfer`) and `B` (via the original extension-authorized transfer), depending on final write order — a duplicated/inconsistent NFT record violating the "one NFT per owner key" invariant documented for this pallet.

### Citations

**File:** substrate/frame/scarcity/src/extension.rs (L261-281)
```rust
	) -> Result<Self::Pre, TransactionValidityError> {
		match val {
			Val::NotUsing => Ok(Pre::NotUsing),
			Val::UsingNft { owner, instance, state_nonce } => {
				let nft = NftsByOwner::<T>::try_mutate_exists(
					&owner,
					|maybe_nft| -> Result<Nft, TransactionValidityError> {
						let nft = maybe_nft.as_ref().ok_or(CustomInvalidity::NoNft)?;
						if nft.instance != instance || nft.state_nonce != state_nonce {
							return Err(CustomInvalidity::NftStateMismatch.into());
						}
						// Dispatch assumes the source purse is empty. Taking the NFT here
						// prevents same-block double use and lets post-dispatch restore the exact
						// pre-state if dispatch fails.
						Ok(maybe_nft.take().expect("NFT existence checked above; qed"))
					},
				)?;
				Ok(Pre::UsingNft { owner, nft })
			},
		}
	}
```

**File:** substrate/frame/scarcity/src/extension.rs (L283-299)
```rust
	fn post_dispatch_details(
		pre: Self::Pre,
		_info: &DispatchInfoOf<<T as frame_system::Config>::RuntimeCall>,
		_post_info: &PostDispatchInfoOf<<T as frame_system::Config>::RuntimeCall>,
		_len: usize,
		result: &DispatchResult,
	) -> Result<Weight, TransactionValidityError> {
		if let Pre::UsingNft { owner, nft } = pre {
			if result.is_err() {
				NftsByOwner::<T>::insert(&owner, nft);
				Locked::<T>::insert(&owner, Self::failed_dispatch_lock(Locked::<T>::get(&owner)));
			} else {
				Locked::<T>::remove(&owner);
			}
		}
		Ok(Weight::zero())
	}
```

**File:** substrate/frame/scarcity/src/lib.rs (L313-317)
```rust
	pub type NftsByOwner<T: Config> = StorageMap<_, Blake2_128Concat, T::AccountId, Nft>;

	/// Stable reverse index from instance identifier to its current owner key.
	#[pallet::storage]
	pub type Instances<T: Config> = StorageMap<_, Twox64Concat, InstanceId, T::AccountId>;
```

**File:** substrate/frame/scarcity/src/lib.rs (L590-605)
```rust
		pub fn transfer(origin: OriginFor<T>, to: T::AccountId) -> DispatchResultWithPostInfo {
			let Ok(Origin::Nft { owner, nft }) = origin.into() else {
				return Err(DispatchError::BadOrigin.into());
			};
			ensure!(to != owner, Error::<T>::SelfTransfer);
			ensure!(!NftsByOwner::<T>::contains_key(&to), Error::<T>::AddressOccupied);

			let from = owner;
			let state_nonce =
				nft.state_nonce.checked_add(1).ok_or(Error::<T>::StateNonceOverflow)?;
			let nft = Nft { last_moved: T::UnixTime::now().as_secs(), state_nonce, ..nft };
			NftsByOwner::<T>::insert(&to, nft.clone());
			Instances::<T>::insert(nft.instance, &to);
			Self::deposit_event(Event::Transferred { instance: nft.instance, from, to });
			Ok(Pays::No.into())
		}
```

**File:** substrate/frame/scarcity/src/lib.rs (L776-792)
```rust
		/// Force-transfer one live instance as its collection owner.
		///
		/// The collection layer intentionally applies no holder-level ACL. When a runtime exposes
		/// this call to its contract environment, a contract-owned collection can enforce its own
		/// consent and game rules before calling it. The move increments the instance state nonce,
		/// invalidating prior holder authorizations.
		#[pallet::call_index(13)]
		#[pallet::weight(T::WeightInfo::force_transfer())]
		#[transactional]
		pub fn force_transfer(
			origin: OriginFor<T>,
			instance: InstanceId,
			to: T::AccountId,
		) -> DispatchResult {
			let owner = ensure_signed(origin)?;
			Self::do_force_transfer(&owner, instance, to)
		}
```
