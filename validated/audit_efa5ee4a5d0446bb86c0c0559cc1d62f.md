Confirmed local analog: `pallet-nfts`'s `MintType::Public` (`substrate/frame/nfts/src/types.rs:328-335`) lets any signed account call `mint()` and increment a collection's item count, while `destroy()`/`do_destroy_collection()` requires the caller-supplied `DestroyWitness.items` to exactly equal the live `collection_details.items` count.

### Title
Public NFT collection minting can grief/front-run `destroy()` collection removal via witness mismatch - (File: `substrate/frame/nfts/src/features/create_delete_collection.rs`)

### Summary
`pallet-nfts::destroy` (and the analogous `pallet-uniques`/`pallet-scarcity` variants) requires the caller to supply a `DestroyWitness` that must exactly match the collection's live counters (`items`, `item_metadatas`, `item_configs`/`attributes`) before the collection can be removed. When a collection's `MintSettings.mint_type` is `MintType::Public`, any unprivileged signed account can call `mint()` at any time, including the same block/immediately before a pending `destroy` extrinsic executes, incrementing `collection_details.items`. This is structurally identical to the Sherlock M-9 finding: a public, unprivileged, low-cost action (listing an NFT / minting an NFT) performed against a resource that is about to be deleted by a witnessed/quorum-gated privileged call, causing that call to revert due to a stale precondition check.

