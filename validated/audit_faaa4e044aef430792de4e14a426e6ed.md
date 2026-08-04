### Title
Permissionless unsolicited NFT mint permanently occupies a victim's purse key with no receiver consent - (File: `substrate/frame/scarcity/src/lib.rs`)

### Summary
`pallet-scarcity`'s `mint` extrinsic lets **any signed account that owns a collection** — which any account can create for itself via `create_collection` — deliver an NFT instance directly into an arbitrary victim `AccountId` ("purse key") with **no consent, capability, or receiver check** at all. Because the pallet's model restricts a purse key to at most one live NFT, this unsolicited delivery permanently occupies the victim's key until the victim notices and burns the unwanted instance. This is the direct Substrate analog of the ERC-721 `transferFrom()` vs `safeTransferFrom()` issue: the outgoing/incoming transfer path skips any acceptance check on the recipient, and the recipient has no way to reject the asset before it lands and blocks their account.

### Finding Description
`create_collection` is open to any signed origin [1](#0-0) , so becoming a "collection owner" with mint rights requires no privilege beyond an ordinary signed transaction.

The `mint` call then allows that owner to mint into **any** destination account, and the pallet's own doc comment explicitly states the destination gives no consent: [2](#0-1) 

The module-level docs confirm this is a deliberate design gap left for higher layers to fix, and describe the exact griefing consequence: an unsolicited instance blocks the purse key until burned or transferred away: [3](#0-2) 

Because `NftsByOwner` enforces a strict one-instance-per-key invariant (checked via `AddressOccupied`/similar guards in `transfer` and `force_transfer`, e.g. [4](#0-3) ), once an attacker mints into a victim's empty key, that key cannot receive any other NFT — from this collection or any other scarcity collection — until the holder actively burns the unwanted instance. There is no analog of `onERC721Received`/receiver-hook validation, no opt-in registry, and no way for the destination to reject the incoming instance before storage is mutated. This exactly mirrors the reported bug class: the transfer/mint path completes state mutation into the recipient without verifying the recipient can or wants to hold the asset, and existing guards (`AddressOccupied`, `SelfTransfer`, `ItemLocked`) only protect the *sender's* state machine and the "one instance per key" invariant — they do nothing to protect an unwilling *recipient* from being targeted in the first place.

### Impact Explanation
This is a live, chain-affecting griefing/DoS primitive: any account can permanently lock any other account's ability to receive NFTs from any scarcity collection by unilaterally minting an unwanted instance into their purse key. This matches the "permanent user-fund or bridge-state lock" and "public underpriced work that degrades... processing" impact categories — the victim's purse key becomes unusable for NFT receipt for other collections/purposes without any action or consent on their part, and remediation requires the victim to discover and burn an instance they never asked for. Since the mint deposit is paid by the attacker (collection owner), the attack is cheap and repeatable against many victim accounts, and because minting is feeless-agnostic to the victim (their key doesn't even need a System account, per the pallet's coinage-style design), it can target completely inert/unused addresses en masse.

### Likelihood Explanation
High. The only prerequisite is calling `create_collection` (open to any signed account), `define_item`, and then `mint` targeting arbitrary destination `AccountId`s — all ordinary, unprivileged, permissionless dispatchables. No governance, no admin role in the runtime sense, no malicious relayer/validator/collator is required; the "privilege" here is simply being the creator of one's own collection, which any attacker can instantiate for free (aside from the collection/item storage deposit they pay themselves).

### Recommendation
Require destination consent before an NFT instance lands in a purse key that has not opted in, e.g.:
- Add a per-account "accept unsolicited mints" flag/registry that `mint` (and non-owner-initiated deliveries) must check before inserting into `NftsByOwner`, or
- Require the destination to pre-authorize incoming instances (e.g., via a signed "accept" extrinsic or an allow-list per collection), analogous to `safeTransferFrom`'s `onERC721Received` callback, so a recipient's purse key cannot be involuntarily and permanently occupied by an unwanted asset.

### Proof of Concept
1. Attacker calls `Scarcity::create_collection(attacker)` → becomes owner of `collection_id`.
2. Attacker calls `Scarcity::define_item(attacker, collection_id, metadata)` to register an item.
3. Attacker calls `Scarcity::mint(attacker, collection_id, item_id, victim_account, metadata)`.
4. `do_mint` succeeds because `victim_account`'s purse key is empty (`!NftsByOwner::contains_key(victim_account)`), inserting an instance the victim never requested.
5. `victim_account` is now blocked from receiving any other scarcity NFT (from this or another collection) until it actively discovers and calls `Scarcity::burn` on the unwanted instance — an action the victim did not choose to need, at a time chosen entirely by the attacker.

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

**File:** substrate/frame/scarcity/src/lib.rs (L542-547)
```rust
		#[pallet::call_index(0)]
		#[pallet::weight(T::WeightInfo::create_collection())]
		pub fn create_collection(origin: OriginFor<T>) -> DispatchResult {
			let owner = ensure_signed(origin)?;
			Self::do_create_collection(owner).map(|_| ())
		}
```

**File:** substrate/frame/scarcity/src/lib.rs (L564-582)
```rust
		/// Mint an instance of an immutable item definition into an empty purse key.
		///
		/// The destination gives no consent; any empty key is a valid target. See the module
		/// documentation on purse-key occupancy.
		///
		/// `metadata` contains instance-specific overrides. Item metadata remains the shared
		/// default for every instance minted from the definition.
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

**File:** substrate/frame/scarcity/src/lib.rs (L590-595)
```rust
		pub fn transfer(origin: OriginFor<T>, to: T::AccountId) -> DispatchResultWithPostInfo {
			let Ok(Origin::Nft { owner, nft }) = origin.into() else {
				return Err(DispatchError::BadOrigin.into());
			};
			ensure!(to != owner, Error::<T>::SelfTransfer);
			ensure!(!NftsByOwner::<T>::contains_key(&to), Error::<T>::AddressOccupied);
```
