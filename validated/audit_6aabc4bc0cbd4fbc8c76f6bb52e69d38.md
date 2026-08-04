### Title
Documented replay-safety invariant ("era shorter than `LockPeriod`") is never enforced on-chain in `pallet-scarcity`'s `AsScarcity` extension - (File: `substrate/frame/scarcity/src/extension.rs`)

### Summary
`pallet-scarcity`'s module docs state a security-critical invariant: signers of `AsScarcity`-authorized transactions "must sign mortal transactions with an era shorter than `Config::LockPeriod`" so that "every retry after a failure is a fresh signing decision rather than a third-party replay of the old transaction." [1](#0-0)  This is the same failure pattern as the LooksRare M-03 report: an invariant is asserted in documentation/NatSpec, but no corresponding check exists in the implementation that inherits from or consumes it. Just as `OwnableTwoSteps.sol` documented "delay must be set by the inheriting contract" without ever calling `_setupDelayForRenouncingOwnership`, `AsScarcity::validate` never checks the call's mortality era against `T::LockPeriod` — the entire guarantee rests on unenforced caller discipline.

### Finding Description
The extension's `validate` function only checks whether the purse key is *currently* locked (`Locked::<T>::get(&owner)` vs `now`) and whether the NFT `instance`/`state_nonce` match. [2](#0-1)  It never inspects `DispatchInfoOf`/era information to confirm the extrinsic's mortality window is shorter than `T::LockPeriod::get()`, despite the module docs explicitly relying on that relationship to guarantee that a failed-and-locked transaction cannot be replayed by a third party after the lock expires. [3](#0-2) 

The backoff lock itself is keyed only by `owner` and computed purely from `T::UnixTime::now()` and `T::LockPeriod::get()`, with no linkage to, or validation of, the signed transaction's actual era length. [4](#0-3)  Nothing in `prepare` or `post_dispatch_details` cross-checks era vs. lock duration either. [5](#0-4) 

Because Substrate mortal extrinsics can specify an arbitrary era (up to immortal), a signer (or anyone who captures a broadcast signed transaction before it is dispatched) can produce/observe a transaction whose era outlives `LockPeriod`. If that transaction fails once (setting the lock), it remains a valid, gossipable, still-signed extrinsic after the lock naturally expires, at which point anyone can resubmit it to the pool — exactly the "third-party replay" scenario the docs claim is impossible by construction. The `provides` tag `(instance, state_nonce)` only deduplicates within the transaction pool at a given nonce/state and does not prevent inclusion once the lock window elapses, since the NFT is restored to the same `instance`/`state_nonce` on failure. [6](#0-5) [7](#0-6) 

### Impact Explanation
This breaks the documented "fresh signing decision" guarantee for a public, feeless, unprivileged dispatch path (`transfer`/`burn` via `AsScarcity`). [8](#0-7)  An NFT holder who signed a transfer/burn, had it fail once, and later changed their mind (without moving the NFT to bump `state_nonce`) can have that stale authorization executed anyway by any third party who retains a copy of the old signed transaction, once the exponential-backoff lock naturally expires. This is unauthorized execution of an origin-bound action against the signer's later intent, and it affects real value (NFT/purse ownership transfer, deposit-holding collection consideration flows layered on top of `pallet-scarcity`). It matches the "public underpriced work" / "unauthorized execution" category since the extension is explicitly feeless and designed for autonomous public retry.

### Likelihood Explanation
This requires no privileged actor, admin, validator, or relayer — only an ordinary signed account choosing (or a bystander retaining) a mortal era longer than `LockPeriod`, which is entirely within a normal user's control and is never rejected by validation. Since nothing in `validate`, `prepare`, or `post_dispatch_details` checks the era against `LockPeriod`, the guard the documentation promises simply does not exist, making this trivially reachable whenever `LockPeriod` is short relative to allowed mortal eras (up to 65535 blocks) or when an immortal transaction is used.

### Recommendation
Enforce the documented invariant in code rather than relying on caller discipline: in `AsScarcity::validate`, inspect the extrinsic's era (via the transaction's mortality data passed to the extension, or by requiring/validating `frame_system::CheckMortality`'s era bound) and reject (`TransactionValidityError`) any `AsScarcity`-authorized call whose era is not strictly shorter than `T::LockPeriod::get()`. Alternatively, bind the failed-dispatch lock's replay protection directly on-chain, e.g., by additionally invalidating the specific extrinsic (not just the purse's `state_nonce`) so a lock expiry cannot resurrect a stale signed payload.

### Proof of Concept
1. Configure a runtime with `LockPeriod = 60` (seconds) for `pallet-scarcity`.
2. Owner `A` holds NFT `N` at `state_nonce = 5` and signs a mortal `transfer(to: B)` transaction with a long era (e.g., era covering thousands of blocks, well beyond the time equivalent of 60s), authorized via `AsScarcity::AsNft { instance: N, state_nonce: 5 }`.
3. Cause this transaction to fail once at dispatch time (e.g., a same-block race makes `B`'s purse occupied at dispatch, tripping the dispatch-time check even though validate-time passed) — `post_dispatch_details` restores the NFT to `A` and sets `Locked::<T>::insert(A, LockInfo { retries: 1, until: now + LockPeriod })`. [9](#0-8) 
4. `A` decides not to retry the transfer (no longer wants to send to `B`) and does not move the NFT again, so `state_nonce` stays `5`.
5. After `LockPeriod` (60s) elapses, `Locked::<T>::get(A)` no longer blocks validation. [10](#0-9) 
6. Any third party who retained the original signed transaction (still within its long era) rebroadcasts it; `validate` passes because instance/state_nonce still match and the lock has expired, and the transfer to `B` executes without `A`'s current consent — contradicting the documented "every retry is a fresh signing decision" guarantee. [11](#0-10)

### Citations

**File:** substrate/frame/scarcity/src/lib.rs (L41-53)
```rust
//! successor reject an unwanted or unaffordable collection while allowing the runtime to choose
//! how storage consideration is implemented.
//!
//! Footprints count logical records and their encoded payloads rather than exact trie keys and
//! hash prefixes. A runtime can account for backend overhead in its per-record base price and
//! calibrate its byte price to the desired storage policy.
//!
//! Cleanup proceeds from leaves to roots so every call remains bounded. The collection owner
//! force-burns live instances (or holders burn their own), removes item metadata, deletes empty
//! item definitions, removes collection metadata, and finally deletes the empty collection.
//! Instance metadata is bounded and removed automatically on burn. Allocated identifiers are
//! never reused.
//!
```

**File:** substrate/frame/scarcity/src/lib.rs (L71-76)
```rust
//! [`AsScarcity`](extension::AsScarcity) replaces the signed origin before ordinary account checks,
//! so an NFT-only purse does not need a System account. Failed dispatch restores the NFT and
//! temporarily locks the purse key; after the lock expires, the same signed transaction may be
//! submitted again if its NFT state is still current. Callers must sign mortal transactions with
//! an era shorter than [`Config::LockPeriod`] so that retrying is always a fresh signing
//! decision; see the [replay and mortality rules](extension#replay-and-mortality).
```

**File:** substrate/frame/scarcity/src/extension.rs (L163-173)
```rust
	fn failed_dispatch_lock(previous: Option<LockInfo>) -> LockInfo {
		let retries = previous.map(|lock| lock.retries.saturating_add(1)).unwrap_or(1);
		let exponent = retries.saturating_sub(1);
		let multiplier = 2u64.saturating_pow(u32::from(exponent).min(63));
		LockInfo {
			retries,
			until: T::UnixTime::now()
				.as_secs()
				.saturating_add(multiplier.saturating_mul(T::LockPeriod::get())),
		}
	}
```

**File:** substrate/frame/scarcity/src/extension.rs (L196-252)
```rust
	fn validate(
		&self,
		mut origin: <T as frame_system::Config>::RuntimeOrigin,
		call: &<T as frame_system::Config>::RuntimeCall,
		_info: &DispatchInfoOf<<T as frame_system::Config>::RuntimeCall>,
		_len: usize,
		_self_implicit: Self::Implicit,
		_inherited_implication: &impl Implication,
		_source: TransactionSource,
	) -> ValidateResult<Self::Val, <T as frame_system::Config>::RuntimeCall> {
		let transfer_to = match call.is_sub_type() {
			Some(Call::<T>::transfer { to }) => Some(to),
			Some(Call::<T>::burn {}) => None,
			_ => return Ok((ValidTransaction::default(), Val::NotUsing, origin)),
		};
		let Some(AsScarcityInfo::AsNft { instance, state_nonce }) = self.0.as_ref() else {
			return Ok((ValidTransaction::default(), Val::NotUsing, origin));
		};

		let Some(frame_system::Origin::<T>::Signed(owner)) = origin.as_system_ref() else {
			return Err(CustomInvalidity::OriginToAsNftMustBeSigned.into());
		};
		let owner = owner.clone();
		let now = T::UnixTime::now().as_secs();
		if let Some(lock) = Locked::<T>::get(&owner) {
			if lock.until > now {
				return Err(CustomInvalidity::NftTemporarilyLocked.into());
			}
		}
		let nft = NftsByOwner::<T>::get(&owner).ok_or(CustomInvalidity::NoNft)?;
		if nft.instance != *instance || nft.state_nonce != *state_nonce {
			return Err(CustomInvalidity::NftStateMismatch.into());
		}
		if let Some(to) = transfer_to {
			// Pre-validate the destination so ordinary user error is rejected at the pool and
			// never reaches dispatch, where a failure triggers the backoff lock. The
			// dispatch-time checks remain for genuine same-block races. Mirrors coinage's
			// `validate_transfer` pattern. Burns have no destination checks.
			if to == &owner {
				return Err(CustomInvalidity::TransferToSelf.into());
			}
			if NftsByOwner::<T>::contains_key(to) {
				return Err(CustomInvalidity::DestinationOccupied.into());
			}
		}
		let priority = now.saturating_sub(nft.last_moved).min(T::MaxTransferPriority::get());
		let validity = ValidTransaction::with_tag_prefix("Scarcity")
			.and_provides((nft.instance, nft.state_nonce))
			.priority(priority)
			.into();
		origin.set_caller_from(Origin::Nft { owner: owner.clone(), nft });
		Ok((
			validity,
			Val::UsingNft { owner, instance: *instance, state_nonce: *state_nonce },
			origin,
		))
	}
```

**File:** substrate/frame/scarcity/src/extension.rs (L254-299)
```rust
	fn prepare(
		self,
		val: Self::Val,
		_origin: &<T as frame_system::Config>::RuntimeOrigin,
		_call: &<T as frame_system::Config>::RuntimeCall,
		_info: &DispatchInfoOf<<T as frame_system::Config>::RuntimeCall>,
		_len: usize,
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