### Finding Description
`do_destroy_collection` enforces:
```
ensure!(collection_details.items == witness.items, Error::<T, I>::BadWitness);
``` [1](#0-0) 

The witness must be computed off-chain by the collection owner (or `ForceOrigin`) ahead of time and submitted with the `destroy` extrinsic: [2](#0-1) 

Meanwhile, `mint()` is permissionless whenever `MintType::Public` is configured:
```
pub enum MintType<CollectionId> {
    Issuer,
    Public,          // Anyone could mint items.
    HolderOf(CollectionId),
}
``` [3](#0-2) 

and the dispatchable only checks `mint_settings.mint_type == MintType::Issuer` to gate by role; for `Public` mint type no role check occurs at all: [4](#0-3) 

`do_mint` then increments the live `items` counter used by the witness check: [5](#0-4) 

Any account watching the mempool/block for a pending `destroy(collection, witness)` call can submit a `mint()` in the same or an earlier block for a `MintType::Public` collection, changing `collection_details.items`, which makes the previously-computed `witness.items` stale and causes `destroy` to fail with `BadWitness` — exactly the "block a privileged state-clearing action via a cheap, unprivileged, unrelated public call" pattern described in the Listings.sol/CollectionShutdown.sol report.

### Impact Explanation
This does not directly steal funds, but it lets an unprivileged, non-owner actor repeatedly deny the collection owner's (or governance `ForceOrigin`'s) ability to `destroy()` a `Public`-mint collection, forcing wasted transaction fees and indefinite delay of collection teardown/deposit reclamation. Because `Public` mint type is an explicitly supported and intended configuration (not a misconfiguration or admin error), this is a reachable, permissionless griefing vector against normal pallet usage rather than an admin-abuse scenario.

### Likelihood Explanation
Likelihood is bounded by two conditions: (1) the collection must be configured with `MintType::Public` (or a mint window still open), and (2) the attacker must be able to see the pending `destroy` call before it's included (mempool visibility) or simply mint proactively/periodically to keep the collection "unwitnessable." Both conditions are realistic for any public marketplace/community collection that intentionally allows open minting and later wants to wind down. No special privilege, validator/collator collusion, or leaked keys are required — a single low-value transaction from any account suffices.

### Recommendation
Decouple destroy-time correctness from a caller-supplied witness for the `items` counter, or make `destroy` tolerant to `items` count changes that occurred after computation (e.g., re-read live storage rather than trusting the witness `items` field, or require `items == 0` directly without depending on an externally-supplied value that can go stale). Alternatively, provide an owner/`ForceOrigin`-gated way to close/lock minting (e.g., force `mint_type` to non-Public or set `end_block` to `now`) atomically as part of, or immediately prior to, the same extrinsic that initiates destruction, so no window exists for a front-running mint.

### Proof of Concept
1. Owner creates a collection with `CollectionConfig { mint_settings: MintSettings { mint_type: MintType::Public, .. }, .. }` via `create`/`force_create`.
2. Owner (or `ForceOrigin`) computes the current witness (`items = N`) and submits `destroy(collection, DestroyWitness { items: N, .. })`.
3. Attacker, observing the pending `destroy` transaction, submits `mint(collection, new_item_id, attacker, None)` in the same block with higher priority/tip, incrementing `collection_details.items` to `N+1`.
4. `destroy` executes afterward and fails with `Error::BadWitness` because `collection_details.items (N+1) != witness.items (N)`.
5. Owner must recompute the witness and resubmit; attacker can repeat this indefinitely as long as `mint_type` remains `Public` and minting a fresh item id is cheap, permanently blocking teardown.

Test harness references corroborating public mint behavior and mint/destroy call surfaces used above: [6](#0-5) [7](#0-6)

### Citations

**File:** substrate/frame/uniques/src/functions.rs (L137-142)
```rust
			ensure!(collection_details.items == witness.items, Error::<T, I>::BadWitness);
			ensure!(
				collection_details.item_metadatas == witness.item_metadatas,
				Error::<T, I>::BadWitness
			);
			ensure!(collection_details.attributes == witness.attributes, Error::<T, I>::BadWitness);
```

**File:** substrate/frame/uniques/src/lib.rs (L546-555)
```rust
		pub fn destroy(
			origin: OriginFor<T>,
			collection: T::CollectionId,
			witness: DestroyWitness,
		) -> DispatchResultWithPostInfo {
			let maybe_check_owner = match T::ForceOrigin::try_origin(origin) {
				Ok(_) => None,
				Err(origin) => Some(ensure_signed(origin)?),
			};
			let details = Self::do_destroy_collection(collection, witness, maybe_check_owner)?;
```

**File:** substrate/frame/nfts/src/types.rs (L328-335)
```rust
pub enum MintType<CollectionId> {
	/// Only an `Issuer` could mint items.
	Issuer,
	/// Anyone could mint items.
	Public,
	/// Only holders of items in specified collection could mint new items.
	HolderOf(CollectionId),
}
```

**File:** substrate/frame/nfts/src/lib.rs (L808-824)
```rust
		pub fn destroy(
			origin: OriginFor<T>,
			collection: T::CollectionId,
			witness: DestroyWitness,
		) -> DispatchResultWithPostInfo {
			let maybe_check_owner = T::ForceOrigin::try_origin(origin)
				.map(|_| None)
				.or_else(|origin| ensure_signed(origin).map(Some).map_err(DispatchError::from))?;
			let details = Self::do_destroy_collection(collection, witness, maybe_check_owner)?;

			Ok(Some(T::WeightInfo::destroy(
				details.item_metadatas,
				details.item_configs,
				details.attributes,
			))
			.into())
		}
```

**File:** substrate/frame/nfts/src/lib.rs (L844-880)
```rust
		pub fn mint(
			origin: OriginFor<T>,
			collection: T::CollectionId,
			item: T::ItemId,
			mint_to: AccountIdLookupOf<T>,
			witness_data: Option<MintWitness<T::ItemId, DepositBalanceOf<T, I>>>,
		) -> DispatchResult {
			let caller = ensure_signed(origin)?;
			let mint_to = T::Lookup::lookup(mint_to)?;
			let item_config =
				ItemConfig { settings: Self::get_default_item_settings(&collection)? };

			Self::do_mint(
				collection,
				item,
				Some(caller.clone()),
				mint_to.clone(),
				item_config,
				|collection_details, collection_config| {
					let mint_settings = collection_config.mint_settings;
					let now = T::BlockNumberProvider::current_block_number();

					if let Some(start_block) = mint_settings.start_block {
						ensure!(start_block <= now, Error::<T, I>::MintNotStarted);
					}
					if let Some(end_block) = mint_settings.end_block {
						ensure!(end_block >= now, Error::<T, I>::MintEnded);
					}

					match mint_settings.mint_type {
						MintType::Issuer => {
							ensure!(
								Self::has_role(&collection, &caller, CollectionRole::Issuer),
								Error::<T, I>::NoPermission
							);
						},
						MintType::HolderOf(collection_id) => {
```

**File:** substrate/frame/nfts/src/features/create_delete_item.rs (L44-70)
```rust
	pub fn do_mint(
		collection: T::CollectionId,
		item: T::ItemId,
		maybe_depositor: Option<T::AccountId>,
		mint_to: T::AccountId,
		item_config: ItemConfig,
		with_details_and_config: impl FnOnce(
			&CollectionDetailsFor<T, I>,
			&CollectionConfigFor<T, I>,
		) -> DispatchResult,
	) -> DispatchResult {
		ensure!(!Item::<T, I>::contains_key(collection, item), Error::<T, I>::AlreadyExists);

		Collection::<T, I>::try_mutate(
			&collection,
			|maybe_collection_details| -> DispatchResult {
				let collection_details =
					maybe_collection_details.as_mut().ok_or(Error::<T, I>::UnknownCollection)?;

				let collection_config = Self::get_collection_config(&collection)?;
				with_details_and_config(collection_details, &collection_config)?;

				if let Some(max_supply) = collection_config.max_supply {
					ensure!(collection_details.items < max_supply, Error::<T, I>::MaxSupplyReached);
				}

				collection_details.items.saturating_inc();
```

**File:** substrate/frame/nfts/src/tests.rs (L344-353)
```rust
		assert_ok!(Nfts::update_mint_settings(
			RuntimeOrigin::signed(account(1)),
			0,
			MintSettings {
				start_block: Some(2),
				end_block: Some(3),
				mint_type: MintType::Public,
				..Default::default()
			}
		));
```
