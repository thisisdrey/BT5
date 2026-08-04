## Analysis

I confirmed that `ItemAttributesApprovalsOf` (a per-item delegate-approval set for the `Account(delegate)` attribute namespace in `pallet-nfts`) is **only cleared explicitly via `cancel_item_attributes_approval`**, and its only other reference outside `attributes.rs` is a single touch point in `create_delete_item.rs` (burn path) and `lib.rs`/`tests.rs`. Crucially, `Pallet::do_transfer` in `substrate/frame/nfts/src/features/transfer.rs` clears `details.approvals` (the transfer-delegate set) and removes `ItemPriceOf`/`PendingSwapOf`, but it does **not** touch `ItemAttributesApprovalsOf`. This exactly mirrors the Open Dollar bug class: a permission set keyed by `(collection, item)` — not by owner — silently survives an ownership change, so the old owner's previously approved delegate keeps write-access to the item after transfer, unknown to the new owner. [1](#0-0) [2](#0-1) [3](#0-2) 

### Title
Stale `ItemAttributesApprovalsOf` delegate permissions survive NFT item ownership transfer - (File: `substrate/frame/nfts/src/features/transfer.rs`)

### Summary
`pallet-nfts` lets an item owner approve a third-party `delegate` to write attributes in the item's `Account(delegate)` namespace via `approve_item_attributes` / `do_approve_item_attributes`, which inserts into `ItemAttributesApprovalsOf::<T,I>(collection, item)`. When the item is later transferred to a new owner via `transfer`/`do_transfer`, only `details.approvals` (the transfer-delegate map), `ItemPriceOf`, and `PendingSwapOf` are cleared — `ItemAttributesApprovalsOf` is left untouched.

