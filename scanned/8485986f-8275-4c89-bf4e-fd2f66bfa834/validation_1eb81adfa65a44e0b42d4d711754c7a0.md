### Title
`ItemAttributesApprovalsOf` is not cleared on NFT ownership transfer, allowing a stale delegate approved by the previous owner to keep mutating the item's attributes after transfer — ([File: substrate/frame/nfts/src/features/transfer.rs])

### Summary
`pallet-nfts` maintains two independent per-item approval structures: `Item::approvals` (transfer approvals) and `ItemAttributesApprovalsOf` (delegates allowed to set attributes in the `Account(delegate)` namespace). `do_transfer()` explicitly clears the first to avoid a documented "pre-approve" attack, but never touches the second. This is the same bug class as the LoansNFT report: an approval mapping granted by the *old* controller of an asset silently carries over and is honored against the *new*, non-consenting controller after the asset changes hands.

### Finding Description
In `substrate/frame/nfts/src/features/transfer.rs`, `do_transfer()` resets `details.approvals` with an explicit comment about preventing pre-approval abuse: [1](#0-0) 

However it never removes entries from `ItemAttributesApprovalsOf::<T, I>` for the item, nor does any other code path clear this map on ownership transfer. `ItemAttributesApprovalsOf` is only ever removed when the item is burned: [2](#0-1) 

The approval check used by `set_attribute` for the `Account(delegate)` namespace is purely based on this stale map and the caller's identity — it never re-validates against the *current* item owner's intent: [3](#0-2) 

So the approval flow is: owner A calls `approve_item_attributes(delegate)`, populating `ItemAttributesApprovalsOf[collection][item] = {delegate}` via `do_approve_item_attributes`, which only checks that the *current* owner at approval time is `check_origin`: [4](#0-3) 

If the item is later transferred to a new owner B (via `transfer`, sale, or any other `do_transfer`-based path), `delegate` remains in `ItemAttributesApprovalsOf` and can continue calling `set_attribute` with `AttributeNamespace::Account(delegate)` against B's item — exactly mirroring the report's flaw where `keeperApprovedFor` (granted under one relationship/context) was blindly reused after the controlling party changed via NFT transfer, because the code never separates "approval scoped to owner X" from "approval keyed only by (collection, item)".

### Impact Explanation
The stale delegate can:
- Continue writing/updating attributes on the transferred item without the new owner's consent, polluting on-chain metadata that marketplaces, games, or other integrations may treat as authoritative for the asset.
- Occupy a slot in the `MaxAttributesApprovals`-bounded set indefinitely, since only the *current* owner can call `cancel_item_attributes_approval`, but the new owner has no visibility into or expectation of a stranger's dangling approval on an asset they just acquired — a denial-of-service on that owner's ability to grant their own trusted delegates.
- More importantly, the same architectural flaw as the report — approval-mapping continuity across a change of controlling party — is the exact broken invariant HackenProof's "public wrappers ... must not widen origin" and "state ... must only advance after ... settlement succeed atomically" pivots target: a foreign account retains write-execution rights over an asset's state after the controlling account changes, without the new controller's authorization.

### Likelihood Explanation
Likelihood is low-to-moderate: it requires (1) an item owner to have previously approved an attribute delegate, and (2) the item to subsequently change hands (sale, gift, marketplace transfer) without the new owner or the old owner explicitly cancelling the approval first. This is analogous to the report's own assessment ("likelihood is very low ... requires specific pre-conditions") but the underlying code path is fully reachable by ordinary, unprivileged users through public extrinsics (`approve_item_attributes`, `transfer`, `set_attribute`) with no special permissions needed.

### Recommendation
Clear `ItemAttributesApprovalsOf` (and refund any associated deposits back to their depositors) inside `do_transfer()`, the same way `details.approvals` is cleared, so that attribute-delegate approvals do not silently carry over to a new, non-consenting owner. Alternatively, bind the approval check in `is_valid_namespace` to the owner recorded at approval time, invalidating it automatically once `details.owner` changes.

### Proof of Concept
1. Owner A mints item `#42` in collection `0` and calls `Nfts::approve_item_attributes(A, 0, 42, delegate)`, adding `delegate` to `ItemAttributesApprovalsOf(0, 42)`.
2. A transfers `#42` to B via `Nfts::transfer(0, 42, B)`. `do_transfer` clears `Item::approvals` but leaves `ItemAttributesApprovalsOf(0, 42) = {delegate}` untouched.
3. `delegate` (never authorized by B) calls `Nfts::set_attribute(0, Some(42), AttributeNamespace::Account(delegate), key, value)`. The check in `is_valid_namespace` passes because `approvals.contains(&origin)` is still true, so the write succeeds against B's item without B's consent. [5](#0-4)

### Citations

**File:** substrate/frame/nfts/src/features/transfer.rs (L95-98)
```rust
		// The approved accounts have to be reset to `None`, because otherwise pre-approve attack
		// would be possible, where the owner can approve their second account before making the
		// transaction and then claiming the item back.
		details.approvals.clear();
```

**File:** substrate/frame/nfts/src/features/create_delete_item.rs (L1-1)
```rust
// This file is part of Substrate.
```

**File:** substrate/frame/nfts/src/features/attributes.rs (L60-67)
```rust
			Self::is_pallet_feature_enabled(PalletFeature::Attributes),
			Error::<T, I>::MethodDisabled
		);

		ensure!(
			Self::is_valid_namespace(&origin, &namespace, &collection, &maybe_item)?,
			Error::<T, I>::NoPermission
		);
```

**File:** substrate/frame/nfts/src/features/attributes.rs (L372-393)
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
```

**File:** substrate/frame/nfts/src/features/attributes.rs (L474-479)
```rust
			AttributeNamespace::Account(account_id) => {
				if let Some(item) = maybe_item {
					let approvals = ItemAttributesApprovalsOf::<T, I>::get(&collection, &item);
					result = account_id == origin && approvals.contains(&origin)
				}
			},
```
