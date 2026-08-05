### Title
Collection Admin/Issuer/Freezer roles are not reset on `transfer_ownership`, letting a former owner retain control of an NFT collection - (File: substrate/frame/nfts/src/features/transfer.rs)

### Summary
`pallet-nfts` (and the near-identical `pallet-uniques`) stores collection-level permission roles (Issuer, Admin, Freezer) in `CollectionRoleOf`, separately from the `owner` field stored in `Collection`. These roles are only cleared when `set_team`/`do_set_team` is explicitly called. `transfer_ownership` / `do_transfer_ownership` changes only `details.owner` and never touches `CollectionRoleOf`, so any Issuer/Admin/Freezer previously appointed by the outgoing owner remains fully privileged after ownership is handed to a new owner. This is the same broken invariant as the LUKSO M-06 finding: a "universal" (non-owner-scoped) permission key persists across an ownership transfer, letting the old controller retain de-facto control of the asset.

### Finding Description
In `substrate/frame/nfts/src/features/transfer.rs`, `do_transfer_ownership` only mutates the `owner` field of `CollectionDetails` and updates `CollectionAccount`/deposit bookkeeping: [1](#0-0) 

Compare this with `do_set_team`, the only function that clears roles, via `Self::clear_roles(&collection)` before reinserting the new role set: [2](#0-1) 

`CollectionRoleOf` is a storage map keyed by `(collection, account)`, independent from the `owner` field in `Collection`: [3](#0-2) 

Because `do_transfer_ownership` never calls `clear_roles`, any account the *previous* owner assigned as Issuer, Admin, or Freezer via `set_team` keeps that role after `transfer_ownership` completes. The `Admin`/`Freezer` roles are checked independently of the current `owner` in downstream extrinsics (e.g. lock/thaw/freeze operations gate on `has_role(..., CollectionRole::Freezer/Admin)`, not on `details.owner`), so the stale role holder can still exercise privileged control (freezing items, locking collection settings, minting under Issuer, etc.) over a collection whose ownership has moved to a new, unsuspecting owner. `pallet-uniques::transfer_ownership` has the analogous gap: it updates `details.owner` but does not reset `details.issuer/admin/freezer`. [4](#0-3) 

### Impact Explanation
This falls under the "runtime bugs that compromise intended behavior" / "unauthorized execution or origin escalation" impact classes: a new owner who acquires a collection via `transfer_ownership` reasonably expects full administrative control, but a malicious or merely negligent prior owner's Admin/Issuer/Freezer appointees remain able to freeze items, lock collection settings/metadata, or otherwise interfere with or "rug" the collection without any privileged, governance, or admin abuse being the root cause — the root cause is a missing state-clearing step in a public, unprivileged extrinsic path (`transfer_ownership`, callable by any collection owner). This can degrade or effectively lock the new owner's control over collection assets, matching the "permanent user-fund or bridge-state lock"/"compromise intended behavior" criteria for the live-scope gate.

### Likelihood Explanation
Likelihood is comparable to the original LUKSO finding: it requires that a collection previously had non-owner roles assigned (a very ordinary configuration via `set_team`) and that ownership is subsequently transferred (e.g. a marketplace sale of a collection, or transfer to a DAO/new operator) without the new owner independently calling `set_team` to reset roles. No malicious peer, validator, or leaked key is needed — this is purely a self-contained public dispatch path (`set_team` then `transfer_ownership`), executable entirely by the account that currently owns the collection, and the danger materializes purely from state that any caller can observe or arrange in advance of a transfer.

### Recommendation
When executing `do_transfer_ownership` (and the same logic in `pallet-uniques`), clear `CollectionRoleOf` for the collection (call `Self::clear_roles(&collection)?` in `pallet-nfts`, or reset `issuer/admin/freezer` fields to the new owner or `None` in `pallet-uniques`) unless the new owner explicitly opts to retain the existing team, mirroring what `do_set_team` already does. Alternatively, require the new owner to affirmatively re-establish the team roles as part of accepting ownership (similar to the existing `set_accept_ownership` flow), and document this expectation clearly so integrators are not silently exposed to stale privileged accounts.

### Proof of Concept
1. Owner A creates a collection: `Nfts::create(A, A, config)`.
2. Owner A calls `Nfts::set_team(A, collection, Some(A), Some(A), Some(A))`, making themselves Issuer/Admin/Freezer as well as owner (`do_set_team` populates `CollectionRoleOf(collection, A) = Issuer|Admin|Freezer`, see [5](#0-4) ).
3. New owner B calls `set_accept_ownership(B, Some(collection))`, then A calls `transfer_ownership(A, collection, B)`. `do_transfer_ownership` updates only `details.owner = B`; `CollectionRoleOf(collection, A)` is left untouched (see [6](#0-5) ).
4. A, no longer the owner, still holds `CollectionRole::Freezer`/`Admin`/`Issuer` and can call collection-locking/freezing extrinsics (which check `has_role`, not `details.owner`) to freeze items, lock collection settings, or mint new items into B's collection without B's consent — despite B believing they now fully control the collection.

### Citations

**File:** substrate/frame/nfts/src/features/transfer.rs (L124-162)
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

			// Update account ownership information.
			CollectionAccount::<T, I>::remove(&details.owner, &collection);
			CollectionAccount::<T, I>::insert(&new_owner, &collection, ());

			details.owner = new_owner.clone();
			OwnershipAcceptance::<T, I>::remove(&new_owner);
			frame_system::Pallet::<T>::dec_consumers(&new_owner);

			// Emit `OwnerChanged` event.
			Self::deposit_event(Event::OwnerChanged { collection, new_owner });
			Ok(())
		})
	}
```

**File:** substrate/frame/nfts/src/features/roles.rs (L38-88)
```rust
	pub(crate) fn do_set_team(
		maybe_check_owner: Option<T::AccountId>,
		collection: T::CollectionId,
		issuer: Option<T::AccountId>,
		admin: Option<T::AccountId>,
		freezer: Option<T::AccountId>,
	) -> DispatchResult {
		Collection::<T, I>::try_mutate(collection, |maybe_details| {
			let details = maybe_details.as_mut().ok_or(Error::<T, I>::UnknownCollection)?;
			let is_root = maybe_check_owner.is_none();
			if let Some(check_origin) = maybe_check_owner {
				ensure!(check_origin == details.owner, Error::<T, I>::NoPermission);
			}

			let roles_map = [
				(issuer.clone(), CollectionRole::Issuer),
				(admin.clone(), CollectionRole::Admin),
				(freezer.clone(), CollectionRole::Freezer),
			];

			// only root can change the role from `None` to `Some(account)`
			if !is_root {
				for (account, role) in roles_map.iter() {
					if account.is_some() {
						ensure!(
							Self::find_account_by_role(&collection, *role).is_some(),
							Error::<T, I>::NoPermission
						);
					}
				}
			}

			let roles = roles_map
				.into_iter()
				.filter_map(|(account, role)| account.map(|account| (account, role)))
				.collect();

			let account_to_role = Self::group_roles_by_account(roles);

			// Delete the previous records.
			Self::clear_roles(&collection)?;

			// Insert new records.
			for (account, roles) in account_to_role {
				CollectionRoleOf::<T, I>::insert(&collection, &account, roles);
			}

			Self::deposit_event(Event::TeamChanged { collection, issuer, admin, freezer });
			Ok(())
		})
	}
```

**File:** substrate/frame/nfts/src/features/roles.rs (L97-121)
```rust
	pub(crate) fn clear_roles(collection_id: &T::CollectionId) -> Result<(), DispatchError> {
		let res = CollectionRoleOf::<T, I>::clear_prefix(
			&collection_id,
			CollectionRoles::max_roles() as u32,
			None,
		);
		ensure!(res.maybe_cursor.is_none(), Error::<T, I>::RolesNotCleared);
		Ok(())
	}

	/// Returns true if a specified account has a provided role within that collection.
	///
	/// - `collection_id`: A collection to check the role in.
	/// - `account_id`: An account to check the role for.
	/// - `role`: A role to validate.
	///
	/// Returns `true` if the account has the specified role, `false` otherwise.
	pub(crate) fn has_role(
		collection_id: &T::CollectionId,
		account_id: &T::AccountId,
		role: CollectionRole,
	) -> bool {
		CollectionRoleOf::<T, I>::get(&collection_id, &account_id)
			.map_or(false, |roles| roles.has_role(role))
	}
```

**File:** substrate/frame/uniques/src/lib.rs (L866-904)
```rust
		#[pallet::call_index(11)]
		#[pallet::weight(T::WeightInfo::transfer_ownership())]
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

				CollectionAccount::<T, I>::remove(&details.owner, &collection);
				CollectionAccount::<T, I>::insert(&new_owner, &collection, ());

				details.owner = new_owner.clone();
				OwnershipAcceptance::<T, I>::remove(&new_owner);
				frame_system::Pallet::<T>::dec_consumers(&new_owner);

				Self::deposit_event(Event::OwnerChanged { collection, new_owner });
				Ok(())
			})
		}
```
