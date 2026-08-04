## Finding

### Title
`do_transfer` in pallet-nfts allows an approved delegate to seize a for-sale item without going through `buy_item`'s payment settlement - (File: `substrate/frame/nfts/src/features/transfer.rs`)

### Summary
The `pallet-nfts` trading feature enforces payment only inside `do_buy_item`, which calls `T::Currency::transfer` before moving ownership [1](#0-0) . However, the generic ownership-transfer primitive `do_transfer`, used by the plain `transfer` dispatchable, the `Transfer` trait implementation, and any approved-delegate path, performs the ownership change and unconditionally clears the item's sale/price state without ever checking or collecting payment [2](#0-1) . This is structurally identical to the reported CosmWasm bug: `transfer_nft` settles payment while `send_nft` moves the token via the same underlying `_transfer_nft` primitive without any settlement check.

### Finding Description
`do_transfer` retrieves item details, applies a caller-supplied `with_details` permission closure, then moves ownership and clears `ItemPriceOf`/`PendingSwapOf` [2](#0-1) . It never inspects `ItemPriceOf` to require payment - that logic lives exclusively in `do_buy_item` [3](#0-2) .

Any code path that reaches `do_transfer` with permission to move the item - i.e. the owner, or an account holding a transfer approval - bypasses payment entirely. The `Transfer` trait exposed to other pallets/runtime code calls `do_transfer` directly with no price check whatsoever [4](#0-3) . Likewise, the permissionless `approve_transfer` dispatchable lets the owner grant a delegate approval independent of the sale/price system; once approved, that delegate can invoke the plain `transfer` call (which routes through `do_transfer`) instead of `buy_item`, receiving the item while `ItemPriceOf` is simply wiped rather than enforced [5](#0-4) .

This mirrors the H-08 root cause exactly: two independent transfer entrypoints exist over the same underlying ownership-mutation primitive, only one of which (`buy_item`/`transfer_nft`) contains settlement logic, while the other (`transfer`/`send_nft`) shares the mutation primitive but omits the payment check.

### Impact Explanation
Any composed marketplace/escrow flow built on top of `pallet-nfts` that grants a prospective buyer a transfer approval before settlement (e.g. via `approve_transfer`, or any higher-level pallet using the `Transfer::transfer` trait implementation) is exposed to a buyer who calls the raw `transfer` path to take the item and skip payment, matching the theft-of-token-without-payment impact described in the source report - unbacked ownership transfer of value without settlement.

### Likelihood Explanation
The likelihood depends entirely on whether a downstream runtime or marketplace pallet grants transfer approval to a bidder prior to full payment settlement (the "auto_approve" pattern from the original report). `pallet-nfts` itself does not do this in `do_buy_item`, so the base pallet's `buy_item`/`set_price` flow is safe in isolation; the risk is that `approve_transfer` and the `Transfer` trait are exposed as general-purpose, permissionless building blocks that any composing pallet/runtime can wire into a bid-then-approve marketplace flow without realizing `do_transfer` performs no price enforcement.

### Recommendation
Have `do_transfer` check `ItemPriceOf` and either reject the transfer or require that it only be cleared via `do_buy_item` (or an equivalent settlement-only removal), so that any transfer path - approvals, `Transfer` trait callers, or plain `transfer` - cannot silently bypass a listed price. Alternatively, disallow `approve_transfer` while an item has an active `ItemPriceOf` entry, forcing settlement through `buy_item`.

### Proof of Concept
1. Owner mints item, lists it via `set_price(collection, item, Some(price), None)`.
2. Owner (or any code representing an "auto-approve" marketplace layer built on the pallet) calls `approve_transfer(collection, item, buyer, None)` to grant the prospective buyer a transfer delegate approval, as would happen in a bid-then-approve UX.
3. Buyer calls `transfer(collection, item, buyer)` directly instead of `buy_item`. This flows through `do_transfer`, whose permission closure accepts the approved delegate, moves ownership, and clears `ItemPriceOf` without any `T::Currency::transfer` call [6](#0-5) .
4. Buyer now owns the item; seller received no payment, and the sale listing is silently cleared as if settled.

### Citations

**File:** substrate/frame/nfts/src/features/buy_sell.rs (L139-160)
```rust
		let details = Item::<T, I>::get(&collection, &item).ok_or(Error::<T, I>::UnknownItem)?;
		ensure!(details.owner != buyer, Error::<T, I>::NoPermission);

		let price_info =
			ItemPriceOf::<T, I>::get(&collection, &item).ok_or(Error::<T, I>::NotForSale)?;

		ensure!(bid_price >= price_info.0, Error::<T, I>::BidTooLow);

		if let Some(only_buyer) = price_info.1 {
			ensure!(only_buyer == buyer, Error::<T, I>::NoPermission);
		}

		T::Currency::transfer(
			&buyer,
			&details.owner,
			price_info.0,
			ExistenceRequirement::KeepAlive,
		)?;

		let old_owner = details.owner.clone();

		Self::do_transfer(collection, item, buyer.clone(), |_, _| Ok(()))?;
```

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

**File:** substrate/frame/nfts/src/impl_nonfungibles.rs (L412-419)
```rust
impl<T: Config<I>, I: 'static> Transfer<T::AccountId> for Pallet<T, I> {
	fn transfer(
		collection: &Self::CollectionId,
		item: &Self::ItemId,
		destination: &T::AccountId,
	) -> DispatchResult {
		Self::do_transfer(*collection, *item, destination.clone(), |_, _| Ok(()))
	}
```
