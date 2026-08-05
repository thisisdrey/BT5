## Analysis

The Lens report's core broken invariant: a non-privileged-but-permitted actor (whitelisted creator) can call a permissionless/standard identifier-minting function (`mintHandle`) that consumes a namespace slot needed later by a fixed-identifier migration/claim path (`batchMigrateProfiles` → `migrateHandle`), and because identifiers can never be reused once minted, the fixed-id operation permanently reverts (`"ERC721: token already minted"`).

I searched for an analogous "unprivileged claim of a shared identifier space blocks a distinct fixed-id creation path" pattern across parachain registration, asset creation, and NFT collection creation. `paras_registrar::reserve()` and `pallet_assets::create()` (with `AutoIncAssetId`) both strictly enforce sequential/next-id allocation, which prevents squatting an arbitrary future id — these are not exploitable. `ForeignAssets::create` is gated behind `ForeignCreators`/XCM origin checks tied to the asset's own location, so a caller cannot squat someone else's namespace either.

`pallet-nfts` is different. It exposes two disjoint id-issuance paths sharing the same `NextCollectionId` sequence space:
- `create`/`force_create` (public dispatchables) allocate `T::CollectionId` sequentially via `NextCollectionId`.
- `create_collection_with_id` (only reachable via the `Create` trait, e.g. from `pallet-nft-fractionalization` or other tightly-coupled callers) creates a collection at an **arbitrary caller-supplied id**, explicitly documented as bypassing the sequence.

The pallet's own doc comment on `Config::CollectionId` warns: *"the `create_collection_with_id` function... can claim a value in the ID sequence"* — i.e., the developers know these two paths collide on the same identifier space, and there is no reservation/guard preventing the sequential permissionless path from consuming an id that a fixed-id caller intends to use, or vice versa.

### Title
Unprivileged sequential `Nfts::create` can squat a `CollectionId` a fixed-id creation path needs, permanently blocking it - (File: substrate/frame/nfts/src/lib.rs)

### Summary
`pallet-nfts` allocates collection ids for the public `create`/`force_create` extrinsics from a single auto-incrementing counter (`NextCollectionId`), while a second, disjoint creation path, `create_collection_with_id` (exposed via the `nonfungibles_v2::Create` trait and used by callers such as `pallet-nft-fractionalization`), inserts a collection at any caller-chosen id without consulting or advancing that counter. Because both paths write into the same `Collection<T,I>` storage map and any signed account can permissionlessly drive the sequential counter forward (`create`), an ordinary user can pre-empt a specific `CollectionId` that a fixed-id caller later needs, causing that caller's creation to permanently fail with `CollectionIdInUse`.

