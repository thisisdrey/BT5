Audit Report

## Title
`pallet-assets::transfer_ownership` moves ownership and its reserved deposit onto a new owner without requiring their consent - (File: `substrate/frame/assets/src/lib.rs`)

## Summary
`Assets::transfer_ownership` allows the current asset owner to unilaterally reassign the `owner` role — and force-move the asset's reserved deposit — to any account, without any acceptance step from that account. [1](#0-0)  By contrast, `pallet-uniques::transfer_ownership` and `pallet-nfts::do_transfer_ownership` gate the identical operation behind an `OwnershipAcceptance` consent record, rejecting the call with `Error::Unaccepted` unless the destination previously opted in. [2](#0-1) [3](#0-2) 

## Finding Description
In `Assets::transfer_ownership`, the checks performed are limited to `details.status == AssetStatus::Live`, `origin == details.owner`, and that `repatriate_reserved` moves the full deposit (`remaining.is_zero()`). There is no check on the destination `owner`'s consent before `details.owner = owner` is set and `repatriate_reserved(&details.owner, &owner, deposit, Reserved)` moves the deposit onto their account. [4](#0-3) 

The sibling pallets implement the same conceptual operation — an owner handoff for a similarly-modeled collection/asset with a reserved deposit — but require the new owner to first call `set_accept_ownership`, storing an entry in `OwnershipAcceptance`, and check it with `ensure!(acceptable_collection.as_ref() == Some(&collection), Error::<T, I>::Unaccepted)` before mutating owner state or moving the deposit. [5](#0-4) [6](#0-5)  This confirms the maintainers treat consent as a necessary control for this exact class of operation, and its absence in `pallet-assets` is a genuine inconsistency rather than an intentional design choice specific to assets.

The existing test `transfer_owner_should_work` demonstrates the mechanics: account 1 (attacker-equivalent) unilaterally moves its reserved balance and owner role onto account 2 with no action from account 2. [7](#0-6) 

## Impact Explanation
`repatriate_reserved` force-moves the asset's reserved deposit (asset deposit + metadata deposit) from the attacker onto the victim's account and reassigns `details.owner` to the victim, all without the victim's consent. [8](#0-7)  This forces reserved balance onto a victim account chosen unilaterally by any current asset owner, locking part of the victim's balance in the `Reserved` state against their will and burdening them with the administrative `owner` role (and its obligations, e.g. needing to call `destroy` or `transfer_ownership` again) while any pre-existing `Issuer`/`Admin`/`Freezer` roles remain under the attacker's control. This matches the required "permanent user-fund lock" impact category until the victim notices and unwinds the unwanted ownership/deposit.

## Likelihood Explanation
The attack requires only unprivileged, signed, public extrinsics: `Assets::create` (or using any asset the attacker already owns) followed by `Assets::transfer_ownership` naming the victim. No governance, admin, validator, or malicious-peer assumption is required, and the path is fully reachable by any external account.

## Recommendation
Add an `OwnershipAcceptance`-style consent gate to `pallet-assets::transfer_ownership`, mirroring `pallet-uniques`/`pallet-nfts`: require the destination account to call a new `set_accept_ownership(id)` extrinsic first, and check that acceptance (returning an `Unaccepted`-style error otherwise) before mutating `details.owner` or calling `repatriate_reserved`.

## Proof of Concept
1. Attacker calls `Assets::create(origin=attacker, id=X, admin=attacker, min_balance=1)`, reserving a deposit from their own balance.
2. Attacker calls `Assets::transfer_ownership(origin=attacker, id=X, owner=victim)`.
3. In `Asset::<T,I>::try_mutate`, `ensure!(origin == details.owner, ...)` passes since attacker is the current owner; no check exists on `victim`'s consent. [9](#0-8) 
4. `repatriate_reserved(&attacker, &victim, deposit, Reserved)` executes, moving the reserved deposit onto `victim`'s account and setting `details.owner = victim`. [10](#0-9) 
5. `victim` now unexpectedly holds reserved balance and the `owner` role for asset `X` without ever signing or approving anything — contrast with `Uniques::transfer_ownership`/`Nfts::transfer_ownership`, where the identical call fails with `Error::Unaccepted` unless `victim` first called `set_accept_ownership`. [11](#0-10)

### Citations

**File:** substrate/frame/assets/src/lib.rs (L1322-1354)
```rust
		#[pallet::call_index(15)]
		pub fn transfer_ownership(
			origin: OriginFor<T>,
			id: T::AssetIdParameter,
			owner: AccountIdLookupOf<T>,
		) -> DispatchResult {
			let origin = ensure_signed(origin)?;
			let owner = T::Lookup::lookup(owner)?;
			let id: T::AssetId = id.into();

			Asset::<T, I>::try_mutate(id.clone(), |maybe_details| {
				let details = maybe_details.as_mut().ok_or(Error::<T, I>::Unknown)?;
				ensure!(details.status == AssetStatus::Live, Error::<T, I>::AssetNotLive);
				ensure!(origin == details.owner, Error::<T, I>::NoPermission);
				if details.owner == owner {
					return Ok(());
				}

				let metadata_deposit = Metadata::<T, I>::get(&id).deposit;
				let deposit = details.deposit + metadata_deposit;

				// `repatriate_reserved` is best-effort: reject any partial move so the recorded
				// deposit stays in sync with what is actually reserved on the owner.
				let remaining =
					T::Currency::repatriate_reserved(&details.owner, &owner, deposit, Reserved)?;
				ensure!(remaining.is_zero(), Error::<T, I>::IncompleteDepositTransfer);

				details.owner = owner.clone();

				Self::deposit_event(Event::OwnerChanged { asset_id: id, owner });
				Ok(())
			})
		}
```

**File:** substrate/frame/uniques/src/lib.rs (L868-892)
```rust
		pub fn transfer_ownership(
			origin: OriginFor<T>,
			collection: T::CollectionId,
			new_owner: AccountIdLookupOf<T>,
		) -> DispatchResult {
			let origin = ensure_signed(origin)?;
			let new_owner = T::Lookup::lookup(new_owner)?;

			let acceptable_collection = OwnershipAcceptance::<T, I>::get(&new_owner);
			ensure!(acceptable_collection.as_ref() == Some(&collection), Error::<T, I>::Unaccepted);

			Collection::<T, I>::try_mutate(collection.clone(), |maybe_details| {
				let details = maybe_details.as_mut().ok_or(Error::<T, I>::UnknownCollection)?;
				ensure!(origin == details.owner, Error::<T, I>::NoPermission);
				if details.owner == new_owner {
					return Ok(());
				}

				// Move the deposit to the new owner.
				T::Currency::repatriate_reserved(
					&details.owner,
					&new_owner,
					details.total_deposit,
					Reserved,
				)?;
```

**File:** substrate/frame/nfts/src/features/transfer.rs (L124-148)
```rust
	pub(crate) fn do_transfer_ownership(
		origin: T::AccountId,
		collection: T::CollectionId,
		new_owner: T::AccountId,
	) -> DispatchResult {
		// Check if the new owner is acceptable based on the collection's acceptance settings.
		let acceptable_collection = OwnershipAcceptance::<T, I>::get(&new_owner);
		ensure!(acceptable_collection.as_ref() == Some(&collection), Error::<T, I>::Unaccepted);

		// Try to retrieve and mutate the collection details.
		Collection::<T, I>::try_mutate(collection, |maybe_details| {
			let details = maybe_details.as_mut().ok_or(Error::<T, I>::UnknownCollection)?;
			// Check if the `origin` is the current owner of the collection.
			ensure!(origin == details.owner, Error::<T, I>::NoPermission);
			if details.owner == new_owner {
				return Ok(());
			}

			// Move the deposit to the new owner.
			T::Currency::repatriate_reserved(
				&details.owner,
				&new_owner,
				details.owner_deposit,
				Reserved,
			)?;
```

**File:** substrate/frame/assets/src/tests.rs (L959-990)
```rust
#[test]
fn transfer_owner_should_work() {
	build_and_execute(|| {
		Balances::make_free_balance_be(&1, 100);
		Balances::make_free_balance_be(&2, 100);
		assert_ok!(Assets::create(RuntimeOrigin::signed(1), 0, 1, 1));
		assert_eq!(asset_ids(), vec![0, 999]);

		assert_eq!(Balances::reserved_balance(&1), 1);

		assert_ok!(Assets::transfer_ownership(RuntimeOrigin::signed(1), 0, 2));
		assert_eq!(Balances::reserved_balance(&2), 1);
		assert_eq!(Balances::reserved_balance(&1), 0);

		assert_noop!(
			Assets::transfer_ownership(RuntimeOrigin::signed(1), 0, 1),
			Error::<Test>::NoPermission
		);

		// Set metadata now and make sure that deposit gets transferred back.
		assert_ok!(Assets::set_metadata(
			RuntimeOrigin::signed(2),
			0,
			vec![0u8; 10],
			vec![0u8; 10],
			12
		));
		assert_ok!(Assets::transfer_ownership(RuntimeOrigin::signed(2), 0, 1));
		assert_eq!(Balances::reserved_balance(&1), 22);
		assert_eq!(Balances::reserved_balance(&2), 0);
	});
}
```
