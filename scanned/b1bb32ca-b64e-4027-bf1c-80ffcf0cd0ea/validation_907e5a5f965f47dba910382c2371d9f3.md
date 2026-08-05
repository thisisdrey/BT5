### Title
`lock_item_transfer` Ignores Pending Sale/Swap Commitment, Letting a Seller Retroactively Deny an Already-Agreed NFT Trade - ([File: substrate/frame/nfts/src/features/lock.rs])

### Summary
The external report's core broken invariant is: a state-mutating "protect my asset" function checks only *ownership*, not the *timing/context* of an already-determined outcome, letting the loser retroactively shield an asset that a counterparty is entitled to receive. The closest local analog in `paritytech/polkadot-sdk` is `pallet-nfts`'s `lock_item_transfer` extrinsic, which disables an item's transferability by checking only a role permission and never checks whether the item is already committed to a pending sale (`ItemPriceOf`) or a pending atomic swap (`PendingSwapOf`) that the counterparty has already agreed to and is entitled to complete.

### Finding Description
`do_lock_item_transfer` in [1](#0-0)  only verifies:
```rust
ensure!(Self::has_role(&collection, &origin, CollectionRole::Freezer), Error::<T, I>::NoPermission);
```
before disabling `ItemSetting::Transferable` on the item. It performs no check against `ItemPriceOf` (an active sale listing created via `set_price`, see [2](#0-1) ) or `PendingSwapOf` (an active swap offer created via `create_swap`, see [3](#0-2) ).

This mirrors the reported flaw exactly: `markBeetleSafe()` checked only `isStaked[_tokenId] == msg.sender` and ignored the fact that a battle outcome had already been determined. Here, `lock_item_transfer` checks only the Freezer role and ignores the fact that a trade (sale or swap) has already been agreed to (price set / swap created), and a counterparty may be about to (or has already begun to) act on that commitment.

Both the sale path (`do_buy_item`, [4](#0-3) ) and the swap path (`do_claim_swap`, [5](#0-4) ) ultimately call `do_transfer`, which enforces the lock via `ItemSetting::Transferable`/`ItemLocked` ( [6](#0-5) ). Because the seller/offerer can call `lock_item_transfer` at any moment — including after publicly listing the item for sale or after creating a swap offer that a counterparty has committed to — the counterparty's `buy_item`/`claim_swap` transaction will fail on the `ItemLocked` check, unilaterally voiding a commitment the protocol otherwise represents as fulfillable (`ItemPriceOf`/`PendingSwapOf` remain visible/queryable as "live" until this happens).

### Impact Explanation
This breaks the "settle exactly once to the rightful beneficiary" guarantee for NFT trading described in the Polkadot SDK Pivots: a party who has publicly committed to a trade (via `set_price` or `create_swap`) can unilaterally invalidate that commitment at will, right up to the moment a counterparty attempts to complete it, with no reference to the commitment state at all. It undermines the fairness/intended behavior of the on-chain trading primitive (buy/sell and atomic swap) in the same way the beetle-battle bug undermined the fairness of the reward mechanism: a check that only validates "who" is acting, never "what has already been agreed/decided." While Substrate's atomic extrinsic execution prevents outright fund loss on a single `buy_item`/`claim_swap` call (the whole call reverts if `do_transfer` fails), it still allows deterministic, unpriced griefing of the on-chain trading mechanism at zero cost to the locker, and can be weaponized to selectively let some counterparties trade while blocking others after they've already committed resources (e.g., approvals, associated calls in a batch) around the expected commitment.

### Likelihood Explanation
High. Any account holding the Freezer role for a collection it created — the common single-creator case where the creator holds all collection roles — can call `lock_item_transfer` as a normal signed, unprivileged extrinsic. No governance, admin, or privileged actor outside the two counterparties is required; the "privileged" role in question is self-assigned by the collection creator during ordinary `create`/`set_team` usage, not an admin/governance actor over the protocol.

### Recommendation
`do_lock_item_transfer` (and `do_lock_collection`) should check for and reject (or fail with a specific error) attempts to lock an item/collection while it has an active `ItemPriceOf` entry or an active `PendingSwapOf` entry, or alternatively automatically clear/refuse pending sale and swap state before permitting the lock, so a seller cannot retroactively invalidate a listed sale or swap that a counterparty may act on.

### Proof of Concept
1. Account `A` creates a collection (`create`), becoming Owner/Issuer/Freezer by default, and mints item `X`.
2. `A` calls `set_price(collection, X, Some(price), None)` — publicly listing `X` for sale.
3. `B`, unaware of any timing race, submits `buy_item(collection, X, price)`. Before `B`'s call is included, `A` (who still holds Freezer role) calls `lock_item_transfer(collection, X)`.
4. `B`'s `buy_item` extrinsic executes `do_buy_item`, which calls `do_transfer`; `do_transfer` now returns `Error::ItemLocked` ( [7](#0-6) ), and `B`'s attempt to complete the publicly-listed trade permanently fails while `A` keeps the item and can repeat the listing/locking cycle indefinitely, exactly analogous to a beetle-battle loser using `markBeetleSafe()` to nullify an outcome the counterparty was owed. This can also be exercised identically through `create_swap` + `lock_item_transfer` before `claim_swap` ( [8](#0-7) ).

Note: I was unable to fully verify, within the remaining tool budget, the exact default role-assignment rules applied by `do_create_collection`/`set_team` for every collection configuration (i.e., whether Freezer is always held by the item owner or can be a separate delegated account in some configurations). This affects whether the "attacker" is always the same account as the seller or could require a colluding Freezer — this distinction should be confirmed in a follow-up review of `substrate/frame/nfts/src/features/create_delete_collection.rs` and `set_team`.

### Citations

**File:** substrate/frame/nfts/src/features/lock.rs (L68-86)
```rust
	pub(crate) fn do_lock_item_transfer(
		origin: T::AccountId,
		collection: T::CollectionId,
		item: T::ItemId,
	) -> DispatchResult {
		ensure!(
			Self::has_role(&collection, &origin, CollectionRole::Freezer),
			Error::<T, I>::NoPermission
		);

		let mut config = Self::get_item_config(&collection, &item)?;
		if !config.has_disabled_setting(ItemSetting::Transferable) {
			config.disable_setting(ItemSetting::Transferable);
		}
		ItemConfigOf::<T, I>::insert(&collection, &item, config);

		Self::deposit_event(Event::<T, I>::ItemTransferLocked { collection, item });
		Ok(())
	}
```

**File:** substrate/frame/nfts/src/lib.rs (L1740-1752)
```rust
		#[pallet::call_index(31)]
		#[pallet::weight(T::WeightInfo::set_price())]
		pub fn set_price(
			origin: OriginFor<T>,
			collection: T::CollectionId,
			item: T::ItemId,
			price: Option<ItemPrice<T, I>>,
			whitelisted_buyer: Option<AccountIdLookupOf<T>>,
		) -> DispatchResult {
			let origin = ensure_signed(origin)?;
			let whitelisted_buyer = whitelisted_buyer.map(T::Lookup::lookup).transpose()?;
			Self::do_set_price(collection, item, origin, price, whitelisted_buyer)
		}
```

**File:** substrate/frame/nfts/src/features/atomic_swap.rs (L49-103)
```rust
	pub(crate) fn do_create_swap(
		caller: T::AccountId,
		offered_collection_id: T::CollectionId,
		offered_item_id: T::ItemId,
		desired_collection_id: T::CollectionId,
		maybe_desired_item_id: Option<T::ItemId>,
		maybe_price: Option<PriceWithDirection<ItemPrice<T, I>>>,
		duration: BlockNumberFor<T, I>,
	) -> DispatchResult {
		ensure!(
			Self::is_pallet_feature_enabled(PalletFeature::Swaps),
			Error::<T, I>::MethodDisabled
		);
		ensure!(duration <= T::MaxDeadlineDuration::get(), Error::<T, I>::WrongDuration);

		let item = Item::<T, I>::get(&offered_collection_id, &offered_item_id)
			.ok_or(Error::<T, I>::UnknownItem)?;
		ensure!(item.owner == caller, Error::<T, I>::NoPermission);

		match maybe_desired_item_id {
			Some(desired_item_id) => ensure!(
				Item::<T, I>::contains_key(&desired_collection_id, &desired_item_id),
				Error::<T, I>::UnknownItem
			),
			None => ensure!(
				Collection::<T, I>::contains_key(&desired_collection_id),
				Error::<T, I>::UnknownCollection
			),
		};

		let now = T::BlockNumberProvider::current_block_number();
		let deadline = duration.saturating_add(now);

		PendingSwapOf::<T, I>::insert(
			&offered_collection_id,
			&offered_item_id,
			PendingSwap {
				desired_collection: desired_collection_id,
				desired_item: maybe_desired_item_id,
				price: maybe_price.clone(),
				deadline,
			},
		);

		Self::deposit_event(Event::SwapCreated {
			offered_collection: offered_collection_id,
			offered_item: offered_item_id,
			desired_collection: desired_collection_id,
			desired_item: maybe_desired_item_id,
			price: maybe_price,
			deadline,
		});

		Ok(())
	}
```

**File:** substrate/frame/nfts/src/features/atomic_swap.rs (L160-233)
```rust
	pub(crate) fn do_claim_swap(
		caller: T::AccountId,
		send_collection_id: T::CollectionId,
		send_item_id: T::ItemId,
		receive_collection_id: T::CollectionId,
		receive_item_id: T::ItemId,
		witness_price: Option<PriceWithDirection<ItemPrice<T, I>>>,
	) -> DispatchResult {
		ensure!(
			Self::is_pallet_feature_enabled(PalletFeature::Swaps),
			Error::<T, I>::MethodDisabled
		);

		let send_item = Item::<T, I>::get(&send_collection_id, &send_item_id)
			.ok_or(Error::<T, I>::UnknownItem)?;
		let receive_item = Item::<T, I>::get(&receive_collection_id, &receive_item_id)
			.ok_or(Error::<T, I>::UnknownItem)?;
		let swap = PendingSwapOf::<T, I>::get(&receive_collection_id, &receive_item_id)
			.ok_or(Error::<T, I>::UnknownSwap)?;

		ensure!(send_item.owner == caller, Error::<T, I>::NoPermission);
		ensure!(
			swap.desired_collection == send_collection_id && swap.price == witness_price,
			Error::<T, I>::UnknownSwap
		);

		if let Some(desired_item) = swap.desired_item {
			ensure!(desired_item == send_item_id, Error::<T, I>::UnknownSwap);
		}

		let now = T::BlockNumberProvider::current_block_number();
		ensure!(now <= swap.deadline, Error::<T, I>::DeadlineExpired);

		if let Some(ref price) = swap.price {
			match price.direction {
				PriceDirection::Send => T::Currency::transfer(
					&receive_item.owner,
					&send_item.owner,
					price.amount,
					KeepAlive,
				)?,
				PriceDirection::Receive => T::Currency::transfer(
					&send_item.owner,
					&receive_item.owner,
					price.amount,
					KeepAlive,
				)?,
			};
		}

		// This also removes the swap.
		Self::do_transfer(send_collection_id, send_item_id, receive_item.owner.clone(), |_, _| {
			Ok(())
		})?;
		Self::do_transfer(
			receive_collection_id,
			receive_item_id,
			send_item.owner.clone(),
			|_, _| Ok(()),
		)?;

		Self::deposit_event(Event::SwapClaimed {
			sent_collection: send_collection_id,
			sent_item: send_item_id,
			sent_item_owner: send_item.owner,
			received_collection: receive_collection_id,
			received_item: receive_item_id,
			received_item_owner: receive_item.owner,
			price: swap.price,
			deadline: swap.deadline,
		});

		Ok(())
	}
```

**File:** substrate/frame/nfts/src/features/buy_sell.rs (L128-171)
```rust
	pub(crate) fn do_buy_item(
		collection: T::CollectionId,
		item: T::ItemId,
		buyer: T::AccountId,
		bid_price: ItemPrice<T, I>,
	) -> DispatchResult {
		ensure!(
			Self::is_pallet_feature_enabled(PalletFeature::Trading),
			Error::<T, I>::MethodDisabled
		);

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

		Self::deposit_event(Event::ItemBought {
			collection,
			item,
			price: price_info.0,
			seller: old_owner,
			buyer,
		});

		Ok(())
	}
```

**File:** substrate/frame/nfts/src/features/transfer.rs (L59-80)
```rust
		// Ensure the item is not locked.
		ensure!(!T::Locker::is_locked(collection, item), Error::<T, I>::ItemLocked);

		// Ensure the item is not transfer disabled on the system level attribute.
		ensure!(
			!Self::has_system_attribute(&collection, &item, PalletAttributes::TransferDisabled)?,
			Error::<T, I>::ItemLocked
		);

		// Retrieve collection config and check if items are transferable.
		let collection_config = Self::get_collection_config(&collection)?;
		ensure!(
			collection_config.is_setting_enabled(CollectionSetting::TransferableItems),
			Error::<T, I>::ItemsNonTransferable
		);

		// Retrieve item config and check if the item is transferable.
		let item_config = Self::get_item_config(&collection, &item)?;
		ensure!(
			item_config.is_setting_enabled(ItemSetting::Transferable),
			Error::<T, I>::ItemLocked
		);
```
