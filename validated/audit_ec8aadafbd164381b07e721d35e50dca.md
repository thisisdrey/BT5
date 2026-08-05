This confirms the finding. `ItemAttributesApprovalsOf::<T, I>::remove` is only invoked in the burn path (`substrate/frame/nfts/src/features/create_delete_item.rs`), never in `do_transfer` (`substrate/frame/nfts/src/features/transfer.rs`). The transfer function clears `details.approvals`, removes `ItemPriceOf`, and removes `PendingSwapOf`, but never touches `ItemAttributesApprovalsOf`, while `is_valid_namespace` for `AttributeNamespace::Account(account_id)` grants write access purely based on membership in that stale per-item set, with no owner-consistency check.

Audit Report

## Title
Stale `ItemAttributesApprovalsOf` delegate permissions survive NFT item ownership transfer - (File: `substrate/frame/nfts/src/features/transfer.rs`)

## Summary
`pallet-nfts` allows an item owner to approve a delegate to write attributes in the item's `Account(delegate)` namespace via `do_approve_item_attributes`, which stores the delegate in `ItemAttributesApprovalsOf::<T,I>(collection, item)` [1](#0-0) . When the item is transferred via `do_transfer`, only `details.approvals`, `ItemPriceOf`, and `PendingSwapOf` are cleared/removed; `ItemAttributesApprovalsOf` is never touched [2](#0-1) .

## Finding Description
`do_transfer` explicitly documents and implements a "pre-approve attack" mitigation for transfer-approvals by calling `details.approvals.clear()`, and separately removes `ItemPriceOf` and `PendingSwapOf` [2](#0-1) . However, `ItemAttributesApprovalsOf`, a storage map keyed only by `(collection, item)` and not by owner, is never cleared or removed in the transfer path. Authorization for `AttributeNamespace::Account(account_id)` writes is checked purely via membership in `ItemAttributesApprovalsOf::<T, I>::get(&collection, &item)`, with no verification that the current item owner is consistent with who granted the approval [3](#0-2) . Confirming this is not an oversight elsewhere in the pallet, the burn path (`create_delete_item.rs`) does call `ItemAttributesApprovalsOf::<T, I>::remove`, and the explicit `do_cancel_item_attributes_approval` function performs the same clear-and-unreserve logic that is absent from `do_transfer` [4](#0-3) .

## Impact Explanation
After an ownership transfer, the previous owner's approved delegate retains the ability to call `set_attribute` with `AttributeNamespace::Account(delegate)` on the item, forcing deposit reservations and writing attributes without the new owner's knowledge or consent [5](#0-4) . This is an unauthorized-write/origin-persistence bug scoped to the `Account()` attribute namespace: a stale permission set tied to the item identity (rather than reset on ownership change) grants write capability to an account the new owner never approved.

## Likelihood Explanation
The exploit path is fully public and requires no privileged actor: any item owner can call `approve_item_attributes` for a delegate they control, then transfer the item via the normal `transfer` extrinsic. The stale approval persists automatically with no additional steps, making this trivially and repeatably exploitable by a single unprivileged account plus a colluding/self-controlled delegate account.

## Recommendation
Clear `ItemAttributesApprovalsOf::<T, I>(collection, item)` inside `do_transfer` in `substrate/frame/nfts/src/features/transfer.rs`, alongside the existing `details.approvals.clear()`, `ItemPriceOf::remove`, and `PendingSwapOf::remove` calls, unreserving any deposits tied to `Account()`-namespace attributes set by the removed delegates, mirroring the logic already present in `do_cancel_item_attributes_approval`.

## Proof of Concept
1. Owner A mints item `(collection=0, item=42)` and calls `approve_item_attributes(origin=A, 0, 42, delegate=X)`, inserting `X` into `ItemAttributesApprovalsOf(0, 42)`.
2. `X` calls `set_attribute(origin=X, 0, Some(42), AttributeNamespace::Account(X), key, value)`, succeeding because `is_valid_namespace` finds `X` in the approvals set.
3. Owner A transfers item `42` to Owner B via `transfer(origin=A, 0, 42, B)`, invoking `do_transfer`; `details.approvals` is cleared but `ItemAttributesApprovalsOf(0, 42)` still contains `X`.
4. `X` calls `set_attribute(origin=X, 0, Some(42), AttributeNamespace::Account(X), key, value2)` again — this succeeds even though Owner B never approved `X`, confirming the stale-permission bypass.

### Citations

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

**File:** substrate/frame/nfts/src/features/attributes.rs (L412-453)
```rust
	pub(crate) fn do_cancel_item_attributes_approval(
		check_origin: T::AccountId,
		collection: T::CollectionId,
		item: T::ItemId,
		delegate: T::AccountId,
		witness: CancelAttributesApprovalWitness,
	) -> DispatchResult {
		ensure!(
			Self::is_pallet_feature_enabled(PalletFeature::Attributes),
			Error::<T, I>::MethodDisabled
		);

		let details = Item::<T, I>::get(&collection, &item).ok_or(Error::<T, I>::UnknownItem)?;
		ensure!(check_origin == details.owner, Error::<T, I>::NoPermission);

		ItemAttributesApprovalsOf::<T, I>::try_mutate(collection, item, |approvals| {
			approvals.remove(&delegate);

			let mut attributes: u32 = 0;
			let mut deposited: DepositBalanceOf<T, I> = Zero::zero();
			for (_, (_, deposit)) in Attribute::<T, I>::drain_prefix((
				&collection,
				Some(item),
				AttributeNamespace::Account(delegate.clone()),
			)) {
				attributes.saturating_inc();
				deposited = deposited.saturating_add(deposit.amount);
			}
			ensure!(attributes <= witness.account_attributes, Error::<T, I>::BadWitness);

			if !deposited.is_zero() {
				T::Currency::unreserve(&delegate, deposited);
			}

			Self::deposit_event(Event::ItemAttributesApprovalRemoved {
				collection,
				item,
				delegate,
			});
			Ok(())
		})
	}
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

**File:** substrate/frame/nfts/src/features/transfer.rs (L95-103)
```rust
		// The approved accounts have to be reset to `None`, because otherwise pre-approve attack
		// would be possible, where the owner can approve their second account before making the
		// transaction and then claiming the item back.
		details.approvals.clear();

		// Update item details.
		Item::<T, I>::insert(&collection, &item, &details);
		ItemPriceOf::<T, I>::remove(&collection, &item);
		PendingSwapOf::<T, I>::remove(&collection, &item);
```
