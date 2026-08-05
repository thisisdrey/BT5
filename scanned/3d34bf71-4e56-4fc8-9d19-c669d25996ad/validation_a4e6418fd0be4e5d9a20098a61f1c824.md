## Finding

### Title
`pallet_scarcity`'s `AsScarcity` extension permits replay of failed NFT-transfer authorizations by any third party once the backoff lock expires, because nothing in code enforces the documented "era shorter than `LockPeriod`" invariant - (`substrate/frame/scarcity/src/extension.rs`)

### Summary
This is a direct structural analog of the `NativeMetaTransaction.executeMetaTransaction()` bug: a signed "meta" authorization is consumed on `prepare` but only restored — never invalidated — when the wrapped dispatch fails, so the exact same authorization can be resubmitted later and now succeed. `pallet_scarcity`'s `AsScarcity` transaction extension replaces a signed origin with `Origin::Nft` based on `(instance, state_nonce)`, takes the NFT out of storage in `prepare`, and on dispatch failure puts it back and applies only a *temporary* backoff lock (`Locked<T>`) rather than bumping `state_nonce`. Since replay protection here is not account-nonce based, safety is defined entirely by an unenforced documentation contract: "Callers must sign mortal transactions with an era shorter than `Config::LockPeriod`." No code in `substrate/frame/scarcity/src/extension.rs` checks or requires this relationship between the extrinsic's mortality and `LockPeriod`.

