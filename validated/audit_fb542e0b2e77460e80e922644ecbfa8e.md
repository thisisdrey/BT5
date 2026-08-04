## Analysis

The external report's core broken invariant: **an asset-minting entry point pushes value to an attacker/owner-chosen recipient with no check that the recipient can ever move, consent to, or reclaim that asset — and the ownership model makes the value permanently unrecoverable once locked.**

The closest verifiable local analog is `substrate/frame/scarcity`, a "coinage-style" NFT pallet whose `AccountId` slots ("purse keys") function much like ERC721 `_mint` recipients, but with no analog to a "receiver check," and its own custom recovery path (`Origin::Nft` via `AsScarcity`) that can only ever be exercised by whoever controls the *private key* of that specific `AccountId`.

### Title
Unconsented NFT mint to any AccountId permanently strands the instance and the purse key with no receiver-capability check - (File: `substrate/frame/scarcity/src/lib.rs`)

### Summary
`Pallet::do_mint` / `do_mint_inner` in `substrate/frame/scarcity/src/lib.rs` mint an NFT into an arbitrary `to: T::AccountId` supplied by the collection owner (or by any runtime pallet calling the `MintWithoutDeposit` trait) with the only guard being that the destination purse currently holds no NFT (`AddressOccupied`). There is no requirement that `to` be an account capable of ever signing a transaction to move, burn, or otherwise "receive" the instance. This is the Substrate structural analog of `_mint` versus `_safeMint`: the pallet transfers value to a destination without verifying the destination can act on it.