### Finding Description
`do_transfer` explicitly documents and implements the "pre-approve attack" mitigation for transfer approvals: `details.approvals.clear()` [4](#0-3) . But `ItemAttributesApprovalsOf` — a separate storage map keyed only by `(collection, item)`, not by owner — is never cleared, reset, or re-keyed anywhere in the transfer path. `is_valid_namespace` authorizes an `Account(account_id)` write purely by checking `ItemAttributesApprovalsOf::<T, I>::get(&collection, &item)` for membership, with no owner-consistency check [5](#0-4) . This is structurally identical to the Open Dollar `handlerCan` bug: a permission mapping tied to a persistent identifier (the item, analogous to the `safeHandler`) rather than to the current owner, so old grants outlive an ownership change.

### Impact Explanation
After transfer, the previous owner's approved delegate can continue calling `set_attribute`/`do_set_attribute` with `AttributeNamespace::Account(delegate)` on the item, writing attributes and forcing deposit reservations against the item, without the new owner's knowledge or consent [6](#0-5) . While this is scoped to the `Account()` attribute namespace (not fund custody or `ItemOwner`/`CollectionOwner` namespaces), it is an unauthorized-write / origin-persistence bug: the new owner never granted this access and has no visible indication (aside from manually querying `ItemAttributesApprovalsOf`) that a foreign account retains write capability over their newly acquired NFT. This matches the "runtime bugs that compromise intended behavior" and "unauthorized execution" pivot categories.

### Likelihood Explanation
The path is fully public and requires no privileged actor: any item owner can call `approve_item_attributes` for a colluding/self-controlled delegate, then transfer the item via the normal `transfer` extrinsic; the delegate mapping trivially survives. No governance, admin, relayer, validator, or malicious-peer assumption is needed — this is exploitable end-to-end by a single unprivileged account plus one delegate account it controls.

### Recommendation
Clear (or explicitly re-key to prevent stale grants) `ItemAttributesApprovalsOf::<T, I>(collection, item)` inside `do_transfer` in `substrate/frame/nfts/src/features/transfer.rs`, alongside the existing `details.approvals.clear()`, `ItemPriceOf::remove`, and `PendingSwapOf::remove` calls, unreserving any associated attribute deposits (mirroring the logic already present in `do_cancel_item_attributes_approval`).

### Proof of Concept
1. Owner A mints item `(collection=0, item=42)` and calls `approve_item_attributes(origin=A, 0, 42, delegate=X)`, which inserts `X` into `ItemAttributesApprovalsOf(0, 42)` [2](#0-1) .
2. `X` calls `set_attribute(origin=X, 0, Some(42), AttributeNamespace::Account(X), key, value)`, succeeding because `is_valid_namespace` finds `X` in the approvals set.
3. Owner A transfers item `42` to Owner B via `transfer(origin=A, 0, 42, B)`, which invokes `do_transfer`; `details.approvals` is cleared but `ItemAttributesApprovalsOf(0, 42)` still contains `X` [7](#0-6) .
4. `X` calls `set_attribute(origin=X, 0, Some(42), AttributeNamespace::Account(X), key, value2)` again — this succeeds even though Owner B never approved `X`, confirming the stale-permission bypass. Owner B has no way to know this without independently querying `ItemAttributesApprovalsOf`.

### Citations

**File:** substrate/frame/nfts/src/features/transfer.rs (L82-103)
```rust
		// Retrieve the item details.
		let mut details =
			Item::<T, I>::get(&collection, &item).ok_or(Error::<T, I>::UnknownItem)?;

		// Perform the transfer with custom details using the provided closure.
		with_details(&collection_details, &mut details)?;

		// Update account ownership information.
		Account::<T, I>::remove((&details.owner, &collection, &item));
		Account::<T, I>::insert((&dest, &collection, &item), ());
		let origin = details.owner;
		details.owner = dest;

		// The approved accounts have to be reset to `None`, because otherwise pre-approve attack
		// would be possible, where the owner can approve their second account before making the
		// transaction and then claiming the item back.
		details.approvals.clear();

		// Update item details.
		Item::<T, I>::insert(&collection, &item, &details);
		ItemPriceOf::<T, I>::remove(&collection, &item);
		PendingSwapOf::<T, I>::remove(&collection, &item);
```

**File:** substrate/frame/nfts/src/features/attributes.rs (L50-67)
```rust
	pub(crate) fn do_set_attribute(
		origin: T::AccountId,
		collection: T::CollectionId,
		maybe_item: Option<T::ItemId>,
		namespace: AttributeNamespace<T::AccountId>,
		key: BoundedVec<u8, T::KeyLimit>,
		value: BoundedVec<u8, T::ValueLimit>,
		depositor: T::AccountId,
	) -> DispatchResult {
		ensure!(
			Self::is_pallet_feature_enabled(PalletFeature::Attributes),
			Error::<T, I>::MethodDisabled
		);

		ensure!(
			Self::is_valid_namespace(&origin, &namespace, &collection, &maybe_item)?,
			Error::<T, I>::NoPermission
		);
```

**File:** substrate/frame/nfts/src/features/attributes.rs (L372-394)
```rust
	pub(crate) fn do_approve_item_attributes(
		check_origin: T::AccountId,
		collection: T::CollectionId,
		item: T::ItemId,
		delegate: T::AccountId,
	) -> DispatchResult {
		ensure!(
			Self::is_pallet_feature_enabled(PalletFeature::Attributes),
			Error::<T, I>::MethodDisabled
		);

		let details = Item::<T, I>::get(&collection, &item).ok_or(Error::<T, I>::UnknownItem)?;
		ensure!(check_origin == details.owner, Error::<T, I>::NoPermission);

		ItemAttributesApprovalsOf::<T, I>::try_mutate(collection, item, |approvals| {
			approvals
				.try_insert(delegate.clone())
				.map_err(|_| Error::<T, I>::ReachedApprovalLimit)?;

			Self::deposit_event(Event::ItemAttributesApprovalAdded { collection, item, delegate });
			Ok(())
		})
	}
```

**File:** substrate/frame/nfts/src/features/attributes.rs (L461-479)
```rust
	) -> Result<bool, DispatchError> {
		let mut result = false;
		match namespace {
			AttributeNamespace::CollectionOwner => {
				result = Self::has_role(&collection, &origin, CollectionRole::Admin)
			},
			AttributeNamespace::ItemOwner => {
				if let Some(item) = maybe_item {
					let item_details =
						Item::<T, I>::get(&collection, &item).ok_or(Error::<T, I>::UnknownItem)?;
					result = origin == &item_details.owner
				}
			},
			AttributeNamespace::Account(account_id) => {
				if let Some(item) = maybe_item {
					let approvals = ItemAttributesApprovalsOf::<T, I>::get(&collection, &item);
					result = account_id == origin && approvals.contains(&origin)
				}
			},
```
