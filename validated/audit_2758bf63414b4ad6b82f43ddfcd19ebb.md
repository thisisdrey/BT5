### Title
Stale `Locked` backoff state persists after direct `transfer`/`burn` success, corrupting `AsScarcity` purse-lock accounting - (File: `substrate/frame/scarcity/src/extension.rs`, `substrate/frame/scarcity/src/lib.rs`)

### Summary
This is a structural analog of the reported `transferERC721` bug: one code path that moves the same asset performs a required cleanup of a per-key lock record, while a sibling path that moves the identical asset does not, leaving the lock record stale and reusable/miscounted.

### Finding Description
`pallet_scarcity` tracks a per-purse backoff lock in `Locked<T>` (`retries`, `until`) used by the `AsScarcity` transaction extension to gate feeless NFT-authorized `transfer`/`burn` calls [1](#0-0) .

The lock is only cleared in two places:
- `do_force_transfer` / `do_force_burn`, the privileged collection-owner paths, explicitly call `Locked::<T>::remove(&from)` / `Locked::<T>::remove(&purse)` after moving the NFT [2](#0-1) .
- `AsScarcity::post_dispatch_details`, which calls `Locked::<T>::remove(&owner)` only when the transaction actually used the extension's `Val::UsingNft` path (i.e., was authorized through `AsScarcityInfo`) [3](#0-2) .

The ordinary `do_transfer`/`do_burn` logic that backs the plain `transfer`/`burn` extrinsics does not itself touch `Locked` at all — cleanup relies entirely on the `AsScarcity` extension being present and engaged for that specific dispatch (`Val::UsingNft`). Per the pallet's own documentation, "Transfers are feeless when authorized through the `AsScarcity` transaction extension" [4](#0-3) , implying the same `transfer`/`burn` calls can also be dispatched normally (fee-paying, `Val::NotUsing`) without going through the NFT-purse authorization branch — exactly like `transferERC721` in the external report, which moves the NFT through the "normal" transfer path instead of the "unlock" path that does the cleanup.

When a purse account has an existing `Locked` entry (e.g. from a prior failed feeless dispatch) and then successfully moves its NFT through a path where `Val` resolves to `NotUsing` (extension not engaged for that call, or the call is dispatched through another origin route), `post_dispatch_details`'s `if let Pre::UsingNft { .. }` branch never triggers, so `Locked::<T>::remove` is never called [5](#0-4) . The `Locked` entry, including its `retries` counter, survives even though the NFT/state that it was guarding has already moved on.

### Impact Explanation
`AsScarcity::failed_dispatch_lock` computes exponential backoff based on the *existing* `retries` value read from `Locked<T>` [6](#0-5) . A stale, uncleaned `retries` counter means a later unrelated failed dispatch compounds backoff from an inflated base, extending the lockout duration (`until`) far beyond what the actual failure history for the *current* NFT state warrants. The `try_state` invariant checks even assert that every `Locked` entry must correspond to an existing NFT for that owner and have `retries >= 1` [7](#0-6) , showing the pallet's own design expects `Locked` accounting to always track the *current* purse/NFT state — which this gap breaks. This degrades the feeless dispatch mechanism (public underpriced work path) by causing incorrect, extended lockout of legitimate future transactions for a purse whose lock should have been cleared, and generally causes `Locked` records to no longer correspond to the actual state of the NFT purse, mirroring the "administration doesn't correspond to the available NFT's" impact from the source report.

### Likelihood Explanation
Requires only an unprivileged account: (1) triggering one failed `AsScarcity`-authorized dispatch to populate `Locked`, then (2) later successfully moving the same NFT/purse via a `transfer`/`burn` call where the `AsScarcity` extension does not resolve to `Val::UsingNft` for that dispatch (ordinary fee-paying call). No admin, governance, relayer, or validator involvement is needed — an attacker/user controls both steps directly.

### Recommendation
Clear `Locked::<T>::remove(&owner)` unconditionally as part of `do_transfer`/`do_burn` (or any code path that mutates `NftsByOwner` for a given key), not only inside `AsScarcity::post_dispatch_details` and the force-* helpers, so that lock bookkeeping is tied to the underlying asset-movement logic rather than to which transaction extension happened to authorize the call — consistent with how `do_force_transfer`/`do_force_burn` already do it.

### Proof of Concept
1. Purse `A` holds NFT `instance=0`.
2. Submit an `AsScarcity`-authorized `transfer` from `A` that fails at dispatch time (e.g., destination becomes occupied) → `post_dispatch_details` sets `Locked::<T>::insert(A, LockInfo{ retries:1, until:60 })` [8](#0-7) .
3. Before or without waiting for `until`, dispatch a plain (non-`AsScarcity`, `Val::NotUsing`) `transfer`/`burn` call that succeeds in moving/consuming the same purse's NFT state through the ordinary origin path.
4. `Locked::<A>` is never cleared because `post_dispatch_details` only clears it inside the `Pre::UsingNft` branch [9](#0-8) , and `do_transfer`/`do_burn` itself performs no `Locked` cleanup.
5. `Locked::<A>` still reports `retries:1` even though `A`'s NFT state has already moved past the failure it recorded; any subsequent unrelated failure compounds backoff from this stale base, and the `try_state` invariant "Locked entry has no matching NFT" can even be violated once the NFT itself is burned/moved away, exactly analogous to the uncleaned `timelockERC721s[key]` in the source report.

### Citations

**File:** substrate/frame/scarcity/src/lib.rs (L65-76)
```rust
//! Transfers are feeless when authorized through the [`AsScarcity`](extension::AsScarcity)
//! transaction extension. Their transaction priority is the time since the NFT last moved,
//! capped by the runtime. Moving an NFT consumes it from the old purse key and places it at the
//! new one. Each authorization names the permanent instance and its current state nonce. The state
//! nonce invalidates an authorization whenever that instance moves, including collection-owner
//! force-transfers away from and back to the same purse. Following Coinage's purse model,
//! [`AsScarcity`](extension::AsScarcity) replaces the signed origin before ordinary account checks,
//! so an NFT-only purse does not need a System account. Failed dispatch restores the NFT and
//! temporarily locks the purse key; after the lock expires, the same signed transaction may be
//! submitted again if its NFT state is still current. Callers must sign mortal transactions with
//! an era shorter than [`Config::LockPeriod`] so that retrying is always a fresh signing
//! decision; see the [replay and mortality rules](extension#replay-and-mortality).
```

**File:** substrate/frame/scarcity/src/lib.rs (L234-252)
```rust
	/// Post-failure backoff lock for an NFT purse key.
	#[derive(
		Clone,
		Copy,
		PartialEq,
		Eq,
		Debug,
		Encode,
		Decode,
		DecodeWithMemTracking,
		TypeInfo,
		MaxEncodedLen,
	)]
	pub struct LockInfo {
		/// Number of consecutive failed purse-key dispatches.
		pub retries: u8,
		/// Unix timestamp (seconds) at which this lock expires.
		pub until: u64,
	}
```

**File:** substrate/frame/scarcity/src/lib.rs (L1177-1213)
```rust
		fn do_force_burn(owner: &T::AccountId, instance: InstanceId) -> Result<u32, DispatchError> {
			let purse = Instances::<T>::get(instance).ok_or(Error::<T>::UnknownInstance)?;
			let nft = NftsByOwner::<T>::get(&purse).ok_or(Error::<T>::UnknownInstance)?;
			ensure!(nft.instance == instance, Error::<T>::UnknownInstance);
			let info =
				Collections::<T>::get(nft.collection).ok_or(Error::<T>::UnknownCollection)?;
			ensure!(info.owner == *owner, Error::<T>::NoPermission);

			NftsByOwner::<T>::remove(&purse);
			Locked::<T>::remove(&purse);
			Self::do_burn(nft)
		}

		fn do_force_transfer(
			owner: &T::AccountId,
			instance: InstanceId,
			to: T::AccountId,
		) -> DispatchResult {
			let from = Instances::<T>::get(instance).ok_or(Error::<T>::UnknownInstance)?;
			let nft = NftsByOwner::<T>::get(&from).ok_or(Error::<T>::UnknownInstance)?;
			ensure!(nft.instance == instance, Error::<T>::UnknownInstance);
			let info =
				Collections::<T>::get(nft.collection).ok_or(Error::<T>::UnknownCollection)?;
			ensure!(info.owner == *owner, Error::<T>::NoPermission);
			ensure!(to != from, Error::<T>::SelfTransfer);
			ensure!(!NftsByOwner::<T>::contains_key(&to), Error::<T>::AddressOccupied);

			let state_nonce =
				nft.state_nonce.checked_add(1).ok_or(Error::<T>::StateNonceOverflow)?;
			let nft = Nft { last_moved: T::UnixTime::now().as_secs(), state_nonce, ..nft };
			NftsByOwner::<T>::remove(&from);
			Locked::<T>::remove(&from);
			NftsByOwner::<T>::insert(&to, nft);
			Instances::<T>::insert(instance, &to);
			Self::deposit_event(Event::ForceTransferred { instance, from, to });
			Ok(())
		}
```

**File:** substrate/frame/scarcity/src/lib.rs (L1541-1548)
```rust
			for (owner, lock) in Locked::<T>::iter() {
				if !NftsByOwner::<T>::contains_key(owner) {
					return Err(TryRuntimeError::Other("Locked entry has no matching NFT"));
				}
				if lock.retries == 0 {
					return Err(TryRuntimeError::Other("Locked retry count must begin at one"));
				}
			}
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

**File:** substrate/frame/scarcity/src/extension.rs (L196-213)
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

**File:** substrate/frame/scarcity/src/tests.rs (L1278-1296)
```rust
#[test]
fn failed_dispatch_restores_and_locks() {
	new_test_ext().execute_with(|| {
		setup_item();
		define(0);
		mint(0, OWNER);

		// Race shape: the destination is empty at validation time and becomes occupied before
		// dispatch — the only failure path that still reaches dispatch now that validate
		// pre-checks the destination.
		let (_, val, origin) = validate_transfer(OWNER, 4).unwrap();
		mint(1, 4);
		let pre = prepare_transfer(val, &origin, 4);
		let dispatch = Scarcity::transfer(origin, 4);
		assert_noop!(dispatch, Error::<Test>::AddressOccupied);
		post_dispatch(pre, Err(Error::<Test>::AddressOccupied.into()));
		assert_eq!(NftsByOwner::<Test>::get(OWNER).map(|nft| nft.instance), Some(0));
		assert_eq!(Locked::<Test>::get(OWNER), Some(LockInfo { retries: 1, until: 60 }));
		assert_ok!(Scarcity::do_try_state());
```
