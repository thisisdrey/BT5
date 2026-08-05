### Title
`pallet-nfts` item attribute-approvals survive ownership transfer, letting a stale delegate mutate a sold NFT's attributes - (File: `substrate/frame/nfts/src/features/transfer.rs`)

### Summary
The external report's broken invariant is: **state controlled by someone other than the current, informed owner can change the NFT after it has effectively changed hands (e.g., via a marketplace transaction), silently altering what the new holder actually possesses.** In MEME404 this happens through tier-changing burn/mint side effects. The direct on-chain analog in this repository is `pallet-nfts`'s item-attribute delegation: `do_transfer` clears transfer approvals and any price/swap listing, but does **not** clear `ItemAttributesApprovalsOf`, so a delegate approved by the *previous* owner keeps write access to the item's attributes after the item is sold/transferred to a new owner.

### Finding Description
`do_approve_transfer` / `approve_item_attributes` let an item owner name a delegate account allowed to write attributes into a specific namespace on the item (`substrate/frame/nfts/src/features/approvals.rs`). When an item is later transferred with `Pallet::do_transfer`, the pallet explicitly resets the *transfer* approvals (to prevent the classic "pre-approve, sell, then reclaim" attack) and clears the price and pending-swap listings: [1](#0-0) 

However, it never touches `ItemAttributesApprovalsOf`, the storage map backing attribute-namespace delegation. Compare this to `do_burn`, which explicitly removes it: [2](#0-1) 

Because `do_transfer` (and `do_claim_swap`, which calls `do_transfer` twice, in `substrate/frame/nfts/src/features/atomic_swap.rs`) omits the equivalent cleanup, any account previously granted attribute-write access by a *former* owner remains authorized to call `set_attribute`/`force_set_attribute` under the `AttributeNamespace::Account(delegate)` namespace against the item — even after the item is sold to a completely unrelated buyer who never approved that delegate and has no way to know the approval exists (nothing in `transfer`/`buy_item`/`claim_swap` surfaces or resets it).

### Impact Explanation
This is an unauthorized-execution / origin-escalation path: a third party who received an attribute delegation from a prior owner can keep mutating the state (attributes) of an NFT now owned by someone else, without the new owner's consent, indefinitely (attribute approvals have no automatic expiry the way transfer approvals do). Attributes are used by consuming applications for metadata, game state, rarity/tier flags, or marketplace listing hints — exactly the class of "tier"-like state the MEME404 report is worried about. A buyer who purchases an NFT through `buy_item`/`claim_swap` (public dispatchables) can have the item's on-chain attributes altered right after the sale by an account they never interacted with, defeating the invariant that ownership transfer should also transfer exclusive control over item state.

### Likelihood Explanation
High under normal usage: any collection that uses `approve_item_attributes` (documented as a first-class permissionless feature in `substrate/frame/nfts/README.md`) and where items later change hands via `transfer`, `buy_item`, or atomic swaps will retain stale delegations. No malicious relayer, validator, or governance actor is required — only an ordinary buyer and a previously-approved delegate account, both unprivileged.

### Recommendation
Clear `ItemAttributesApprovalsOf::<T, I>::remove(&collection, &item)` inside `do_transfer` (and ensure the same happens for both legs of `do_claim_swap`), mirroring what `do_burn` already does, so attribute-write delegation does not outlive the ownership it was granted under.

### Proof of Concept
1. Owner `A` mints item `(collection, item)` and calls `approve_item_attributes(item, delegate = D, namespace = Account(D))`.
2. `A` lists the item and sells it to `B` via `set_price` + `buy_item`, or via `create_swap`/`claim_swap`.
3. `do_transfer` runs, clearing `approvals`, `ItemPriceOf`, and `PendingSwapOf`, but leaves `ItemAttributesApprovalsOf::(collection, item, D)` intact (see cited transfer.rs lines above).
4. `D` (never approved by `B`) calls `set_attribute(collection, item, AttributeNamespace::Account(D), key, value)`; the pallet's ownership/permission check for this namespace passes because the stale approval entry is still present, letting `D` write into `B`'s newly acquired item.

### Citations

**File:** substrate/frame/nfts/src/features/transfer.rs (L94-104)
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
