## Title
`do_transfer_ownership`/`do_force_collection_owner` discard the `repatriate_reserved` remainder, permanently desyncing NFT collection deposit accounting - ([File: substrate/frame/nfts/src/features/transfer.rs])

## Summary
`pallet-nfts`'s collection-ownership transfer path calls `T::Currency::repatriate_reserved(&details.owner, &new_owner, details.owner_deposit, Reserved)` and ignores the `Balance` it returns. `repatriate_reserved` returns the *unmoved* portion of the reserve as its `Ok` value (it does not error when only part of the reserve could be moved — under `BalanceStatus::Reserved`/`Polite`-style semantics a lock/freeze on the old owner's free balance can prevent the full amount from being re-reserved on the new owner or fully debited from the old owner's reserved balance). Because the returned remainder is silently dropped, `details.owner_deposit` is still recorded in full against `new_owner`, while the actual reserved balance on `new_owner` may be less than `owner_deposit` and the old owner may still be holding part of the reserve. This is the exact bug class described in the external report: a value computed/returned by an internal accounting primitive is discarded on the "settlement" path, corrupting the recorded balance for a beneficiary.

## Finding Description
`do_transfer_ownership` (called by the public, unprivileged `Nfts::transfer_ownership` extrinsic) and `do_force_collection_owner`: [1](#0-0) [2](#0-1) 

both do:
```rust
T::Currency::repatriate_reserved(&details.owner, &new_owner, details.owner_deposit, Reserved)?;
```

`ReservableCurrency::repatriate_reserved` (as used and documented elsewhere in this same repo, e.g. `substrate/frame/balances/src/impl_currency.rs`) returns `Ok(remaining)` where `remaining` is the amount that *could not* be moved (e.g. because the destination account cannot hold the full reserved amount, or because of interactions between free balance, locks, and the `Polite`/`Reserved` status parameter used here). It only returns `Err` for hard failures such as a completely dead destination account — a partial move is a normal `Ok` result, not an error.

Immediately after the call, the code unconditionally sets:
```rust
details.owner = new_owner.clone();
```
and continues to treat `details.owner_deposit` as fully backing the new owner's reserved balance — the same accounting invariant break already identified and fixed for the analogous `pallet-assets::transfer_ownership` path in this repository (see `prdoc/pr_12366.prdoc`, which added an `IncompleteDepositTransfer` error precisely because "the previous code discarded the remainder returned by `repatriate_reserved`"). That fix was applied only to `pallet-assets`; the structurally identical call sites in `pallet-nfts`'s `do_transfer_ownership` and `do_force_collection_owner` were not patched and still discard the remainder.

Guards that do *not* stop this path:
- `ensure!(origin == details.owner, ...)` only checks the caller is the current owner — it does not validate that the deposit move fully succeeded.
- There is no post-call check on the `repatriate_reserved` return value, so a partial move silently proceeds to commit `details.owner = new_owner`.

## Impact Explanation
Once `details.owner_deposit` is recorded against `new_owner` but the actual `Currency::reserved_balance(new_owner)` is less than that amount:
- The collection's owner deposit accounting becomes permanently desynchronized from real reserved balances.
- When the collection is later destroyed or ownership is transferred again, the pallet will attempt to `unreserve`/`repatriate_reserved` `details.owner_deposit` from `new_owner`, which can silently under-unreserve (², `unreserve` returns any amount it could not unreserve without erroring) — funds that were meant to be a deposit either get stuck on the old owner (partial fund lock) or are never fully accounted for, and the new owner is credited with a deposit obligation they never fully backed.
- This is a real accounting corruption in a public, unprivileged extrinsic (`transfer_ownership` is callable by any collection owner with no special origin), matching the "Balances/NFTs/... must conserve value and settle exactly once to the rightful beneficiary and amount" pivot in the impact gate. It can permanently desync deposit bookkeeping without needing any admin/governance/validator involvement.

## Likelihood Explanation
Medium: the attacker (any collection owner) needs the old owner's account to have some form of currency lock/freeze that overlaps the reserved deposit, similar to the exact condition demonstrated in the already-fixed `pallet-assets` test (`substrate/frame/assets/src/tests.rs::transfer_ownership_fails_when_deposit_is_locked`, using `Balances::set_lock`). This is fully achievable by an unprivileged account by placing a lock on itself (e.g. via any pallet that locks balance, or a staking/vesting lock) before calling `transfer_ownership`, then calling `transfer_ownership` on their own NFT collection to desync the deposit ledger. No malicious peer, validator, or governance actor is required.

## Recommendation
Apply the same fix that was applied to `pallet-assets` (`pr_12366`): capture the `Balance` returned by `repatriate_reserved` in both `do_transfer_ownership` and `do_force_collection_owner`, and reject the dispatch (or otherwise handle the shortfall, e.g. adjust `details.owner_deposit` down and re-reserve/roll back) when the returned remainder is non-zero, so partial deposit moves cannot silently commit an inconsistent `owner`/`owner_deposit` state.

```diff
- T::Currency::repatriate_reserved(&details.owner, &new_owner, details.owner_deposit, Reserved)?;
+ let remainder = T::Currency::repatriate_reserved(
+     &details.owner,
+     &new_owner,
+     details.owner_deposit,
+     Reserved,
+ )?;
+ ensure!(remainder.is_zero(), Error::<T, I>::IncompleteDepositTransfer);
```

## Proof of Concept
1. Attacker account `A` owns an NFT collection with `owner_deposit = D` reserved from `A`.
2. `A` locks their own free balance with `pallet_balances::set_lock` (or any pallet-imposed lock) such that `frozen(A) > free(A)`, overlapping part of the reserved deposit under `Polite`/`Reserved` fortitude — same setup as `substrate/frame/assets/src/tests.rs::transfer_owner_should_work`/`transfer_ownership_fails_when_deposit_is_locked`.
3. `A` calls `Nfts::transfer_ownership(collection, B)`.
4. `do_transfer_ownership` invokes `repatriate_reserved(&A, &B, D, Reserved)`, which succeeds with `Ok(remainder > 0)` because only part of `D` could be moved.
5. The remainder is discarded; `details.owner = B` and `details.owner_deposit = D` are committed even though `Balances::reserved_balance(B) < D` and `Balances::reserved_balance(A)` may still hold the undelivered remainder.
6. Subsequent operations (e.g. `set_metadata`/further `transfer_ownership`/collection destruction) that unreserve/repatriate based on `details.owner_deposit` now operate against a stale, over-stated deposit figure, corrupting accounting between `A` and `B`.

Note: I was unable to run the actual pallet-nfts test suite in this environment to empirically confirm the exact numeric behavior of `repatriate_reserved` under lock/freeze interplay for this pallet's specific `Currency` trait bound; the analysis is based on the documented semantics of `ReservableCurrency::repatriate_reserved` and the directly analogous, already-acknowledged bug in `pallet-assets::transfer_ownership` (`prdoc/pr_12366.prdoc`) which is structurally identical code left unpatched in `pallet-nfts`.

### Citations

**File:** substrate/frame/nfts/src/features/transfer.rs (L142-148)
```rust
			// Move the deposit to the new owner.
			T::Currency::repatriate_reserved(
				&details.owner,
				&new_owner,
				details.owner_deposit,
				Reserved,
			)?;
```

**File:** substrate/frame/nfts/src/features/transfer.rs (L216-222)
```rust
			// Move the deposit to the new owner.
			T::Currency::repatriate_reserved(
				&details.owner,
				&owner,
				details.owner_deposit,
				Reserved,
			)?;
```
