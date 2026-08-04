Confirmed: `IncompleteDepositTransfer` was only added to `pallet-assets` (`substrate/frame/assets/src/lib.rs`), fixed by PR documented in `prdoc/pr_12366.prdoc`. The identical `repatriate_reserved(...)?` pattern remains unguarded in `pallet-nfts` (`substrate/frame/nfts/src/features/transfer.rs`) and `pallet-uniques` (`substrate/frame/uniques/src/lib.rs`), each with only 1-2 occurrences and no corresponding remainder check.

### Title
Unchecked `repatriate_reserved` remainder in `pallet-nfts`/`pallet-uniques` `transfer_ownership` desyncs recorded deposit from actually-reserved funds - ([File: substrate/frame/nfts/src/features/transfer.rs])

### Summary
`pallet-nfts::do_transfer_ownership` (and the structurally identical `pallet-uniques::transfer_ownership`) move a collection's aggregate deposit to the new owner via `T::Currency::repatriate_reserved(&details.owner, &new_owner, details.owner_deposit, Reserved)` and then unconditionally flip `details.owner` to the new account, discarding the `Balance` remainder that `repatriate_reserved` returns when the old owner's reserved balance cannot be fully moved (e.g., due to a lock/freeze overlapping the reserve under `Polite` fortitude). This is the exact bug class that was identified and fixed in the sibling pallet, `pallet-assets`, in `prdoc/pr_12366.prdoc`, but the fix was never ported to `pallet-nfts`/`pallet-uniques`.

### Finding Description
In `substrate/frame/nfts/src/features/transfer.rs:142-154`: [1](#0-0) 
the call `T::Currency::repatriate_reserved(&details.owner, &new_owner, details.owner_deposit, Reserved)?` only propagates a hard error; it silently accepts a `BestEffort`/partial move (the `Ok(Balance)` return value indicates how much of `details.owner_deposit` could *not* be repatriated). The code then immediately does:
```
CollectionAccount::<T, I>::remove(&details.owner, &collection);
CollectionAccount::<T, I>::insert(&new_owner, &collection, ());
details.owner = new_owner.clone();
```
without checking that the remainder is zero. The identical pattern exists in `substrate/frame/uniques/src/lib.rs:886-897` (`transfer_ownership` dispatchable).

This mirrors the reported DebtToken bug: two logically-linked pieces of state (here, "who owns the collection" vs. "who actually holds the reserved deposit backing it") are updated through separate code paths that are not kept atomically in sync — ownership flips even when the deposit accounting did not fully follow. `pallet-assets` already recognized this exact defect and fixed it (`prdoc/pr_12366.prdoc`) by capturing the remainder and rejecting the dispatch with a new `IncompleteDepositTransfer` error, rolling back the transactional storage mutation. `pallet-nfts` and `pallet-uniques` were not updated with the equivalent guard, so `grep` confirms `IncompleteDepositTransfer` only exists in `substrate/frame/assets/src/lib.rs`, while `repatriate_reserved` is still called unguarded in `substrate/frame/nfts/src/features/transfer.rs` and `substrate/frame/uniques/src/lib.rs`.

### Impact Explanation
After a successful `transfer_ownership`/`do_transfer_ownership` call where a lock/freeze on the old owner prevented full repatriation:
- `CollectionDetails.owner` (and `owner_deposit`) record the **new** owner as backing the full deposit, while part of the actual reserved balance is still held under the **old** owner's account.
- When the collection (or its items/metadata) is later destroyed/unreserved, the runtime will attempt to unreserve/refund based on the new owner, but the actual currency sits with the old owner — this can permanently strand part of the deposit (funds-lock) or allow accounting to diverge such that unreserve operations against the wrong account fail or double-count.
- This is a public, unprivileged-origin entrypoint (`transfer_ownership`, callable by the current collection owner against any consenting new owner) with no admin/governance/malicious-validator prerequisite — it only requires the old owner's balance to be locked/frozen by some other on-chain mechanism (e.g., staking, vesting, or another pallet's `Freeze`/`Lock`), which is common on production chains.

### Likelihood Explanation
Locks/freezes overlapping a user's free balance are routine (staking bonds, vesting, governance locks, `NamedReservableCurrency` holds from other pallets). Any collection owner whose account happens to have a lock touching the reserved portion — or an owner who intentionally creates such a lock — can trigger a permanently desynced deposit on a normal, public `transfer_ownership` call. No privileged role, relayer, validator, or malicious peer is required, matching the report's threshold for a locally provable analog.

### Recommendation
Port the `pallet-assets` fix from `prdoc/pr_12366.prdoc` to `pallet-nfts` and `pallet-uniques`: capture the `Balance` returned by `repatriate_reserved`, `ensure!(remaining.is_zero(), Error::<T, I>::IncompleteDepositTransfer)` (adding that error variant), and keep the surrounding logic inside the existing `try_mutate`/transactional context so a partial move rolls back the whole ownership transfer instead of committing a desynced state.

### Proof of Concept
1. Collection owner `A` creates a collection via `pallet-nfts::create_collection`, reserving `owner_deposit` D as part of `T::Currency`.
2. `A` acquires a lock/freeze (e.g., via `pallet-vesting`, `pallet-staking` bond, or any `LockableCurrency`/`Fungible::hold` consumer configured on the runtime) that overlaps enough of `A`'s free+reserved balance so that, under `Polite` fortitude, `repatriate_reserved` cannot move the full `D` to a new owner `B`.
3. `B` calls `set_accept_ownership(collection)`; `A` calls `transfer_ownership(collection, B)`.
4. `repatriate_reserved` returns `Ok(remainder>0)` (no error), so execution continues: `CollectionAccount` and `details.owner` are updated to `B`, and `OwnerChanged` is emitted, even though only `D - remainder` was actually moved.
5. `A` still holds `remainder` reserved under the `StorageDeposit`/collection hold reason while accounting now attributes the full `D` to `B`; subsequent collection destruction/deposit release logic operates on `B` only, leaving `A`'s `remainder` reserved balance permanently stuck (fund lock) with no code path to release it. [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** substrate/frame/nfts/src/features/transfer.rs (L142-162)
```rust
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

**File:** substrate/frame/uniques/src/lib.rs (L886-903)
```rust
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
```

**File:** substrate/frame/assets/src/lib.rs (L1340-1354)
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
		}
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
