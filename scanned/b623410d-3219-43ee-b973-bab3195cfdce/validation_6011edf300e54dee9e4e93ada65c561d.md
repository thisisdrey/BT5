### Title
`Nfts::transfer` Fails to Clear `ItemAttributesApprovalsOf`, Letting a Former Owner's Delegate Retain Write Access After Ownership Transfer - (File: `substrate/frame/nfts/src/features/transfer.rs`)

### Summary
`Pallet::do_transfer` in the NFTs pallet updates ownership (`Account`, `Item.owner`) and correctly purges transfer-approvals (`details.approvals`), `ItemPriceOf`, and `PendingSwapOf` on every item transfer, but it never clears `ItemAttributesApprovalsOf`. This is the same class of bug as the external report: an owner-scoped permission/mapping is not fully synced when ownership of the asset changes, letting a party the previous owner authorized keep exercising owner-granted rights (writing attributes) on the item after it belongs to a new, unconsenting owner.

### Finding Description
`do_transfer` explicitly resets several owner-scoped mappings on transfer: [1](#0-0) 

Note it clears `details.approvals` (transfer delegate), `ItemPriceOf`, and `PendingSwapOf`, but there is no call to clear `ItemAttributesApprovalsOf` for the item.

Meanwhile, `ItemAttributesApprovalsOf` is populated by the *current owner at approval time* via `do_approve_item_attributes`, which only checks `check_origin == details.owner` at the moment of approval: [2](#0-1) 

Once approved, the delegate's write authority is validated purely from the standalone `ItemAttributesApprovalsOf` map — not from the item's current owner — in `is_valid_namespace`: [3](#0-2) 

Since `do_transfer` never touches `ItemAttributesApprovalsOf`, a delegate approved by owner A remains a valid attribute-setter for the `Account(delegate)` namespace of the item even after the item is sold/transferred to owner B (via `transfer`, `buy_item`, or `do_claim_swap`, all of which funnel through `do_transfer`). Only an explicit `cancel_item_attributes_approval` call by the *current* owner removes it — but B, the new owner, has no way of knowing A's delegate was approved, since this state is invisible from the item transfer flow and event stream (`Transferred` does not report the stale approvals).

This mirrors exactly the root cause in the external report: `updateOwner()`/`do_transfer()` moves the primary ownership pointer but fails to sync a secondary owner-scoped permission mapping (`refereeFeeAmounts` vs `ItemAttributesApprovalsOf`), so a party tied to the *old* owner retains rights against an asset now controlled by a *new*, non-consenting party.

### Impact Explanation
This is a public-entrypoint origin-widening bug: `transfer`/`buy_item`/`do_claim_swap` are permissionless dispatchables usable by any signed account, and they silently leave a third-party (the previous owner's delegate) with continued, owner-level write authority (`set_attribute`) over the item's `Account(delegate)` namespace after the asset has legitimately changed hands. This lets the ex-owner (via a delegate they control) continue to mutate on-chain state associated with an NFT they no longer own — e.g. injecting misleading attributes, reserving/using storage against the item, or interfering with downstream logic that trusts item attributes (marketplaces, provenance/certification schemes) — without the new owner's consent and with no visible signal that this stale permission exists. It is a genuine violation of the "public wrappers must not widen origin" invariant: `transfer` is supposed to hand exclusive control of the item to the new owner, but a residual owner-granted capability survives the transfer.

### Likelihood Explanation
High feasibility: any account can mint/acquire an NFT, approve a delegate via `approve_item_attributes`, then sell/transfer the item through the standard `transfer`, `buy_item`, or swap-claim path. No governance, validator, relayer, or privileged actor is required — the entire sequence is reachable by an ordinary user with the `Attributes` and `Trading`/`Transfer` pallet features enabled (both enabled on the shipped asset-hub configurations).

### Recommendation
In `do_transfer` (and any other path that reassigns `Item.owner`), also drain/clear `ItemAttributesApprovalsOf` for the `(collection, item)` pair — mirroring the existing handling of `details.approvals`, `ItemPriceOf`, and `PendingSwapOf` — and unreserve/settle any deposits tied to those approvals, exactly as `do_cancel_item_attributes_approval` does. This ensures all owner-scoped mappings are synchronized atomically with the ownership change.

### Proof of Concept
1. Owner A mints item `(collection, item)` and calls `approve_item_attributes(collection, item, delegate)` — `ItemAttributesApprovalsOf[(collection, item)]` now contains `delegate`.
2. `delegate` sets an attribute in `AttributeNamespace::Account(delegate)` for the item (deposit reserved from `delegate`).
3. A sells/transfers the item to B via `Nfts::transfer` (or `buy_item`). `do_transfer` updates `Item.owner` to B, clears `details.approvals`, `ItemPriceOf`, `PendingSwapOf` — but `ItemAttributesApprovalsOf[(collection, item)]` still contains `delegate`.
4. `delegate` calls `set_attribute(collection, item, AttributeNamespace::Account(delegate), key, value)`. `is_valid_namespace` succeeds because `approvals.contains(&origin)` is still true, even though B is now the sole owner and never approved `delegate`.
5. B has no straightforward way to discover or revoke this, since `cancel_item_attributes_approval` requires knowing the exact `delegate` address that A had approved.

### Citations

**File:** substrate/frame/nfts/src/features/transfer.rs (L89-103)
```rust
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

**File:** substrate/frame/nfts/src/features/attributes.rs (L474-479)
```rust
			AttributeNamespace::Account(account_id) => {
				if let Some(item) = maybe_item {
					let approvals = ItemAttributesApprovalsOf::<T, I>::get(&collection, &item);
					result = account_id == origin && approvals.contains(&origin)
				}
			},
```
