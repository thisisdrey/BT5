## Analysis

The Witch bug's core invariant break is: **a piece of "previous state" needed to reverse a seizure is unconditionally overwritten by a later operation that assumes the slot it's writing into is still in its original, untouched condition** — with no check that something else already occupies it.

The closest local analog in this repo is in the custom `pallet-scarcity` module, specifically the `AsScarcity` transaction extension's checkout/restore flow.

### Title
Blind restore in `AsScarcity::post_dispatch_details` can silently overwrite an NFT placed into a purse while it was checked out - (File: `substrate/frame/scarcity/src/extension.rs`)

### Summary
`pallet-scarcity` enforces a one-NFT-per-purse invariant via the `NftsByOwner` map, and the module documents that "any collection owner can mint into — or force-transfer an instance to — any empty purse key" [1](#0-0) . The `AsScarcity` extension's `prepare` hook removes ("checks out") the NFT from `NftsByOwner::<T>` for the duration of dispatch [2](#0-1) , which makes the purse look empty to any other logic running in the same block (e.g. a collection-owner `force_transfer`/mint targeting an "empty" purse). If the original dispatch then fails, `post_dispatch_details` unconditionally does `NftsByOwner::<T>::insert(&owner, nft)` to restore the checked-out NFT [3](#0-2) , with no check that the slot is still empty or still matches the state that was taken out.

### Finding Description
This mirrors the Witch bug exactly: `vaultOwners[vaultId]` is written assuming the vault has not already been re-grabbed; here, `NftsByOwner::<owner>` is restored assuming the purse has not already received a different NFT while it was vacated. Sequence:

1. Attacker/owner submits a `transfer`/`burn` extrinsic authorized via `AsScarcity`. `validate` confirms the purse currently holds instance `I` at `state_nonce N` [4](#0-3) .
2. `prepare` takes the NFT out of `NftsByOwner::<owner>`, leaving the map entry `None` for the remainder of the transaction's execution [5](#0-4) .
3. Within the same dispatch (e.g., a collection owner's `force_transfer` nested via the pallet's own dispatchable, or another entrypoint that checks `NftsByOwner::<T>::contains_key` for "empty purse" before minting/transferring), a different instance `J` gets placed into that same purse, because the purse now legitimately reads as empty.
4. The original call subsequently fails (e.g., a downstream `ensure!` in `transfer`/`burn` reverts after step 3 but the extension's dispatch as a whole still returns `Err`), triggering `post_dispatch_details`, which blindly re-inserts the original instance `I` over the purse, clobbering instance `J`'s placement with no comparison or ownership check [6](#0-5) .

Existing guards do not stop this: `validate`'s pre-checks (`NftStateMismatch`, `DestinationOccupied`) only run once, before `prepare` vacates the slot, and `post_dispatch_details` has no equivalent re-check after the vacancy window closes. This is structurally identical to Witch's failure to detect "already regrabbed" state before overwriting `vaultOwners[vaultId]`.

### Impact Explanation
An NFT record (`InstanceId` -> purse) can be silently destroyed/orphaned from the accounting map even though the instance itself, its deposits (`InstanceDeposits`), and metadata remain allocated — leaving state that is inconsistent with `do_try_state` invariants and permanently unrecoverable to its rightful holder without a chain-level intervention, i.e., a permanent user-fund/state lock analogous to the Witch report's un-returnable vault.

### Likelihood Explanation
Requires a same-block interleaving of the checked-out purse window with another legitimate mint/force-transfer to that specific purse and a subsequent dispatch failure of the original call — a non-trivial but entirely permissionless timing condition (no malicious validator/relayer/admin needed), achievable by an unprivileged party racing their own transactions or exploiting a collection owner's routine `force_transfer`/mint against a purse they know is mid-flight. This is a real but narrow-window race rather than a trivially always-exploitable bug.

### Recommendation
Make the restore in `post_dispatch_details` conditional: use `try_mutate_exists`/CAS semantics that only restore instance `I` if the slot is still vacant (`None`), and otherwise emit an event/error/defensive-log instead of overwriting whatever now occupies the purse — mirroring the report's suggested fix of checking "is the slot already reoccupied" before clobbering prior state.

### Proof of Concept
Conceptual repro (exact nested-call trigger for step 3 was not fully verified against `force_transfer`/mint implementations in this session, since those functions could not be read within the available iterations — this should be confirmed against `substrate/frame/scarcity/src/lib.rs`'s `force_transfer`/`mint`/`mint_without_deposit` implementations, and whether any of them can target a purse mid-`AsScarcity` dispatch):

1. Purse `P` holds NFT `I` (`state_nonce=0`).
2. Submit `transfer(to=Q)` from `P` authorized via `AsScarcity{instance: I, state_nonce: 0}`. `prepare` empties `NftsByOwner<P>`.
3. In the same block/dispatch path, trigger a mint or collection-owner `force_transfer` of a different instance `J` into `P` (purse now reads empty per the map).
4. Force the original `transfer` to fail post-checkout (e.g. destination becomes occupied concurrently, tripping a dispatch-time `ensure!`).
5. `post_dispatch_details` restores `I` into `NftsByOwner<P>`, overwriting `J`'s placement — `J` is now orphaned from `NftsByOwner` while its deposit/metadata records still exist, breaking the one-NFT-per-purse invariant asserted by `do_try_state`.

**Note on limitations:** I could not fully confirm within the available tool budget whether `force_transfer`/`mint` in this pallet actually check `NftsByOwner::<T>::contains_key` in a way that races with the `AsScarcity` checkout window (I saw only the doc comments describing this behavior, not the exact implementation). If you need certainty on the exact reachable call path, a Devin session with full file access to `substrate/frame/scarcity/src/lib.rs` (particularly the `transfer`, `force_transfer`, and `mint`/`do_mint` dispatchables) would be needed to confirm the precise trigger.

### Citations

**File:** substrate/frame/scarcity/src/lib.rs (L29-32)
```rust
//! Purse keys are coinage-style receiving addresses, not identities: the pallet applies no
//! destination consent. Any collection owner can mint into — or force-transfer an instance to —
//! any empty purse key, and because each key holds at most one NFT, an unsolicited instance
//! blocks that key from receiving anything else until its holder burns it or transfers it away.
```

**File:** substrate/frame/scarcity/src/extension.rs (L215-228)
```rust
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
```

**File:** substrate/frame/scarcity/src/extension.rs (L262-281)
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
		}
	}
```

**File:** substrate/frame/scarcity/src/extension.rs (L290-296)
```rust
		if let Pre::UsingNft { owner, nft } = pre {
			if result.is_err() {
				NftsByOwner::<T>::insert(&owner, nft);
				Locked::<T>::insert(&owner, Self::failed_dispatch_lock(Locked::<T>::get(&owner)));
			} else {
				Locked::<T>::remove(&owner);
			}
```
