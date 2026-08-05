### Title
`AsScarcity` transaction extension fails to bound extrinsic mortality against `LockPeriod`, allowing third-party replay of a previously-failed NFT transfer/burn - (File: `substrate/frame/scarcity/src/extension.rs`)

### Summary
The Astaria bug is caused by an operation (`settleLiquidatorNFTClaim`) that releases an asset to a new party without finalizing the accompanying state that guards against reuse (the auction/lien link), letting a stale authorization be exploited again. `pallet-scarcity`'s `AsScarcity` transaction extension has the same class of defect: it documents an invariant that is never actually enforced on-chain, so a previously *failed* signed transfer/burn can be replayed by anyone (not just the original signer) once a short backoff lock expires, even though the wider transaction is still mortal/valid.

### Finding Description
`pallet-scarcity` deliberately does not use `frame_system`'s account-nonce replay protection for NFT-authorized calls (`transfer`/`burn`); it substitutes a "consumption-on-use" scheme driven by `(InstanceId, state_nonce)` and a temporary failure lock [1](#0-0) .

`validate()` only rejects a transaction while `Locked::<T>::get(&owner).until > now`; it never inspects or bounds the extrinsic's mortality/era [2](#0-1) . `prepare()` removes the NFT from `NftsByOwner` unconditionally so the call can execute, and `post_dispatch_details()` restores the exact same `Nft` (same `instance`/`state_nonce`) plus a `LockInfo` computed purely from `T::LockPeriod::get()` when dispatch fails [3](#0-2) .

The safety of this design is explicitly stated to depend on callers self-limiting their transaction era to be shorter than `LockPeriod`, so that "a failed transaction can never re-enter a block" [1](#0-0) . That assumption is never checked anywhere in `validate`, `prepare`, or `post_dispatch_details`. Nothing in the extension inspects `CheckMortality`/`Era` to enforce it. In the shipped development runtime, `LockPeriod = ConstU64<60>` (60 seconds) [4](#0-3) , which is far shorter than typical wallet-chosen mortal-era windows (commonly minutes), so the "era ends before lock expires" property routinely does not hold in practice.

Because the purse account never uses a System account nonce for these authorizations, the on-chain replay-prevention for a *failed* extrinsic rests entirely on the lock. Once the lock (60s) expires while the extrinsic's era (e.g., several minutes) is still valid and the `(instance, state_nonce)` tag is unchanged (nothing else moved the NFT), the identical previously-broadcast, previously-failed extrinsic becomes valid again and can be resubmitted and executed by any third party who observed it — not just the original signer.

### Impact Explanation
This breaks the "public dispatch wrapper must not... undercharge/bypass" and origin-integrity guarantees for the `transfer`/`burn` entry points: an unprivileged third party can force execution of a stale, already-failed NFT transfer/burn without any fresh authorization from the purse holder, at a time the holder never chose (and may have abandoned the intent to move the asset). This is unauthorized execution of a signed instruction outside its intended single-shot semantics, directly contradicting the pallet's own documented invariant ("every retry after a failure is a fresh signing decision") — the same "operation completes without finalizing/void-ing the linking state that should prevent reuse" defect class as the Astaria report.

### Likelihood Explanation
Triggerable by any observer of network traffic/mempool/block explorer with no special privileges, no governance/admin action, and no malicious validator/relayer assumption. The trigger conditions (destination momentarily occupied, or any other transient dispatch-time failure such as `DestinationOccupied`/`SelfTransfer`/`NftStateMismatch` re-check at execution time) are ordinary races explicitly acknowledged in the code comments ("dispatch-time checks remain for genuine same-block races") [5](#0-4) . Given the runtime's very short `LockPeriod` (60s) relative to normal mortal-era windows, the window in which replay is possible is realistically wide.

### Recommendation
Enforce the documented invariant on-chain rather than relying on caller discipline: in `AsScarcity::validate`, derive or bound the extrinsic's remaining mortality (via the `Implication`/mortality info available to transaction extensions) and reject transactions whose era extends past `T::LockPeriod::get()`. Alternatively, bind the `provides`/`requires` tag or a single-use marker to something that is never restored identically after a failed dispatch (e.g., increment `state_nonce` or record a used-nonce even on failure), so a byte-identical retry of a failed extrinsic can never validate again without a fresh signature.

### Proof of Concept
1. Holder `H` owns NFT `X` at purse account `H`, `state_nonce = 0`. `H` signs a mortal `transfer(to = Y)` with an era of, e.g., 100 blocks (~600s at 6s blocks) and broadcasts it.
2. The extrinsic is included in a block but dispatch fails at execution time because `Y` happens to hold another NFT at that moment (`DestinationOccupied`, a "genuine same-block race" per the code comments). `post_dispatch_details` restores `NftsByOwner::<T>::insert(H, nft)` with the same `instance`/`state_nonce`, and sets `Locked::<T>::insert(H, LockInfo{ until: now + 60 })` [6](#0-5) .
3. `H` decides not to retry (e.g., they now want to keep `X`), and does nothing further.
4. ~60 seconds later the lock expires while the original extrinsic's ~600s era is still valid; `Y`'s NFT slot has meanwhile become free.
5. A third party who saved the original signed extrinsic bytes (from mempool/explorer) resubmits it verbatim. `validate()` passes (`Locked` expired, `instance`/`state_nonce` unchanged), `prepare()` consumes the NFT again, and this time the dispatch succeeds — forcibly transferring `X` from `H` to `Y` with no new signature or consent from `H`.

### Citations

**File:** substrate/frame/scarcity/src/extension.rs (L41-53)
```rust
//! # Replay and mortality
//!
//! Purse authorization is not account-nonce-based: a signed NFT transaction stays valid for as
//! long as its purse still holds the named instance at the named state nonce. Two rules bound
//! stale intent, exactly as in Coinage:
//!
//! * Callers must sign **mortal** transactions with an era shorter than [`Config::LockPeriod`]. A
//!   successful move invalidates every outstanding authorization by incrementing the state nonce,
//!   but an unexecuted transaction is otherwise replayable by anyone who has seen it until its era
//!   expires.
//! * Because the era ends before the shortest failure lock does, a failed transaction can never
//!   re-enter a block: every retry after a failure is a fresh signing decision rather than a
//!   third-party replay of the old transaction.
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

**File:** substrate/frame/scarcity/src/extension.rs (L219-228)
```rust
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
```

**File:** substrate/frame/scarcity/src/extension.rs (L229-240)
```rust
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
```

**File:** substrate/frame/scarcity/src/extension.rs (L254-298)
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
```

**File:** substrate/bin/node/runtime/src/lib.rs (L589-589)
```rust
	type LockPeriod = ConstU64<60>;
```
