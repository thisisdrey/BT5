## Title
Unbounded per-collection item loop in `pallet-uniques::destroy` can exceed block weight/PoV limits, permanently locking collection deposits - (`substrate/frame/uniques/src/functions.rs`)

### Summary
The Visor `Visor.sol` bug looped over an unbounded `timelockERC721Keys` array inside a single transaction, risking a call that could never fit into a block. `pallet-uniques` has the same structural flaw: `destroy` drains *every* item of an NFT collection in a single extrinsic call with no per-call cap, unlike its sibling pallets `pallet-assets` (which enforces `RemoveItemsLimit` and requires multiple `destroy_accounts`/`destroy_approvals` calls) and `pallet-nfts` (which requires the collection to be emptied of items via individual `burn` calls before `destroy` is even permitted).

### Finding Description
`do_destroy_collection` in `substrate/frame/uniques/src/functions.rs` only checks that the supplied witness matches the collection's actual counts: [1](#0-0) 
It contains **no check that `items == 0`** (unlike `pallet-nfts`'s `do_destroy_collection`, which enforces `Error::CollectionNotEmpty` when `collection_details.items != 0`, see `substrate/frame/nfts/src/features/create_delete_collection.rs` lines 111 and `substrate/frame/nfts/src/tests.rs` lines 236-239). Instead it directly drains all items in one loop: [2](#0-1) 

The dispatchable `destroy` in `substrate/frame/uniques/src/lib.rs` computes its weight entirely from the caller-supplied witness numbers and dispatches unconditionally: [3](#0-2) 

By contrast, `pallet-assets` explicitly bounds per-call work to `T::RemoveItemsLimit` and forces the caller to invoke `destroy_accounts`/`destroy_approvals` repeatedly: [4](#0-3) [5](#0-4) 

`pallet-uniques` has no such incremental mechanism and no mandatory `CollectionMaxSupply` (it is optional, set per-collection at the owner's discretion), so nothing structurally prevents a collection from growing to a size where the single-call `destroy` loop's actual weight/PoV consumption exceeds the runtime's maximum block weight.

### Impact Explanation
Because `destroy`'s weight is `O(items + item_metadatas + attributes)` and executed atomically in one block with no per-call ceiling, a large collection (built up over time through many `mint` calls, which are individually bounded and cheap) can reach a size where the single `destroy` transaction's required weight/proof size exceeds `BlockWeights::max_block`/`BlockLength`. At that point:
- The extrinsic can never be included in any block (it will always be rejected by the runtime as exceeding the maximum extrinsic/block weight), so the collection can never be destroyed.
- The collection owner's deposits (`total_deposit`, per-item deposits) become permanently locked in `T::Currency::reserve`, since `T::Currency::unreserve(&collection_details.owner, collection_details.total_deposit)` is only reached after the full drain completes successfully.

This matches the "permanent user-fund lock" and "public underpriced work that degrades block production" impact classes: the weight formula assumes the whole operation fits in one block, but nothing enforces that assumption, unlike the analogous `pallet-assets` destroy flow.

### Likelihood Explanation
No privileged actor is required — an ordinary collection owner (or an issuer with mint permissions) using `mint` (an unprivileged, unbounded-supply operation absent an explicitly configured `CollectionMaxSupply`) can grow a collection large enough that its own later `destroy` call becomes unexecutable. This does not require a malicious validator, collator, or governance actor — only normal use of already-exposed, permissionless pallet operations (`create`, `mint` up to whatever size is economically feasible, then `destroy`).

### Recommendation
Align `pallet-uniques::destroy` with `pallet-nfts`/`pallet-assets`: either (a) require the collection to be emptied of items before allowing `destroy` (as `pallet-nfts` does), or (b) introduce a bounded, incremental destroy flow with a `RemoveItemsLimit`-style cap analogous to `pallet-assets::destroy_accounts`/`destroy_approvals`, splitting collection-item cleanup across multiple calls/blocks rather than a single unbounded loop.

### Proof of Concept
1. Create a `pallet-uniques` collection and, over time via many separate `mint` transactions (each individually weight-bounded and accepted), grow the collection to `N` items where `N` is large enough that `T::WeightInfo::destroy(N, M, A)` exceeds the chain's configured `BlockWeights::max_block` (or the resulting PoV size exceeds `BlockLength`).
2. Call `destroy(collection, witness)` with a witness matching the actual counts (as required by `do_destroy_collection`'s equality checks in `substrate/frame/uniques/src/functions.rs` lines 137-142).
3. Observe that the transaction is rejected pre-dispatch by the runtime's weight/length validation (`frame_system::CheckWeight` or the max extrinsic weight check) because its declared/actual weight exceeds the block limit — the collection (and its `total_deposit`) can never be destroyed, in contrast to `pallet-assets`, where `destroy_accounts`/`destroy_approvals` can be called repeatedly with bounded `RemoveItemsLimit` batches regardless of total account count (`substrate/frame/assets/src/functions.rs` lines 821-857).

### Citations

**File:** substrate/frame/uniques/src/functions.rs (L131-143)
```rust
		Collection::<T, I>::try_mutate_exists(collection.clone(), |maybe_details| {
			let collection_details =
				maybe_details.take().ok_or(Error::<T, I>::UnknownCollection)?;
			if let Some(check_owner) = maybe_check_owner {
				ensure!(collection_details.owner == check_owner, Error::<T, I>::NoPermission);
			}
			ensure!(collection_details.items == witness.items, Error::<T, I>::BadWitness);
			ensure!(
				collection_details.item_metadatas == witness.item_metadatas,
				Error::<T, I>::BadWitness
			);
			ensure!(collection_details.attributes == witness.attributes, Error::<T, I>::BadWitness);

```

**File:** substrate/frame/uniques/src/functions.rs (L144-150)
```rust
			for (item, details) in Item::<T, I>::drain_prefix(&collection) {
				Account::<T, I>::remove((&details.owner, &collection, &item));
			}
			#[allow(deprecated)]
			ItemMetadataOf::<T, I>::remove_prefix(&collection, None);
			#[allow(deprecated)]
			ItemPriceOf::<T, I>::remove_prefix(&collection, None);
```

**File:** substrate/frame/uniques/src/lib.rs (L536-563)
```rust
		/// Weight: `O(n + m)` where:
		/// - `n = witness.items`
		/// - `m = witness.item_metadatas`
		/// - `a = witness.attributes`
		#[pallet::call_index(2)]
		#[pallet::weight(T::WeightInfo::destroy(
			witness.items,
 			witness.item_metadatas,
			witness.attributes,
 		))]
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

			Ok(Some(T::WeightInfo::destroy(
				details.items,
				details.item_metadatas,
				details.attributes,
			))
			.into())
		}
```

**File:** substrate/frame/assets/src/lib.rs (L956-978)
```rust
		/// Destroy all accounts associated with a given asset.
		///
		/// `destroy_accounts` should only be called after `start_destroy` has been called, and the
		/// asset is in a `Destroying` state.
		///
		/// Due to weight restrictions, this function may need to be called multiple times to fully
		/// destroy all accounts. It will destroy `RemoveItemsLimit` accounts at a time.
		///
		/// - `id`: The identifier of the asset to be destroyed. This must identify an existing
		///   asset.
		///
		/// Each call emits the `Event::DestroyedAccounts` event.
		#[pallet::call_index(3)]
		#[pallet::weight(T::WeightInfo::destroy_accounts(T::RemoveItemsLimit::get()))]
		pub fn destroy_accounts(
			origin: OriginFor<T>,
			id: T::AssetIdParameter,
		) -> DispatchResultWithPostInfo {
			ensure_signed(origin)?;
			let id: T::AssetId = id.into();
			let removed_accounts = Self::do_destroy_accounts(id, T::RemoveItemsLimit::get())?;
			Ok(Some(T::WeightInfo::destroy_accounts(removed_accounts)).into())
		}
```

**File:** substrate/frame/assets/src/functions.rs (L821-857)
```rust
	/// Destroy accounts associated with a given asset up to the max (T::RemoveItemsLimit).
	///
	/// Each call emits the `Event::DestroyedAccounts` event.
	/// Returns the number of destroyed accounts.
	pub(super) fn do_destroy_accounts(
		id: T::AssetId,
		max_items: u32,
	) -> Result<u32, DispatchError> {
		let mut dead_accounts: Vec<T::AccountId> = vec![];
		let mut remaining_accounts = 0;
		Asset::<T, I>::try_mutate_exists(&id, |maybe_details| -> Result<(), DispatchError> {
			let mut details = maybe_details.as_mut().ok_or(Error::<T, I>::Unknown)?;
			// Should only destroy accounts while the asset is in a destroying state
			ensure!(details.status == AssetStatus::Destroying, Error::<T, I>::IncorrectStatus);

			for (i, (who, mut v)) in Account::<T, I>::iter_prefix(&id).enumerate() {
				if Self::ensure_account_can_die(id.clone(), &who).is_err() {
					continue;
				}
				// unreserve the existence deposit if any
				if let Some((depositor, deposit)) = v.reason.take_deposit_from() {
					T::Currency::unreserve(&depositor, deposit);
				} else if let Some(deposit) = v.reason.take_deposit() {
					T::Currency::unreserve(&who, deposit);
				}
				if let Remove = Self::dead_account(&who, &mut details, &v.reason, false) {
					Account::<T, I>::remove(&id, &who);
					dead_accounts.push(who);
				} else {
					// deposit may have been released, need to update `Account`
					Account::<T, I>::insert(&id, &who, v);
					defensive!("destroy did not result in dead account?!");
				}
				if i + 1 >= (max_items as usize) {
					break;
				}
			}
```
