### Title
`pallet-uniques::transfer_ownership` silently drops undelivered deposit on best-effort `repatriate_reserved`, permanently locking the original owner's reserved funds - (File: `substrate/frame/uniques/src/lib.rs`)

### Summary
`Pallet::transfer_ownership` moves a collection's recorded `total_deposit` to the new owner by calling `T::Currency::repatriate_reserved(&details.owner, &new_owner, details.total_deposit, Reserved)` and then unconditionally updates `details.owner = new_owner` regardless of how much was actually moved. `repatriate_reserved` is explicitly documented as best-effort: it moves "as much funds up to `value` will be deducted as possible" and returns any un-moved remainder instead of failing. The uniques pallet ignores that returned remainder entirely, so ownership (and therefore all future refund/destroy logic keyed on `details.owner`) can be transferred to a new account while part or all of the actual reserved deposit remains stuck on the old, now-disowned account.

### Finding Description
`transfer_ownership` at [1](#0-0)  does:
```rust
T::Currency::repatriate_reserved(&details.owner, &new_owner, details.total_deposit, Reserved)?;
CollectionAccount::<T, I>::remove(&details.owner, &collection);
CollectionAccount::<T, I>::insert(&new_owner, &collection, ());
details.owner = new_owner.clone();
```
`repatriate_reserved` is defined at [2](#0-1)  and is explicitly non-atomic: it only errors if `Err`, but on partial success it returns `Ok(non_zero_remainder)` — the call above uses `?` only to propagate a hard `Err`, and completely discards the `Ok(remainder)` value. If the old owner's reserved balance for that deposit overlaps with a hold/freeze or is otherwise only partially reserved (e.g., due to slashing, other holds competing for the same funds, or partial unreserve elsewhere), only part of `total_deposit` is actually moved to `new_owner`, yet `details.owner` is unconditionally switched to `new_owner` and the collection's bookkeeping (`total_deposit`) is left unchanged, now implicitly "belonging" to an account that never received (all of) the funds.

This is structurally identical to the `AllocationVesting.transferPoints` bug: a transfer of a *right/ownership record* (`points` in the report, `details.owner`/`CollectionAccount` here) is executed without atomically and proportionally moving the *accounting-linked liability* tied to that record (`preclaimed` in the report, `total_deposit`/actual reserved balance here). The record-holder changes hands cleanly, but the consumption/liability state does not follow it, breaking the invariant that ownership and its associated deposit must move together exactly once.

Confirmation that this exact bug class exists and was already fixed for the sibling pallet: [3](#0-2)  shows `pallet-assets::transfer_ownership` was patched to capture the `repatriate_reserved` remainder and reject the dispatch with a new `IncompleteDepositTransfer` error when non-zero, rolling back the storage change: [4](#0-3) . `pallet-uniques::transfer_ownership` was never updated with the equivalent guard, so it retains the vulnerable pre-fix pattern.

### Impact Explanation
- The prior owner's reserved deposit can become permanently stuck: since `CollectionAccount`/`Collection::owner` now points to `new_owner`, only `new_owner` (or team members it appoints) can call further collection management calls (`transfer_ownership` again, `destroy`, etc.). The old owner has no remaining path to trigger `unreserve`/repatriation of the leftover reserved amount tied to this collection deposit, resulting in a permanent user-fund lock — matching the "permanent user-fund ... lock" impact class.
- Conversely, `new_owner` becomes recorded as responsible for `total_deposit` without having actually received the full backing reserve, so any future refund path (e.g. on `destroy`) that unreserves `total_deposit` from `details.owner` (now `new_owner`) will unreserve funds `new_owner` never had reserved for this purpose, creating accounting desync between recorded deposit and actual custody of funds.
- This is an unprivileged, permissionless path (only requires being the current collection owner and target having called `set_accept_ownership`), reachable by any user, not requiring a malicious validator/relayer/admin — satisfying the "public dispatch wrapper causing fund lock/duplicate settlement" acceptance criteria.

### Likelihood Explanation
`repatriate_reserved`'s balances implementation genuinely supports partial fulfillment (that's the whole reason the function returns a remainder rather than simply failing); the condition is reachable whenever the old owner's reserved balance under the relevant status is less than `total_deposit` at the time of the call (e.g., interleaved holds/freezes/other reserve consumers reducing available reserved balance, or race with another reserve-consuming operation in the same block). No admin or governance action is needed to trigger it — an attacker/owner can even engineer the shortfall themselves (e.g. by having part of their reserved balance consumed by another reserve operation) immediately before calling `transfer_ownership`, then walk away from a partially-unpaid ownership transfer, leaving the counterparty's or their own funds mis-accounted.

### Recommendation
Mirror the fix already applied to `pallet-assets::transfer_ownership`: capture the `Ok(remainder)` result of `repatriate_reserved` and reject the dispatch (rolling back all storage writes) if `remainder` is non-zero, e.g.:
```rust
let remaining = T::Currency::repatriate_reserved(
    &details.owner, &new_owner, details.total_deposit, Reserved,
)?;
ensure!(remaining.is_zero(), Error::<T, I>::IncompleteDepositTransfer);
```
Apply the same pattern to any other pallet using `repatriate_reserved` in an ownership/ deposit-transfer call without checking the returned remainder (e.g. review `pallet-nfts` if it has an analogous `transfer_ownership`).

### Proof of Concept
Conceptual reproduction (illustrates the missing-remainder-check path; exact numeric setup depends on `Currency` mock configuration allowing a shortfall in reserved balance under `Reserved` status at call time):
1. Owner `A` creates a collection, `total_deposit = D` is reserved from `A`.
2. Before calling `transfer_ownership`, reduce `A`'s reserved balance available under the `Reserved` status below `D` (e.g., via a competing reserve/slash operation, or a runtime configuration where other pallets share the same reserve bucket).
3. `B` calls `set_accept_ownership(collection)`.
4. `A` calls `Uniques::transfer_ownership(collection, B)`. `repatriate_reserved(A, B, D, Reserved)` moves only `D' < D` and returns `Ok(D - D')`, which is discarded.
5. Assert: `Collection::get(collection).owner == B` while `A` still has `D - D'` reserved for this collection with no owner-permission path to retrieve it, and `B`'s recorded ownership implies full backing of `D` that was never actually transferred.

### Citations

**File:** substrate/frame/uniques/src/lib.rs (L868-904)
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

**File:** substrate/frame/support/src/traits/tokens/currency/reservable.rs (L69-81)
```rust
	/// Moves up to `value` from reserved balance of account `slashed` to balance of account
	/// `beneficiary`. `beneficiary` must exist for this to succeed. If it does not, `Err` will be
	/// returned. Funds will be placed in either the `free` balance or the `reserved` balance,
	/// depending on the `status`.
	///
	/// As much funds up to `value` will be deducted as possible. If this is less than `value`,
	/// then `Ok(non_zero)` will be returned.
	fn repatriate_reserved(
		slashed: &AccountId,
		beneficiary: &AccountId,
		value: Self::Balance,
		status: BalanceStatus,
	) -> Result<Self::Balance, DispatchError>;
```

**File:** prdoc/pr_12366.prdoc (L1-14)
```text
title: 'pallet-assets: enforce full deposit transfer in transfer_ownership'
doc:
- audience: Runtime Dev
  description: |-
    `transfer_ownership` previously discarded the remainder returned by `repatriate_reserved`.
    Under `Polite` fortitude, a lock or freeze on the current owner that overlaps their free
    balance can leave part of the reserved deposit behind, leaving the asset's recorded deposit
    out of sync with what is actually reserved. The call now captures the returned remainder and
    rejects the dispatch with a new `IncompleteDepositTransfer` error when it is non-zero;
    the storage layer rolls back the partial move. To recover, the current owner must clear or
    reduce the offending locks/freezes and retry.
crates:
- name: pallet-assets
  bump: major
```

**File:** substrate/frame/assets/src/lib.rs (L1340-1353)
```rust
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
```