### Finding Description
`do_mint_inner` unconditionally inserts the NFT for the caller-chosen `to`: [1](#0-0) [2](#0-1) 

The module's own documentation confirms there is deliberately no destination consent check, and that an unwanted instance permanently occupies the key: [3](#0-2) 

The only way to move an NFT out of a purse key is the `transfer`/`burn` dispatchables, both of which require the `Origin::Nft` origin, which is produced *exclusively* by the `AsScarcity` transaction extension after validating that the transaction was **signed by the account that owns the purse key** (`frame_system::Origin::<T>::Signed(owner)`): [4](#0-3) 

There is no root/force call to reclaim or forcibly move an NFT out of a purse key that nobody controls — `force_transfer`/`force_burn` are owner-invoked collection-management calls, but they still just move the instance to another `to: T::AccountId` chosen by the collection owner, with exactly the same lack of a receiver-capability check: [5](#0-4) 

Because `NftsByOwner` allows **at most one NFT per key** (the "coinage model"), the analog to the ERC-721 bug is even sharper here than the Solidity original: minting to an uncontrollable `AccountId` (e.g., a derived/hashed address with no known private key, a pallet sovereign account, or any address the collection owner picks that the "holder" never registered or consented to) not only strands that one instance forever (nobody can ever construct a validly-signed `AsScarcity` transaction from that origin), it also permanently blocks that same key from ever receiving any other Scarcity instance in the future, since `AddressOccupied` will reject all further mints/transfers to it.

### Impact Explanation
High — permanent, protocol-level loss of an asset. Any minted instance sent to an uncontrolled or non-consenting `AccountId` is locked in storage forever with no path — signed or governance/root — to recover, transfer, or burn it, because `Origin::Nft` can only be produced from a live, valid signature by that specific account. This mirrors the "records minted... stuck forever" impact of the original finding, but the Substrate model additionally causes lasting collateral damage: the poisoned key can never legitimately receive a Scarcity instance again.

### Likelihood Explanation
Moderate. `mint`, `force_transfer`, and `MintWithoutDeposit::mint_without_deposit` are all directly invocable by an ordinary collection owner (no privileged runtime call needed) with `to` freely chosen — analogous to the report's caveat that "users can freely implement Engines" that allow contract recipients. Any owner who mints to a mistyped address, a purely derived/hashed `AccountId` (e.g. a multi-location-derived sovereign account with no keypair), or an address supplied by an untrusted third party will trigger permanent loss, and there's no built-in safeguard (no equivalent of `_safeMint`'s receiver-capability check, and no privileged escape hatch).

### Recommendation
Add either (a) an explicit recipient-consent/pre-registration step (similar to requiring the destination to first "claim" via a signed extrinsic before it can receive, akin to `nominate_collection_owner`/`claim_collection_ownership`), or (b) a `ForceOrigin`-gated administrative call that can move/burn an NFT out of a purse key without requiring a signature from that key, to provide a recovery path when a mint or force-transfer targets a destination that turns out to be non-consenting or permanently uncontrollable.

### Proof of Concept
1. Collection owner calls `Scarcity::create_collection` and `define_item`, then `Scarcity::mint(origin, collection, item, to = X, metadata)` where `X` is an `AccountId` derived purely from data (e.g., a hash with no known private key, or any account the owner does not control and did not get consent from). [6](#0-5) 
2. `do_mint` succeeds (`X` is unoccupied) and inserts `NftsByOwner::<T>::insert(&X, nft)`. [2](#0-1) 
3. Because nobody controls `X`'s private key, no valid `AsScarcity`-signed `transfer`/`burn` extrinsic can ever be constructed from `Origin::Signed(X)`, so `Origin::Nft` for that instance can never be produced. [4](#0-3) 
4. The instance is permanently unreachable, and any future mint/force-transfer attempt to `X` fails with `AddressOccupied`, confirmed by the existing test asserting this exact occupancy rule with no override path. [7](#0-6)

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

**File:** substrate/frame/scarcity/src/lib.rs (L1286-1296)
```rust
			let mut info =
				Collections::<T>::get(collection).ok_or(Error::<T>::UnknownCollection)?;
			let mut definition =
				ItemDefs::<T>::get(collection, item).ok_or(Error::<T>::UnknownItem)?;
			ensure!(!NftsByOwner::<T>::contains_key(&to), Error::<T>::AddressOccupied);

			let instance = NextInstanceId::<T>::get();
			let next_instance = instance.checked_add(1).ok_or(Error::<T>::TooManyInstances)?;
			let next_supply = definition.supply.checked_add(1).ok_or(Error::<T>::SupplyOverflow)?;
			let next_live_supply =
				definition.live_supply.checked_add(1).ok_or(Error::<T>::SupplyOverflow)?;
```

**File:** substrate/frame/scarcity/src/lib.rs (L1318-1326)
```rust
			definition.supply = next_supply;
			definition.live_supply = next_live_supply;
			NextInstanceId::<T>::put(next_instance);
			let collection_owner = info.owner.clone();
			Collections::<T>::insert(collection, info);
			ItemDefs::<T>::insert(collection, item, definition);
			NftsByOwner::<T>::insert(&to, nft);
			Instances::<T>::insert(instance, &to);
			InstanceMetadataCount::<T>::insert(instance, 0);
```

**File:** substrate/frame/scarcity/src/extension.rs (L215-225)
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
```

**File:** substrate/frame/scarcity/src/tests.rs (L1076-1096)
```rust
#[test]
fn mint_without_deposit_checks_collection_item_and_destination() {
	new_test_ext().execute_with(|| {
		setup_item();
		define(0);

		assert_noop!(
			Scarcity::mint_without_deposit(99, 0, RECIPIENT, metadata(&[])),
			Error::<Test>::UnknownCollection
		);
		assert_noop!(
			Scarcity::mint_without_deposit(0, 99, RECIPIENT, metadata(&[])),
			Error::<Test>::UnknownItem
		);
		assert_ok!(Scarcity::mint_without_deposit(0, 0, RECIPIENT, metadata(&[])));
		assert_noop!(
			Scarcity::mint_without_deposit(0, 1, RECIPIENT, metadata(&[])),
			Error::<Test>::AddressOccupied
		);
	});
}
```
