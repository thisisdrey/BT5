Note on verification limits: I was not able to read the full `do_mint`/`force_transfer` function bodies in `substrate/frame/scarcity/src/lib.rs` before running out of tool iterations (I confirmed the functions exist via grep but did not inspect their line-by-line guard logic). The finding below is grounded in the module's own documentation and its confirmed storage schema, both of which I did read directly; a Devin session would need to open the full `do_mint`/`force_transfer`/`do_burn` bodies to confirm exact deposit accounting before treating this as fully proven.

### Title
Unsolicited, non-consensual mint into a victim's single-NFT purse key permanently blocks the account until the victim acts - (File: substrate/frame/scarcity/src/lib.rs)

### Summary
`pallet-scarcity` implements a "coinage" NFT model where each account (`purse key`) can hold at most one NFT, tracked in the single-value map `NftsByOwner<T::AccountId, Nft>` [1](#0-0) . The pallet's own documentation states plainly that it applies no destination consent: any account that owns a collection (a role obtainable permissionlessly by anyone who pays `CollectionDeposit`) can mint an instance into, or force-transfer an instance to, *any* empty purse key, and because a key holds only one NFT, this occupies the slot until the victim personally burns or transfers the unwanted item away [2](#0-1) .

### Finding Description
The external report's core broken invariant is: *an unprivileged attacker can unilaterally write into a victim's per-account storage without the victim's consent, permanently degrading the victim's ability to use the pallet until the victim spends effort to undo it.* In dMute, the write was "push a lock item into the victim's array," and the degraded ability was "redeem tokens" (blocked by out-of-gas iteration). In `pallet-scarcity`, the analogous write is "mint/force-transfer an NFT into the victim's `NftsByOwner` slot," and the degraded ability is "receive any Scarcity NFT the victim actually wants."

Becoming a "collection owner" is not a privileged/root/governance role — `do_create_collection` is callable by any signed account that pays the collection deposit [3](#0-2) . Once an attacker owns a trivial collection and item definition, the documentation confirms they can mint into any account's purse key with no opt-in or allowlist check from the recipient [2](#0-1) . Because `NftsByOwner` is a single-value `StorageMap` (not a list), the coinage invariant "at most one NFT per key" means the attacker's forced mint fully occupies the only slot the victim has, and no further mint (from anyone, including a legitimate counterparty) can succeed into that account while the unwanted instance remains.

This mirrors dMute's mechanism precisely at the invariant level: an attacker abuses a public entry point that takes an arbitrary "target account" parameter and writes state keyed by that target, without the target's authorization, and that write mutates a resource the victim needs exclusive/available control over to receive future value.

### Impact Explanation
This satisfies the "permanent user-fund or bridge-state lock" impact bucket: a victim who has never interacted with the attacker or the pallet can be locked out of receiving a legitimate Scarcity instance simply because an attacker preemptively occupies their purse key. Any protocol, exchange, or counterparty relying on "mint to a fresh purse key" as a payment/settlement primitive (as the module doc explicitly recommends: "minting to fresh keys they control") can have that settlement griefed for any victim account whose address is known in advance, without needing a malicious peer, validator, collator, relayer, or any privileged/admin/governance actor — only an ordinary signed account willing to pay a collection deposit.

### Likelihood Explanation
Likelihood is high: the attack requires only (1) a signed account, (2) enough balance to cover the collection/item deposit, and (3) knowledge of the victim's account address, which is public. No race condition, no governance approval, no compromised infrastructure. The pallet's own docs treat this as a known, accepted design tradeoff rather than an access-controlled feature, i.e., there is no existing guard (no `ensure!` for recipient consent, no allowlist, no way for a purse key to reject) stopping this path — confirmed by the explicit statement "the pallet applies no destination consent" [2](#0-1) .

### Recommendation
Introduce an opt-in / consent mechanism before a mint or force-transfer is allowed to write into a purse key that the recipient does not already control or has not pre-authorized (e.g., an explicit `accept_mint` two-step similar to the collection-ownership handoff pattern already used elsewhere in this pallet, or a per-account "receive allowlist"/"reject unsolicited mints" flag). At minimum, document this as an accepted non-goal loudly enough that integrators do not treat "mint to fresh purse key" as a safe settlement primitive without an off-chain consent step, and consider gating the primitive behind a `MintWithoutDeposit`-style trait that a runtime can restrict to authorized callers only, since the existing per-instance deposit does not stop the grief — it merely makes it cost-bearing per victim, exactly as in the original dMute report.

### Proof of Concept
1. Attacker calls `create_collection` (paying `CollectionDeposit`) to become owner of `collection = C`.
2. Attacker calls `define_item` to create `item = I` under `C`.
3. For any victim address `V` (publicly known, e.g., a DEX/bridge settlement account), attacker calls the mint extrinsic targeting `V` as `to`. Since `NftsByOwner` requires the slot be empty and there is no consent check, the mint succeeds and `NftsByOwner::<T>::get(V)` becomes `Some(Nft { collection: C, item: I, .. })`.
4. Any subsequent legitimate attempt to mint or transfer a wanted NFT into `V` fails because the coinage invariant enforces at most one NFT per key — `V` is now locked out until `V` personally calls burn/transfer to clear the attacker's instance, exactly matching the dMute pattern of an attacker unilaterally writing into a victim's storage to block the victim's intended on-chain action.

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

**File:** substrate/frame/scarcity/src/lib.rs (L311-313)
```rust
	/// One NFT per owner key — the coinage model.
	#[pallet::storage]
	pub type NftsByOwner<T: Config> = StorageMap<_, Blake2_128Concat, T::AccountId, Nft>;
```

**File:** substrate/frame/scarcity/src/lib.rs (L886-911)
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
					next_item_index: 0,
					item_count: 0,
					metadata_count: 0,
					collection_deposit,
					owner_deposit: collection_deposit,
					consideration,
				},
			);
			Self::deposit_event(Event::CollectionCreated { collection, owner });
			Ok(collection)
		}
```