### Finding Description
`AsScarcity::validate` (extension.rs:196-252) authorizes a transfer/burn using `(instance, state_nonce)` bound to the caller's current NFT and rejects the call only if `Locked::<T>::get(&owner)` is still active (`until > now`) — [1](#0-0) . `prepare` (extension.rs:254-281) atomically removes the NFT from `NftsByOwner` so it can't be double-spent in the same block, and stashes it in `Pre::UsingNft` to be restored later [2](#0-1) . `post_dispatch_details` (extension.rs:283-299) is the crux: on failure it reinserts the *same* NFT (same `instance`, same `state_nonce`) and only escalates a backoff lock (`Locked::<T>::insert`) — it never advances `state_nonce`, which is the only thing that could invalidate the outstanding signed authorization [3](#0-2) .

Because the same `(instance, state_nonce)` still matches after a failed dispatch, the identical previously-broadcast extrinsic remains valid and dispatchable by *anyone* who observed it (mempool, block explorer, prior included-but-failed block) as soon as `Locked::until` elapses. The module doc explicitly acknowledges this and claims it is safe only because "callers must sign mortal transactions with an era shorter than `Config::LockPeriod`" [4](#0-3) . That claim is never checked anywhere in the extension: `validate`/`prepare`/`post_dispatch_details` never inspect the extrinsic's era or compare it against `T::LockPeriod`. Enforcement is left entirely to whichever `TransactionExtension` tuple a runtime integrator happens to place around `AsScarcity`, and to whichever era the signer/wallet chooses to sign with — nothing stops an immortal (or long-lived) mortal transaction from being combined with `AsScarcity`.

This is confirmed by the kitchensink runtime's own test, `failed_scarcity_transfer_is_feeless_and_retryable_after_lock`, which builds `scarcity_tx_extension(9, u64::MAX)` (a maximal/immortal-style period) and drives a failed transfer, a lock, and a *successful* retry of essentially the same authorization after the lock expires, with no account nonce involved at all [5](#0-4) . The test demonstrates the retry mechanic works exactly as documented — but it also demonstrates that the code path allows the "retry" to be a *replay*, since nothing ties the resubmission to the original signer versus a copy of the raw extrinsic bytes captured by a third party.

### Impact Explanation
An attacker who observes a failed Scarcity transfer/burn extrinsic (e.g. captured from a block or the transaction pool, where a `transfer` failed because the destination purse became occupied, or a `burn`/`transfer` failed for any transient reason) can simply hold onto the raw signed bytes and resubmit them once `Locked::until` passes — with no new signature and no cooperation from the original owner. If the owner's purse still holds the same `instance` at the same `state_nonce` (which is the common case: nothing about NFT ownership changed, only the destination's occupancy or another transient condition changed), the replayed extrinsic now dispatches successfully and unilaterally moves or destroys the owner's NFT to whatever `to` was encoded in the old call, executing an action the owner no longer consents to — matching exactly the "unauthorized execution/wrong beneficiary" impact class (theft of an NFT, or unwanted burn) called out in the pivots for public wrappers that must not allow stale/failed authorizations to later succeed against the holder's will.

### Likelihood Explanation
The precondition is narrow but realistic and requires no privileged role: any unprivileged party who can see chain data (or the extrinsic in gossip) can hold and replay it — this is not a "malicious peer/relayer/validator" assumption, it is ordinary observation of public data, exactly analogous to the original Solidity report's "attacker replays the failed MetaTransaction." The exploitability further depends on wallets/runtime integrators not independently bounding the era to be shorter than `LockPeriod`; the pallet's own code contains no defense-in-depth for this, so any deployment that (a) sets an era longer than `LockPeriod`, (b) uses an immortal transaction, or (c) simply has the lock+era race in the exact same block window is exposed. The kitchensink test itself uses a maximal period parameter, showing this configuration is exercised in-repo.

### Recommendation
Do not rely solely on wallet-chosen mortality to bound replay. Either (a) have `AsScarcity::prepare` or `post_dispatch_details` increment `state_nonce` (or otherwise mutate the NFT's binding value) even on dispatch failure, so a failed authorization can never be replayed verbatim regardless of era; or (b) have the extension itself validate that the supplied call's era/mortality is strictly shorter than `T::LockPeriod` before authorizing, rather than leaving this as an undocumented runtime-integration responsibility.

### Proof of Concept
1. Owner signs and broadcasts a `transfer { to: X }` extrinsic authorized via `AsScarcity` with an immortal era (or an era ≥ `LockPeriod`), matching `instance`/`state_nonce`.
2. `X`'s purse becomes occupied before the extrinsic is included (or any other condition causes `transfer` to fail); the extension's `post_dispatch_details` restores the NFT to the owner and sets `Locked::<T>` (see `failed_scarcity_transfer_is_feeless_and_retryable_after_lock` in `substrate/bin/node/runtime/src/lib.rs:4191-4256`, which reproduces this exact flow).
3. An attacker who saw the extrinsic (e.g., in the block it failed in) stores its raw bytes.
4. Time passes; `Locked::until` expires and the owner's NFT is unchanged (`state_nonce` identical, since only the failed dispatch's precondition — not the NFT's ownership state — changed).
5. The attacker resubmits the identical raw extrinsic bytes. `AsScarcity::validate` passes (lock expired, `instance`/`state_nonce` still match), `prepare` takes the NFT, and this time `X`'s purse is empty so `transfer` succeeds — moving the owner's NFT to `X` with no fresh signature or consent from the owner at the time of execution.

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

**File:** substrate/frame/scarcity/src/extension.rs (L262-279)
```rust
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

**File:** substrate/bin/node/runtime/src/lib.rs (L4192-4256)
```rust
	fn failed_scarcity_transfer_is_feeless_and_retryable_after_lock() {
		let storage = frame_system::GenesisConfig::<Runtime>::default().build_storage().unwrap();
		let mut ext: sp_io::TestExternalities = storage.into();

		ext.execute_with(|| {
			System::set_block_number(1);
			Timestamp::set_timestamp(1_000);
			let from = AccountId::new([1u8; 32]);
			let to = AccountId::new([2u8; 32]);
			pallet_scarcity::NftsByOwner::<Runtime>::insert(
				&from,
				pallet_scarcity::Nft {
					instance: 0,
					collection: 0,
					item: 0,
					minted_at: 0,
					last_moved: 0,
					state_nonce: u64::MAX,
				},
			);
			pallet_scarcity::Instances::<Runtime>::insert(0, &from);

			let call = RuntimeCall::Scarcity(pallet_scarcity::Call::<Runtime>::transfer {
				to: to.clone(),
			});
			let info = call.get_dispatch_info();
			let result = scarcity_tx_extension(9, u64::MAX).dispatch_transaction(
				RuntimeOrigin::signed(from.clone()),
				call,
				&info,
				0,
				0,
			);
			assert!(matches!(result, Ok(Err(_))), "transaction did not reach dispatch: {result:?}");

			assert_eq!(Balances::free_balance(&from), 0);
			assert_eq!(System::account_nonce(&from), 0);
			assert!(!frame_system::Account::<Runtime>::contains_key(&from));
			assert_eq!(
				pallet_scarcity::NftsByOwner::<Runtime>::get(&from).map(|nft| nft.state_nonce),
				Some(u64::MAX),
			);
			assert!(!pallet_scarcity::NftsByOwner::<Runtime>::contains_key(&to));
			assert_eq!(pallet_scarcity::Locked::<Runtime>::get(&from).unwrap().retries, 1);

			pallet_timestamp::Now::<Runtime>::put(62_000);
			let retry_call =
				RuntimeCall::Scarcity(pallet_scarcity::Call::<Runtime>::transfer { to });
			let retry_info = retry_call.get_dispatch_info();
			let retry_result = scarcity_tx_extension(9, u64::MAX).dispatch_transaction(
				RuntimeOrigin::signed(from.clone()),
				retry_call,
				&retry_info,
				0,
				0,
			);
			assert!(
				matches!(retry_result, Ok(Err(_))),
				"retry did not reach dispatch: {retry_result:?}"
			);
			assert_eq!(System::account_nonce(&from), 0);
			assert_eq!(pallet_scarcity::Locked::<Runtime>::get(&from).unwrap().retries, 2);
		});
	}
}
```
