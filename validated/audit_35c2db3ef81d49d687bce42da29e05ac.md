Audit Report

## Title
`pallet-nfts` item attribute-approvals survive ownership transfer, letting a stale delegate mutate a sold NFT's attributes - (File: `substrate/frame/nfts/src/features/transfer.rs`)

## Summary
`Pallet::do_transfer` resets transfer approvals, `ItemPriceOf`, and `PendingSwapOf` on ownership change, but never clears `ItemAttributesApprovalsOf`, the storage map that authorizes an `Account(delegate)` namespace to write item attributes. Because `is_valid_namespace` for `AttributeNamespace::Account` only checks `account_id == origin && approvals.contains(&origin)` — with no ownership check — a delegate approved by the previous owner keeps write access to attributes on an item after it is sold via `transfer`, `buy_item`, or `claim_swap`, letting an unrelated third party silently mutate state on an NFT now owned by someone else.

## Finding Description
`do_approve_item_attributes`/`approve_item_attributes` insert an entry into `ItemAttributesApprovalsOf::<T, I>` keyed by `(collection, item)` granting a `delegate` write access to the `AttributeNamespace::Account(delegate)` namespace. `do_transfer` [1](#0-0)  clears `details.approvals` (transfer approvals) and removes `ItemPriceOf`/`PendingSwapOf`, but does not touch `ItemAttributesApprovalsOf`. By contrast, `do_burn` explicitly removes it: [2](#0-1) .

The permission check exploited is `is_valid_namespace`, which for the `Account` namespace only validates that the caller is the delegate and is present in `ItemAttributesApprovalsOf`, with no reference to the item's current owner: [3](#0-2) . This is reached from the public `set_attribute` extrinsic via `do_set_attribute`: [4](#0-3) , and `do_set_attribute` gates on `is_valid_namespace` alone: [5](#0-4) .

`do_claim_swap` calls `do_transfer` twice (once per leg of the swap) and inherits the same gap, so attribute delegations survive atomic swaps too: [6](#0-5) .

The only way to clear a stale delegation is `do_cancel_item_attributes_approval`, which requires `check_origin == details.owner` [7](#0-6)  — but a new owner who never knew a delegation existed has no reason or on-chain signal to call it.

## Impact Explanation
This is an origin-escalation / unauthorized-execution issue: an account that was never granted permission by the current owner (`B`) can still write into `Attribute::<T, I>` entries under `AttributeNamespace::Account(delegate)` for an item `B` now owns, because the stale `ItemAttributesApprovalsOf` entry from the previous owner (`A`) is never invalidated. Item attributes are used by downstream consumers for metadata, rarity/tier state, and marketplace hints, so a former delegate can silently alter what a buyer effectively holds after a `transfer`, `buy_item` (price + `do_transfer`), or `claim_swap`. There is no automatic expiry for attribute approvals (unlike transfer approvals' optional deadline), so exposure persists indefinitely until a new, unaware owner manually cancels an approval they don't know exists.

## Likelihood Explanation
High under normal usage of the documented `approve_item_attributes` feature: any collection using attribute delegation followed by an ordinary ownership change (`transfer`, `buy_item`, or swap claim) will retain the stale delegate. No privileged actor, validator, or compromised infrastructure is required — only two unprivileged accounts (a prior delegate and an item that changes hands), both interacting exclusively through public extrinsics (`approve_item_attributes`, `transfer`/`buy_item`/`claim_swap`, `set_attribute`).

## Recommendation
Clear `ItemAttributesApprovalsOf::<T, I>::remove(&collection, &item)` (and unreserve any associated attribute deposits, mirroring `do_cancel_item_attributes_approval`'s cleanup) inside `do_transfer`, so that attribute-write delegation does not outlive the ownership grant it was created under. Ensure both `do_transfer` invocations inside `do_claim_swap` benefit from this fix since it swaps ownership on both legs.

## Proof of Concept
1. Owner `A` mints item `(collection, item)` and calls `approve_item_attributes(collection, item, delegate = D)`, inserting `D` into `ItemAttributesApprovalsOf::(collection, item)`.
2. `A` sells the item to `B` via `set_price` + `buy_item` (or `create_swap`/`claim_swap`); `do_transfer` runs, clearing `approvals`, `ItemPriceOf`, `PendingSwapOf`, but leaving `ItemAttributesApprovalsOf::(collection, item, D)` intact, per [1](#0-0) .
3. `D` (never approved by `B`) calls `set_attribute(collection, Some(item), AttributeNamespace::Account(D), key, value)`. `is_valid_namespace` passes because `D` is still in `ItemAttributesApprovalsOf`, per [3](#0-2) , and the write succeeds into `B`'s item without `B`'s consent.
4. This mirrors the existing unit test `set_external_account_attributes_should_work` [8](#0-7) , which can be extended with a `do_transfer`/`buy_item` step between approval and `set_attribute` to demonstrate the stale-delegate write succeeding post-sale.

### Citations

**File:** substrate/frame/nfts/src/features/transfer.rs (L94-103)
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

**File:** substrate/frame/nfts/src/features/create_delete_item.rs (L260-264)
```rust
		Item::<T, I>::remove(&collection, &item);
		Account::<T, I>::remove((&owner, &collection, &item));
		ItemPriceOf::<T, I>::remove(&collection, &item);
		PendingSwapOf::<T, I>::remove(&collection, &item);
		ItemAttributesApprovalsOf::<T, I>::remove(&collection, &item);
```

**File:** substrate/frame/nfts/src/features/attributes.rs (L64-67)
```rust
		ensure!(
			Self::is_valid_namespace(&origin, &namespace, &collection, &maybe_item)?,
			Error::<T, I>::NoPermission
		);
```

**File:** substrate/frame/nfts/src/features/attributes.rs (L412-425)
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

**File:** substrate/frame/nfts/src/lib.rs (L1429-1445)
```rust
		pub fn set_attribute(
			origin: OriginFor<T>,
			collection: T::CollectionId,
			maybe_item: Option<T::ItemId>,
			namespace: AttributeNamespace<T::AccountId>,
			key: BoundedVec<u8, T::KeyLimit>,
			value: BoundedVec<u8, T::ValueLimit>,
		) -> DispatchResult {
			let origin = ensure_signed(origin)?;
			let depositor = match namespace {
				AttributeNamespace::CollectionOwner => {
					Self::collection_owner(collection).ok_or(Error::<T, I>::UnknownCollection)?
				},
				_ => origin.clone(),
			};
			Self::do_set_attribute(origin, collection, maybe_item, namespace, key, value, depositor)
		}
```

**File:** substrate/frame/nfts/src/features/atomic_swap.rs (L211-219)
```rust
		Self::do_transfer(send_collection_id, send_item_id, receive_item.owner.clone(), |_, _| {
			Ok(())
		})?;
		Self::do_transfer(
			receive_collection_id,
			receive_item_id,
			send_item.owner.clone(),
			|_, _| Ok(()),
		)?;
```

**File:** substrate/frame/nfts/src/tests.rs (L1260-1343)
```rust
#[test]
fn set_external_account_attributes_should_work() {
	new_test_ext().execute_with(|| {
		Balances::make_free_balance_be(&account(1), 100);
		Balances::make_free_balance_be(&account(2), 100);

		assert_ok!(Nfts::force_create(
			RuntimeOrigin::root(),
			account(1),
			collection_config_with_all_settings_enabled()
		));
		assert_ok!(Nfts::force_mint(
			RuntimeOrigin::signed(account(1)),
			0,
			0,
			account(1),
			default_item_config()
		));
		assert_ok!(Nfts::approve_item_attributes(
			RuntimeOrigin::signed(account(1)),
			0,
			0,
			account(2)
		));

		assert_noop!(
			Nfts::set_attribute(
				RuntimeOrigin::signed(account(2)),
				0,
				Some(0),
				AttributeNamespace::Account(account(1)),
				bvec![0],
				bvec![0],
			),
			Error::<Test>::NoPermission,
		);
		assert_ok!(Nfts::set_attribute(
			RuntimeOrigin::signed(account(2)),
			0,
			Some(0),
			AttributeNamespace::Account(account(2)),
			bvec![0],
			bvec![0],
		));
		assert_ok!(Nfts::set_attribute(
			RuntimeOrigin::signed(account(2)),
			0,
			Some(0),
			AttributeNamespace::Account(account(2)),
			bvec![1],
			bvec![0],
		));
		assert_eq!(
			attributes(0),
			vec![
				(Some(0), AttributeNamespace::Account(account(2)), bvec![0], bvec![0]),
				(Some(0), AttributeNamespace::Account(account(2)), bvec![1], bvec![0]),
			]
		);
		assert_eq!(Balances::reserved_balance(account(2)), 6);

		// remove permission to set attributes
		assert_ok!(Nfts::cancel_item_attributes_approval(
			RuntimeOrigin::signed(account(1)),
			0,
			0,
			account(2),
			CancelAttributesApprovalWitness { account_attributes: 2 },
		));
		assert_eq!(attributes(0), vec![]);
		assert_eq!(Balances::reserved_balance(account(2)), 0);
		assert_noop!(
			Nfts::set_attribute(
				RuntimeOrigin::signed(account(2)),
				0,
				Some(0),
				AttributeNamespace::Account(account(2)),
				bvec![0],
				bvec![0],
			),
			Error::<Test>::NoPermission,
		);
	});
}
```
