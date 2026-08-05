### Title
Unsolicited, non-consensual NFT minting permanently locks a target purse key — analog of `_mint()`/`_safeMint()` receiver-consent gap (`File: substrate/frame/scarcity/src/lib.rs`)

### Summary
The Sherlock finding's core broken invariant is: a mint entry point places an asset into a recipient-controlled account **without any check that the recipient consented to or can safely hold it**, allowing the mint to corrupt/lock the recipient's state. `pallet-scarcity`'s minting path (`do_mint_inner`) reproduces the same invariant break in FRAME terms: any signed account that owns a collection/item definition can mint an NFT into *any* other account's purse-key slot with the only check being that the slot is currently empty, not that the target consented to receive it.

### Finding Description
`pallet-scarcity` implements a "coinage-style" one-NFT-per-account model: each account (`purse key`) can hold at most one instance, tracked in `NftsByOwner<T>`. The mint helper is: [1](#0-0) 

The only guard on the destination is `ensure!(!NftsByOwner::<T>::contains_key(&to), Error::<T>::AddressOccupied)` — it checks the slot is *empty*, not that the target account agreed to receive this specific instance. There is no analog of the ERC-721 `onERC721Received` callback / receiver-acceptance step that `_safeMint()` performs before committing state.

The pallet's own doc comments confirm the design gap explicitly: [2](#0-1) 

This states that "any collection owner can mint into — or force-transfer an instance to — any empty purse key," and that doing so "blocks that key from receiving anything else until its holder burns it or transfers it away." This is the direct FRAME analog of `FootiumClub.sol` using `_mint()` instead of `_safeMint()`: the minting authority (equivalent to the contract owner who calls `mint()`) can push an item into an account that never opted in, and the account has no way to reject the transfer before it lands in storage — only after the fact via burn/transfer, and only once it notices the state has been altered.

`force_transfer` on the collection owner side has the same unconditional destination-write pattern based on the same `do_mint_inner`/`NftsByOwner` insert without consent, compounding the issue since a live instance can be forcibly relocated onto any empty purse key too.

### Impact Explanation
Any account that is used as a "fresh, disposable" purse key per the pallet's intended usage pattern can be pre-occupied by an unrelated collection owner before the legitimate holder ever transacts with it. Because the model caps holding at exactly one instance per account, an attacker-controlled or merely first-mover collection can mint garbage/unwanted instances into target purse keys, permanently denying that account the ability to receive the NFT it actually wants until the victim notices, and burns or transfers away the unsolicited instance (paying gas/deposit-unlock costs to do so). This is a public, underpriced griefing/DoS vector against arbitrary third-party accounts with no consent gate, directly mirroring the impact class from the seed report (asset minted into a destination that cannot properly "accept" or use it).

### Likelihood Explanation
Likelihood is high for any account that publishes its address in advance (e.g. to receive an expected drop) since minting is available to any collection/item owner and the only precondition is that the target's slot is currently empty — a condition trivially satisfiable by monitoring chain state before the legitimate mint executes.

### Recommendation
Add an explicit receiver-consent step to `do_mint_inner` (and to the `force_transfer` path that reuses it) analogous to `_safeMint()`'s acceptance hook — e.g., require the destination to have pre-signed a mint claim (the pallet already has `mint_pre_signed`-style patterns elsewhere in the nft pallets, see `substrate/frame/nfts/src/features/create_delete_item.rs` `do_mint_pre_signed`) or require a pending "offer" that the recipient explicitly claims before `NftsByOwner` is written, rather than allowing unsolicited direct inserts into third-party accounts.

### Proof of Concept
1. Alice creates a fresh account intending to receive `CollectionA/Item1` from a trusted minter later.
2. Bob, owner of an unrelated `CollectionB/Item9`, calls the scarcity mint extrinsic targeting Alice's account before she transacts.
3. `do_mint_inner` passes the `!NftsByOwner::<T>::contains_key(&to)` check (slot empty) and inserts Bob's unwanted instance into Alice's `NftsByOwner` entry.
4. Alice's account is now `AddressOccupied`; the trusted minter's later attempt to mint `CollectionA/Item1` to Alice fails with `Error::<T>::AddressOccupied` until Alice notices and burns/transfers Bob's instance away. [3](#0-2)

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

**File:** substrate/frame/scarcity/src/lib.rs (L1274-1290)
```rust
		#[transactional]
		fn do_mint_inner(
			collection: CollectionId,
			item: ItemIndex,
			to: T::AccountId,
			metadata: Vec<(MetadataKeyOf<T>, MetadataValueOf<T>)>,
			with_deposit: bool,
		) -> Result<InstanceId, DispatchError> {
			ensure!(
				metadata.len() <= T::MaxInstanceMetadata::get() as usize,
				Error::<T>::TooManyInstanceMetadata
			);
			let mut info =
				Collections::<T>::get(collection).ok_or(Error::<T>::UnknownCollection)?;
			let mut definition =
				ItemDefs::<T>::get(collection, item).ok_or(Error::<T>::UnknownItem)?;
			ensure!(!NftsByOwner::<T>::contains_key(&to), Error::<T>::AddressOccupied);
```
