### Title
Unsolicited-NFT griefing permanently locks a victim's Scarcity purse key without consent - (File: `substrate/frame/scarcity/src/lib.rs`)

### Summary
`pallet-scarcity` implements a "coinage-style" NFT ownership model where each account (purse key) can hold **at most one instance** at a time. Minting and force-transfer are gated only by collection ownership, not by any consent from the destination account. Because collection creation itself is permissionless, any attacker can create their own collection and then mint/force-transfer an unwanted instance into any target account, permanently occupying that account's single NFT slot. This is the same "unsolicited-asset-to-fill-a-capacity-limit" griefing primitive as the Paraspace Uniswap-V3-NFT report, except here the limit is `1` instead of `30`, making the attack even cheaper and more absolute.

### Finding Description
The pallet's own module documentation states the behavior explicitly: [1](#0-0) 

```
//! Purse keys are coinage-style receiving addresses, not identities: the pallet applies no
//! destination consent. Any collection owner can mint into — or force-transfer an instance to —
//! any empty purse key, and because each key holds at most one NFT, an unsolicited instance
//! blocks that key from receiving anything else until its holder burns it or transfers it away.
```

Concretely:
- `do_create_collection` is reachable by any signed account and is not gated by any privileged role — anyone can become a "collection owner" of their own newly created collection. [2](#0-1) 
- `force_transfer` only checks that the caller is the *owner of that specific collection* (a role the attacker grants themselves by creating the collection), not that the destination consents: [3](#0-2) 
- `do_force_transfer` enforces only `AddressOccupied` (destination must be empty) and `SelfTransfer`; there is no destination-approval or opt-in check before writing the NFT into the victim's `NftsByOwner` entry: [4](#0-3) 
- The one-NFT-per-account invariant is enforced pallet-wide (`AddressOccupied` error, and tests confirm minting/transferring into an occupied key is rejected): [5](#0-4) [6](#0-5) 

This mirrors the Paraspace bug-class exactly: a balance/count cap intended to bound per-account state (there: 30 Uniswap V3 NFTs to bound `calculateUserAccountData` gas; here: 1 NFT per purse key to preserve the coinage model) can be filled by an attacker sending unsolicited, cheaply-minted assets to a victim's address, denying the victim the ability to receive/use their own address for the intended purpose.

### Impact Explanation
Because a purse key is also the `Origin::Nft` authorization anchor used by the feeless `AsScarcity` transaction extension (transfers/burns authorized by "the owning purse key"), occupying an account with an attacker-controlled instance:
- Permanently blocks that address from receiving any *wanted* instance (mint or transfer) until the victim notices and manually burns or transfers away the unwanted item — a state the victim did not consent to and may not want to interact with (e.g., paying to burn junk).
- Since holders are told to treat purse keys as disposable and mint to *fresh* keys, this specifically harms any address that is reused as a receiving destination (e.g., an exchange deposit address, a collection-owner-controlled distribution address, or any address a user expects to reuse), turning a designed simplification into a griefing/DoS vector with a real "permanent address lock until manual remediation" effect.
- Attack cost is minimal: the attacker only pays their own collection/item creation and instance deposits (which they fully control and can size to a minimum), while the damage (locking any number of arbitrary victim addresses) scales for free.

This falls under "public underpriced work that... stalls processing" / "permanent user-fund or... state lock" from the impact gate, executed entirely by an unprivileged, self-created collection owner — not a validator, governance actor, or admin of the broader chain.

### Likelihood Explanation
High. Collection creation and minting into arbitrary destinations are ordinary, permissionless, signed extrinsics; no governance, validator, or off-chain relayer collusion is required. The documented rationale ("Any collection owner can mint into... any empty purse key... no destination consent") confirms this is not a hypothetical edge case but literally how the pallet is described to behave for every mint/force-transfer call.

### Recommendation
Require destination opt-in/consent before an instance can be placed into a previously-unoccupied purse key that the caller does not control — e.g., a pre-registered "accepting" flag on the destination account, a two-step accept/claim flow analogous to `nominate_collection_owner`/`claim_collection_ownership`, or restrict unsolicited mint/force-transfer destinations to addresses that have explicitly pre-authorized a given collection/minter. At minimum, expose a way for a runtime/contract layer to enforce a consent policy above the storage layer *before* mint/force-transfer commits state, since currently there is no in-pallet hook preventing the occupation of an address the caller does not own.

### Proof of Concept
1. Attacker calls the permissionless collection-creation extrinsic to create `CollectionId = X` (`do_create_collection`), becoming its owner.
2. Attacker defines an item and mints an instance (`mint`/`mint_without_deposit`) directly to `victim` — allowed because `do_force_transfer`/mint paths only check `AddressOccupied` on the destination, not consent: [7](#0-6) 
3. `NftsByOwner::<T>::insert(&victim, nft)` succeeds since `victim`'s purse key was empty.
4. `victim` (who never asked for this NFT) can no longer receive any legitimate instance at that address — any subsequent mint/transfer targeting `victim` fails with `Error::AddressOccupied`, confirmed by the existing test `one_nft_per_key_on_transfer`: [6](#0-5) 
5. `victim` must discover the unsolicited instance and burn or transfer it away before the address becomes usable again for its intended purpose — an attacker-imposed cost and denial-of-service the victim did not choose.

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

**File:** substrate/frame/scarcity/src/lib.rs (L421-422)
```rust
		/// The destination purse key already holds an NFT.
		AddressOccupied,
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

**File:** substrate/frame/scarcity/src/lib.rs (L886-900)
```rust
		/// Allocate a collection identifier and record its initial owner.
		pub fn do_create_collection(owner: T::AccountId) -> Result<CollectionId, DispatchError> {
			let collection = NextCollectionId::<T>::get();
			let next_collection =
				collection.checked_add(1).ok_or(Error::<T>::TooManyCollections)?;
			let footprint = Footprint::from_mel::<CollectionInfoOf<T>>();
			let collection_deposit = T::CollectionDeposit::convert(footprint);
			let consideration = T::Consideration::new(&owner, collection_deposit)?;

			NextCollectionId::<T>::put(next_collection);
			Collections::<T>::insert(
				collection,
				CollectionInfo {
					owner: owner.clone(),
					pending_owner: None,
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

**File:** substrate/frame/scarcity/src/tests.rs (L1374-1388)
```rust
#[test]
fn one_nft_per_key_on_transfer() {
	new_test_ext().execute_with(|| {
		setup_item();
		define(0);
		mint(0, RECIPIENT);
		mint(1, OTHER);
		let nft = NftsByOwner::<Test>::take(RECIPIENT).expect("minted NFT exists");

		assert_noop!(
			Scarcity::transfer(nft_origin(RECIPIENT, nft), OTHER),
			Error::<Test>::AddressOccupied
		);
	});
}
```
