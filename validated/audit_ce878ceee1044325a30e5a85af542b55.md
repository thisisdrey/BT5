Based on the local scan, the strongest analog to the "unsolicited/unwanted NFT lock" bug class from the report is in `substrate/frame/scarcity`, a coinage-style NFT pallet where minting into a destination account requires **no consent from that destination**, and the pallet enforces **at most one NFT per account** — so an unsolicited mint can permanently occupy a victim's only NFT slot.

### Title
Unconsented public `mint` into arbitrary accounts permanently occupies the one-NFT-per-account purse slot, DoS'ing legitimate NFT receipt - (File: `substrate/frame/scarcity/src/lib.rs`)

### Summary
The `pallet-scarcity` module enforces a strict "one NFT per account" (coinage) invariant via `NftsByOwner<T>` [1](#0-0) . Any account that has created a collection (a fully permissionless action, see `create_collection`) can call the public `mint` extrinsic and target any arbitrary, uninvolved account as `to`, with zero consent or opt-in from that account [2](#0-1) . Because each account can hold only one live NFT at a time, an attacker can mint a throwaway/garbage NFT into any target's account, permanently occupying their single purse slot until the *victim* burns it themselves — the same "unsolicited-token locks the account" invariant break as the original ReraiseETHCrowdfund/PartyGovernanceNFT report, just implemented via a different mechanism (single-slot enforcement instead of `_mint`/`safeMint` mismatch).

### Finding Description
`do_mint_inner` only checks that the destination purse is currently empty before inserting the NFT — it performs no consent, allow-list, or opt-in check against `to`: [3](#0-2) 

The dispatchable `mint` is reachable by any signed collection owner, and collection ownership itself is permissionless via `create_collection`: [4](#0-3) [5](#0-4) 

The module documentation explicitly acknowledges this: "Purse keys are coinage-style receiving addresses, not identities: the pallet applies no destination consent. Any collection owner can mint into ... any empty purse key, and because each key holds at most one NFT, an unsolicited instance blocks that key from receiving anything else until its holder burns it or transfers it away." [6](#0-5) 

This is functionally the same broken invariant as the report: a recipient can be handed a token they never asked for, and the pallet's own accounting rule (here: one-NFT-per-account, there: soul-bound non-transferability) makes that token block the account from receiving the NFT it actually wants — a public, underprivileged, unpriced write that degrades the victim's ability to use the system, requiring the victim to spend a `burn` transaction just to undo someone else's unsolicited action.

### Impact Explanation
Any account can be targeted by any other account (no special privilege beyond calling a public, feeless-adjacent `create_collection`/`mint`) and have its single NFT slot occupied with junk, blocking it from receiving a legitimately wanted NFT (e.g., a reward, a claim NFT, a governance credential minted by a runtime built on top of this pallet) until the victim notices and burns the unwanted item. In a runtime where this pallet backs a reward/claim/credential system, this becomes a griefing/DoS primitive against arbitrary users at negligible cost to the attacker (one `mint` call), matching the "permanent user-fund or bridge-state lock" and "public underpriced work" impact classes.

### Likelihood Explanation
High: `create_collection` and `mint` are both public, signed-origin dispatchables with no governance or admin gate; the only requirement is that the caller be the owner of *some* collection, which they can create themselves for free (bar the storage deposit). No malicious validator, collator, relayer, or leaked key is required — this is a pure unprivileged public-entrypoint griefing vector.

### Recommendation
Require destination opt-in/consent before an unsolicited instance can occupy an account's purse slot — analogous to the C4 recommendation of using `_safeMint` or letting the receiver specify an alternate address. Concretely: either (a) require the destination to pre-register acceptance (mirroring the existing `nominate_collection_owner`/`claim_collection_ownership` pending-acceptance pattern already used elsewhere in this same pallet for collection ownership transfer), or (b) let `to` be a claimable escrow reference rather than a live purse-key write, so a target's occupied slot cannot be forced by a third party without their action.

### Proof of Concept
1. Attacker calls `create_collection` (permissionless) to become a collection owner.
2. Attacker calls `define_item` to define an item.
3. Attacker calls `mint(collection, item, to = victim, metadata = [])` where `victim` is any account with an empty purse key [5](#0-4) .
4. `do_mint_inner` inserts the NFT into `NftsByOwner::<T>::insert(&to, nft)` unconditionally once the emptiness check passes [7](#0-6) .
5. Victim's account now holds an unsolicited NFT; any subsequent legitimate `mint`/`transfer` targeting the victim fails with `Error::AddressOccupied` [8](#0-7)  until the victim manually burns the attacker's item.

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

**File:** substrate/frame/scarcity/src/lib.rs (L541-547)
```rust
		/// Create a collection owned by the signer.
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

**File:** substrate/frame/scarcity/src/lib.rs (L1274-1300)
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

			let instance = NextInstanceId::<T>::get();
			let next_instance = instance.checked_add(1).ok_or(Error::<T>::TooManyInstances)?;
			let next_supply = definition.supply.checked_add(1).ok_or(Error::<T>::SupplyOverflow)?;
			let next_live_supply =
				definition.live_supply.checked_add(1).ok_or(Error::<T>::SupplyOverflow)?;
			let now = T::UnixTime::now().as_secs();
			let nft =
				Nft { instance, collection, item, minted_at: now, last_moved: now, state_nonce: 0 };
			let instance_deposit = if with_deposit {
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

**File:** substrate/frame/scarcity/src/tests.rs (L1076-1095)
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
```
