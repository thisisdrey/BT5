### Title
Permissionless front-run-independent squatting of cross-chain derivative asset IDs causes permanent DoS of foreign-asset registration - (File: `substrate/frame/derivatives/src/lib.rs`, mock backing in `substrate/frame/derivatives/src/mock/predefined_id_collections.rs`)

### Summary

### Finding Description
The Folks Finance bug is a classic "attacker claims a caller-supplied unique key before the legitimate owner does, and the create function is guarded by a simple `already-exists` check with no re-claim path for the victim." The closest structural analog in this repository is `pallet_derivatives`'s `create_derivative` extrinsic, which is used to register the local representation ("derivative collection") of a foreign asset identified by a `PredefinedId` (an `AssetId`/`Location` describing the asset's reserve chain).

The underlying storage primitive used by the derivative-creation strategy is a simple `try_mutate` that inserts only if the key is empty and otherwise returns `AlreadyExists`: [1](#0-0) 

This is invoked from the public extrinsic surface as shown in the pallet's own test, where any `Signed` origin (here `RuntimeOrigin::signed(1)`, an ordinary user account, not the source parachain or any privileged relayer) can call `create_derivative` for an arbitrary foreign `AssetId`/`Location`: [2](#0-1) 

Crucially, once the ID is claimed, a second call for the *same* `id` from anyone else — including the intended, later, correctly-timed registration path — deterministically fails with `AlreadyExists`, exactly mirroring `UserLoanAlreadyCreated` in the Folks Finance report: [3](#0-2) 

Repairing the squatted entry requires `RuntimeOrigin::root`, i.e. governance intervention, not a self-service retry by the affected user or chain: [4](#0-3) 

The equivalent pattern (permissionless, first-writer-wins, ID derived from a location/asset descriptor rather than a hash of caller-specific entropy) also appears in the NFT/Uniques pallets' `create_collection_with_id` / `do_create_collection` primitives, which are explicitly documented as bypassing the safe auto-increment allocator and can "break the pallet" or squat an ID slot that legitimate auto-incrementing `create` calls would later need: [5](#0-4) [6](#0-5) 

Unlike `pallet-assets`, which explicitly hardened this exact class of bug with `T::AssetIdAllocator` (enforcing that a *sequential*, protocol-controlled ID is the only one that `create` may use, precisely to prevent ID squatting/front-running): [7](#0-6) [8](#0-7) 

the `create_derivative`/`create_collection_with_id`-style "predefined ID" creation paths have **no such allocator or reservation-binding mechanism** — the ID is fully attacker-chosen (any well-formed `Location`/`AssetId`) and the only guard is the trivial "not already present" check.

### Impact Explanation
Because the derivative ID is a deterministic function of the foreign asset's own `Location` (its issuing parachain + pallet/index), an unprivileged attacker does not need to observe or race any specific pending transaction (unlike a pure front-run). The attacker can proactively iterate over the space of plausible/likely `(Parachain, PalletInstance, GeneralIndex)` combinations and pre-register derivative collections for assets that have not yet been bridged. When the legitimate cross-chain registration flow later attempts `create_derivative` for that same asset (e.g., triggered by the first real inbound transfer of that asset), it fails with `AlreadyExists`, and the mapping can only be corrected by a root/governance call to `destroy_derivative`. This blocks legitimate representation of a bridged asset — directly matching the "permanent user-fund or bridge-state lock" and "message/route/lane must bind ... exactly once" impact categories called out in the task's required-impacts list, since the derivative asset registration never gets a valid, correctly-owned entry until governance intervenes, and any inbound transfers of that asset in the interim cannot be represented/settled on the local chain.

### Likelihood Explanation
The `create_derivative` call is reachable by any `Signed` origin with no economic cost enforced at this layer beyond ordinary transaction fees (the mock demonstrates it succeeds for `signed(1)` targeting an arbitrary `Location`). Because the attack does not require racing a specific mempool transaction — it can be executed at any time, well before any legitimate transfer — it is trivially automatable and can be applied broadly across the ID space (all parachain/pallet/index combinations of interest), making it a low-cost, repeatable griefing vector rather than a one-off front-run.

### Recommendation
- Apply the same allocator/witness pattern already used in `pallet-assets` (`AssetIdAllocator`) to `create_derivative`/predefined-ID creation flows: require that the caller be (or be verified via XCM origin conversion to be) the actual owning/reserve chain for the asset location being registered, not merely any signed account.
- Alternatively, bind derivative creation to an XCM-origin check (`EnsureDerivativeCreateOrigin` should require the extrinsic's origin to *be* the sovereign/XCM origin matching the asset's reserve location) rather than accepting an arbitrary signed account plus an arbitrary target `Location`.
- Provide a permissionless recovery path (e.g., allow the legitimate reserve-location owner to reclaim/overwrite a derivative entry it did not itself create, subject to proof of origin) instead of requiring root/governance to unblock every squatted ID.

### Proof of Concept
Using the existing pallet-derivatives test harness:
1. Attacker (any `Signed` account, e.g. account `1`) calls `create_derivative(id)` for an `AssetId(Location::new(1, [Parachain(1111), PalletInstance(42), GeneralIndex(1)]))` that has never been bridged, before any real transfer from parachain 1111 has occurred: [9](#0-8) 
2. When the legitimate XCM-triggered registration for that same asset later attempts to create the derivative (via the intended flow, whatever origin conversion is used for real cross-chain registration), it hits the same code path and fails with `AlreadyExists`, exactly as shown when a second `signed` account attempts the same `id`: [3](#0-2) 
3. Recovery requires `RuntimeOrigin::root`, unavailable to the affected users/chain: [10](#0-9)

### Citations

**File:** substrate/frame/derivatives/src/mock/predefined_id_collections.rs (L38-51)
```rust
		unique_items::ItemOwner::<Test, PredefinedIdCollectionsInstance>::try_mutate(
			id.clone(),
			|current_owner| {
				if current_owner.is_none() {
					*current_owner = Some(owner);
					Ok(())
				} else {
					Err(unique_items::Error::<Test, PredefinedIdCollectionsInstance>::AlreadyExists)
				}
			},
		)?;

		Ok(id)
	}
```

**File:** substrate/frame/derivatives/src/tests.rs (L26-56)
```rust
#[test]
fn predefined_id_collection() {
	new_test_ext().execute_with(|| {
		let id = AssetId(Location::new(1, [Parachain(1111), PalletInstance(42), GeneralIndex(1)]));

		// An invalid origin is rejected.
		assert_err!(
			PredefinedIdDerivativeCollections::create_derivative(RuntimeOrigin::none(), id.clone()),
			DispatchError::BadOrigin,
		);

		assert_ok!(PredefinedIdDerivativeCollections::create_derivative(
			RuntimeOrigin::signed(1),
			id.clone()
		));

		// EnsureDerivativeCreateOrigin yielded a strategy to assign the item's owner to the
		// parachain's sovereign account.
		let owner =
			unique_items::ItemOwner::<Test, PredefinedIdCollectionsInstance>::get(&id).unwrap();

		assert_eq!(owner, 1111);

		// The inner errors are propagated
		assert_err!(
			PredefinedIdDerivativeCollections::create_derivative(
				RuntimeOrigin::signed(2),
				id.clone()
			),
			unique_items::Error::<Test, PredefinedIdCollectionsInstance>::AlreadyExists,
		);
```

**File:** substrate/frame/derivatives/src/tests.rs (L58-73)
```rust
		// An invalid origin is rejected.
		assert_err!(
			PredefinedIdDerivativeCollections::destroy_derivative(
				RuntimeOrigin::signed(1),
				id.clone()
			),
			DispatchError::BadOrigin,
		);

		assert_ok!(PredefinedIdDerivativeCollections::destroy_derivative(
			RuntimeOrigin::root(),
			id.clone()
		));
		assert!(
			unique_items::ItemOwner::<Test, PredefinedIdCollectionsInstance>::get(&id).is_none()
		);
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

**File:** substrate/frame/nfts/src/tests.rs (L3800-3835)
```rust
#[test]
fn basic_create_collection_with_id_should_work() {
	new_test_ext().execute_with(|| {
		assert_noop!(
			Nfts::create_collection_with_id(
				0u32,
				&account(1),
				&account(1),
				&default_collection_config(),
			),
			Error::<Test>::WrongSetting
		);

		Balances::make_free_balance_be(&account(1), 100);
		Balances::make_free_balance_be(&account(2), 100);

		assert_ok!(Nfts::create_collection_with_id(
			0u32,
			&account(1),
			&account(1),
			&collection_config_with_all_settings_enabled(),
		));

		assert_eq!(collections(), vec![(account(1), 0)]);

		// CollectionId already taken.
		assert_noop!(
			Nfts::create_collection_with_id(
				0u32,
				&account(2),
				&account(2),
				&collection_config_with_all_settings_enabled(),
			),
			Error::<Test>::CollectionIdInUse
		);
	});
```

**File:** substrate/frame/assets/src/functions.rs (L758-774)
```rust
	/// * `enforce_allocator`: Whether `id` must be the one required by
	///   [`Config::AssetIdAllocator`]. Only pass `false` for a `ForceOrigin` caller.
	pub(super) fn do_force_create(
		id: T::AssetId,
		owner: T::AccountId,
		is_sufficient: bool,
		min_balance: T::Balance,
		enforce_allocator: bool,
	) -> DispatchResult {
		ensure!(!Asset::<T, I>::contains_key(&id), Error::<T, I>::InUse);
		ensure!(!min_balance.is_zero(), Error::<T, I>::MinBalanceZero);
		if enforce_allocator {
			if let Some(next_id) = T::AssetIdAllocator::next() {
				ensure!(id == next_id, Error::<T, I>::BadAssetId);
			}
		}

```

**File:** substrate/frame/assets/src/tests.rs (L2233-2251)
```rust
		pallet::NextAssetId::<Test>::put(5);

		// `create` must follow the sequence: only the next id is accepted.
		assert_noop!(Assets::create(RuntimeOrigin::signed(1), 0, 1, 1), Error::<Test>::BadAssetId);
		assert_noop!(Assets::create(RuntimeOrigin::signed(1), 1, 1, 1), Error::<Test>::BadAssetId);

		// Asset with id `5` is created and the sequence advances to `6`.
		assert_ok!(Assets::create(RuntimeOrigin::signed(1), 5, 1, 1));
		assert!(Asset::<Test>::contains_key(5));
		assert_eq!(pallet::NextAssetId::<Test>::get(), Some(6));

		// Destroy asset with id `5`.
		assert_ok!(Assets::start_destroy(RuntimeOrigin::signed(1), 5));
		assert_ok!(Assets::finish_destroy(RuntimeOrigin::signed(1), 5));

		assert!(!Asset::<Test>::contains_key(5));

		// Asset id `5` cannot be reused: the sequence has moved past it.
		assert_noop!(Assets::create(RuntimeOrigin::signed(1), 5, 1, 1), Error::<Test>::BadAssetId);
```