### Finding Description
`Nfts::create` (public, `EnsureSigned`-gated in most runtime configs, e.g. `AsEnsureOriginWithArg<EnsureSigned<AccountId>>`) computes the next collection id purely from `NextCollectionId::<T,I>::get().or(T::CollectionId::initial_value())` and then calls `do_create_collection`, which only checks `!Collection::<T,I>::contains_key(collection)`: [1](#0-0) 

`do_create_collection` unconditionally inserts into `Collection` if the id is free: [2](#0-1) 

Separately, `create_collection_with_id` performs the same insert for an **arbitrary, caller-supplied** id, explicitly bypassing the auto-increment sequence: [3](#0-2) 

The pallet's own `Config::CollectionId` documentation acknowledges the collision risk between the two paths: [4](#0-3) 

Because `NextCollectionId` only advances forward and never re-checks a "reserved for the future fixed-id caller" range, any signed account can permissionlessly call `create` repeatedly to walk the sequence up to and claim a specific id (e.g., the id a `pallet-nft-fractionalization` new asset registration, an offline-computed migration id, or any other integration relying on a deterministic/expected `CollectionId` is about to use). Once claimed, `Collection::<T,I>::contains_key(collection)` is `true` forever (the squatter fully owns and controls that collection; nothing forces them to destroy it), so the later fixed-id call to `create_collection_with_id` deterministically hits `Error::<T,I>::CollectionIdInUse` and can never succeed for that id — exactly mirroring the Lens `mintHandle()`/`batchMigrateProfiles()` collision: two different creation entry points share one identifier space, one is fully permissionless and sequential, the other needs a specific value, and there is no on-chain guard reserving the value for the legitimate caller ahead of time.

### Impact Explanation
This is a public, underpriced griefing primitive with permanent state-lock impact: any account with the `CollectionDeposit` (only) can create nftS::create for a moderate number of blocks/collections to walk `NextCollectionId` up to and occupy a target id, permanently denying any consumer that depends on `create_collection_with_id` reaching that exact id (e.g. `pallet-nft-fractionalization`'s collection/asset pairing, or any runtime-specific migration/bridge logic that expects a specific `CollectionId`). This matches the "permanent user-fund or bridge-state lock" / "public underpriced work that degrades... processing" impact classes: the squatted collection can never be freed for the intended purpose without governance intervention (force-destroying the squatter's collection), and the squatter recovers their deposit at will, making the attack essentially free apart from transaction fees and the (refundable) `CollectionDeposit`.

### Likelihood Explanation
Likelihood is moderate-to-high wherever a chain configures `pallet-nfts` with a permissionless `CreateOrigin` (e.g., `AsEnsureOriginWithArg<EnsureSigned<AccountId>>`, as used on Asset Hub Westend/Rococo and the staking-async parachain runtime) alongside any consumer of the `create_collection_with_id`/`Create::create_collection_with_id` API that expects to claim a specific, predictable id (such as `pallet-nft-fractionalization`, which is deployed on Asset Hub runtimes). No malicious node, validator, relayer, or admin action is required — a single unprivileged signed account with a modest balance for repeated `CollectionDeposit`s and gas is sufficient.

### Recommendation
- Reserve a disjoint id range/namespace for callers of `create_collection_with_id` that is excluded from the auto-increment sequence used by `create`/`force_create` (analogous to how `pallet-assets`' `AutoIncAssetId::advance_from` treats ids below `NextAssetId` as a deliberately reserved range for forced assignment).
- Alternatively, require `create_collection_with_id` callers to pre-register/reserve their intended id (mirroring the Lens fix of validating against the pre-existing identifier before allowing a colliding permissionless mint) so a race against the sequential public path cannot occur.
- At minimum, document and enforce (via a runtime-level check, not just a doc comment) that any pallet relying on `create_collection_with_id` cannot be deployed alongside a permissionless `create` unless the ids are proven disjoint.

### Proof of Concept
1. Deploy a runtime with `pallet-nfts::Config::CreateOrigin = AsEnsureOriginWithArg<EnsureSigned<AccountId>>` and a consumer pallet (e.g. `pallet-nft-fractionalization`) that will later call `Nfts::create_collection_with_id(target_id, ...)` for a specific `target_id` known or predictable off-chain (e.g., sequential item registration order).
2. Attacker account, with normal signed origin, repeatedly calls `Nfts::create(origin, admin, config)` (paying `CollectionDeposit` each time, refundable via `destroy`), driving `NextCollectionId` forward until it creates a collection exactly at `target_id`.
3. When the legitimate consumer later invokes `create_collection_with_id(target_id, ...)`, `do_create_collection` hits `ensure!(!Collection::<T, I>::contains_key(collection), Error::<T, I>::CollectionIdInUse)` and reverts permanently for that id, exactly as `batchMigrateProfiles()` reverts with `"ERC721: token already minted"` once the handle has been squatted. [2](#0-1)

### Citations

**File:** substrate/frame/nfts/src/lib.rs (L135-144)
```rust
		/// Identifier for the collection of item.
		///
		/// SAFETY: The functions in the `Incrementable` trait are fallible. If the functions
		/// of the implementation both return `None`, the automatic CollectionId generation
		/// should not be used. So the `create` and `force_create` extrinsics and the
		/// `create_collection` function will return an `UnknownCollection` Error. Instead use
		/// the `create_collection_with_id` function. However, if the `Incrementable` trait
		/// implementation has an incremental order, the `create_collection_with_id` function
		/// should not be used as it can claim a value in the ID sequence.
		type CollectionId: Member + Parameter + MaxEncodedLen + Copy + Incrementable;
```

**File:** substrate/frame/nfts/src/lib.rs (L710-741)
```rust
		#[pallet::call_index(0)]
		#[pallet::weight(T::WeightInfo::create())]
		pub fn create(
			origin: OriginFor<T>,
			admin: AccountIdLookupOf<T>,
			config: CollectionConfigFor<T, I>,
		) -> DispatchResult {
			let collection = NextCollectionId::<T, I>::get()
				.or(T::CollectionId::initial_value())
				.ok_or(Error::<T, I>::UnknownCollection)?;

			let owner = T::CreateOrigin::ensure_origin(origin, &collection)?;
			let admin = T::Lookup::lookup(admin)?;

			// DepositRequired can be disabled by calling the force_create() only
			ensure!(
				!config.has_disabled_setting(CollectionSetting::DepositRequired),
				Error::<T, I>::WrongSetting
			);

			Self::do_create_collection(
				collection,
				owner.clone(),
				admin.clone(),
				config,
				T::CollectionDeposit::get(),
				Event::Created { collection, creator: owner, owner: admin },
			)?;

			Self::set_next_collection_id(collection);
			Ok(())
		}
```

**File:** substrate/frame/nfts/src/features/create_delete_collection.rs (L36-44)
```rust
	pub fn do_create_collection(
		collection: T::CollectionId,
		owner: T::AccountId,
		admin: T::AccountId,
		config: CollectionConfigFor<T, I>,
		deposit: DepositBalanceOf<T, I>,
		event: Event<T, I>,
	) -> DispatchResult {
		ensure!(!Collection::<T, I>::contains_key(collection), Error::<T, I>::CollectionIdInUse);
```

**File:** substrate/frame/nfts/src/impl_nonfungibles.rs (L186-213)
```rust
	/// Create a collection of nonfungible items with `collection` Id to be owned by `who` and
	/// managed by `admin`. Should be only used for applications that do not have an
	/// incremental order for the collection IDs and is a replacement for the auto id creation.
	///
	///
	/// SAFETY: This function can break the pallet if it is used in combination with the auto
	/// increment functionality, as it can claim a value in the ID sequence.
	fn create_collection_with_id(
		collection: T::CollectionId,
		who: &T::AccountId,
		admin: &T::AccountId,
		config: &CollectionConfigFor<T, I>,
	) -> Result<(), DispatchError> {
		// DepositRequired can be disabled by calling the force_create() only
		ensure!(
			!config.has_disabled_setting(CollectionSetting::DepositRequired),
			Error::<T, I>::WrongSetting
		);

		Self::do_create_collection(
			collection,
			who.clone(),
			admin.clone(),
			*config,
			T::CollectionDeposit::get(),
			Event::Created { collection, creator: who.clone(), owner: admin.clone() },
		)
	}
```
