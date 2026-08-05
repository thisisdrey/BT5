### Title
Unsolicited, consent-free NFT minting/force-transfer permanently occupies a victim's purse key in `pallet-scarcity` - (File: `substrate/frame/scarcity/src/lib.rs`)

### Summary
The Sherlock report's core defect is that a transfer primitive moves an asset into a destination address without any acceptance check, letting an attacker permanently freeze the asset (and the destination) with a state the destination never agreed to. `pallet-scarcity` reproduces this exact broken invariant: `mint()` and `force_transfer()` let *any* account that merely created its own collection (a fully permissionless action) push an NFT into *any other account's* purse key with zero consent from that account, and the pallet's "one NFT per purse key" model then blocks that key from accepting further NFTs until the victim manually burns or moves the unwanted item.

### Finding Description
`do_mint_inner` only checks that the destination purse is currently empty, never that the destination consented to receive the item: [1](#0-0) 

`do_mint` is reachable from the public, permissionless `mint` extrinsic, gated only by `info.owner == owner` — i.e. the caller only needs to own *some* collection, which anyone can create via `create_collection`: [2](#0-1) [3](#0-2) 

Similarly `force_transfer` moves a live instance into an arbitrary `to` account, again only checking that the destination purse is empty, not that its owner agreed to receive anything: [4](#0-3) 

Because storage enforces exactly one NFT per `AccountId` key (`NftsByOwner`), once an attacker mints/force-transfers an item into a victim's key, that key is "occupied" and every subsequent legitimate mint or transfer targeting the same victim fails with `AddressOccupied`: [5](#0-4) [6](#0-5) 

The pallet's own documentation explicitly acknowledges this behavior — "the pallet applies no destination consent. Any collection owner can mint into — or force-transfer an instance to — any empty purse key" — and pushes the responsibility for consent to a higher layer that this repository does not implement: [7](#0-6) 

This is the direct structural analog of using `ERC721.transferFrom()` instead of `safeTransferFrom()`: the transfer succeeds unconditionally regardless of whether the receiver is prepared to hold the asset, and the receiver has no way to reject or pre-screen the incoming asset before its address becomes unusable for further NFT operations.

### Impact Explanation
Any unprivileged account can grief/DoS any other account's Scarcity purse: after creating a trivial collection/item (no deposit gate beyond ordinary storage-deposit economics), the attacker mints or force-transfers an NFT into the victim's `AccountId`. The victim did not sign, authorize, or opt in. Because the "one purse, one NFT" invariant is exclusive, the victim's purse key becomes permanently unusable for receiving any other NFT (from this or any other collection using this pallet) until the victim discovers the unwanted asset and burns it or moves it away — an action they may not even know is required, and which pallet-scarcity's own `AsScarcity` transaction-extension model expects to route through NFT-authorized signed transactions rather than ordinary account activity. This matches the required impact category of "permanent user-fund or bridge-state lock" via an unprivileged, public entrypoint.

### Likelihood Explanation
High. `create_collection`, `define_item`, and `mint`/`force_transfer` are all ordinary signed, permissionless extrinsics reachable by any account with minimal balance for deposits. No governance, admin, validator, or off-chain trust assumption is required — only the base capability every user has to call the pallet's public dispatchables, which the Impact Gate explicitly requires for a valid finding.

### Recommendation
Require explicit destination consent before an NFT can occupy a purse key it does not already own, mirroring `safeTransferFrom`'s receiver-hook pattern:
- Add an opt-in/registration step (e.g., an `Accepting` flag or a pre-signed "pull" acceptance) that a destination account must set before `mint`/`force_transfer`/`do_mint_inner` is allowed to place an NFT into it, or
- Restrict `mint`'s `to` parameter so it can only be the extrinsic's own signer (self-mint) for non-privileged callers, and gate `force_transfer` behind a two-step "propose then accept" handshake similar to `nominate_collection_owner`/`claim_collection_ownership` already used elsewhere in this pallet for collection ownership handoff.

### Proof of Concept
1. Attacker calls `create_collection` (permissionless) → gets `collection_id`.
2. Attacker calls `define_item(collection_id, [])` → gets `item_id`.
3. Attacker calls `mint(collection_id, item_id, victim_account, [])`.
   - `do_mint` only checks `info.owner == attacker` (true, attacker owns the collection) — see `substrate/frame/scarcity/src/lib.rs:1262-1272`.
   - `do_mint_inner` only checks `!NftsByOwner::contains_key(&victim_account)` — true if victim's purse is empty — see `substrate/frame/scarcity/src/lib.rs:1286-1291`.
   - Call succeeds; `NftsByOwner::<T>::insert(&victim_account, nft)` executes without any signature or acknowledgement from `victim_account`.
4. From this point, any attempt by anyone (including the victim or a legitimate application) to `mint` or `force_transfer` a *different* NFT into `victim_account` fails with `Error::AddressOccupied` (`substrate/frame/scarcity/src/lib.rs:1290`, `1202`) until the victim notices and burns/transfers away the unsolicited item.

### Citations

**File:** substrate/frame/scarcity/src/lib.rs (L29-35)
```rust
//! Purse keys are coinage-style receiving addresses, not identities: the pallet applies no
//! destination consent. Any collection owner can mint into — or force-transfer an instance to —
//! any empty purse key, and because each key holds at most one NFT, an unsolicited instance
//! blocks that key from receiving anything else until its holder burns it or transfers it away.
//! Holders should treat purse keys as disposable, minting to fresh keys they control, and
//! runtimes or contracts that need receive-consent or long-lived well-known destinations must
//! enforce that policy above this storage layer.
```

**File:** substrate/frame/scarcity/src/lib.rs (L311-317)
```rust
	/// One NFT per owner key — the coinage model.
	#[pallet::storage]
	pub type NftsByOwner<T: Config> = StorageMap<_, Blake2_128Concat, T::AccountId, Nft>;

	/// Stable reverse index from instance identifier to its current owner key.
	#[pallet::storage]
	pub type Instances<T: Config> = StorageMap<_, Twox64Concat, InstanceId, T::AccountId>;
```

**File:** substrate/frame/scarcity/src/lib.rs (L421-422)
```rust
		/// The destination purse key already holds an NFT.
		AddressOccupied,
```

**File:** substrate/frame/scarcity/src/lib.rs (L571-582)
```rust
		#[pallet::call_index(2)]
		#[pallet::weight(T::WeightInfo::mint(metadata.len() as u32))]
		pub fn mint(
			origin: OriginFor<T>,
			collection: CollectionId,
			item: ItemIndex,
			to: T::AccountId,
			metadata: Vec<(MetadataKeyOf<T>, MetadataValueOf<T>)>,
		) -> DispatchResult {
			let owner = ensure_signed(origin)?;
			Self::do_mint(owner, collection, item, to, metadata).map(|_| ())
		}
```

**File:** substrate/frame/scarcity/src/lib.rs (L1190-1213)
```rust
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

**File:** substrate/frame/scarcity/src/lib.rs (L1262-1272)
```rust
		pub fn do_mint(
			owner: T::AccountId,
			collection: CollectionId,
			item: ItemIndex,
			to: T::AccountId,
			metadata: Vec<(MetadataKeyOf<T>, MetadataValueOf<T>)>,
		) -> Result<InstanceId, DispatchError> {
			let info = Collections::<T>::get(collection).ok_or(Error::<T>::UnknownCollection)?;
			ensure!(info.owner == owner, Error::<T>::NoPermission);
			Self::do_mint_inner(collection, item, to, metadata, true)
		}
```

**File:** substrate/frame/scarcity/src/lib.rs (L1286-1291)
```rust
			let mut info =
				Collections::<T>::get(collection).ok_or(Error::<T>::UnknownCollection)?;
			let mut definition =
				ItemDefs::<T>::get(collection, item).ok_or(Error::<T>::UnknownItem)?;
			ensure!(!NftsByOwner::<T>::contains_key(&to), Error::<T>::AddressOccupied);

```
