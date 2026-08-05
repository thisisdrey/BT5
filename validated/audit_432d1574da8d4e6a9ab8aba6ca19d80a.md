Audit Report

## Title
Unchecked balance transfer in `claim_child_bounty` causes silent-failure payouts and permanent fund lock - (File: `substrate/frame/child-bounties/src/lib.rs`)

## Summary
`pallet-child-bounties::claim_child_bounty` transfers the curator fee and beneficiary payout from the child-bounty's sovereign account and guards the results only with `debug_assert!`, which is a no-op in release builds. If either transfer fails, the extrinsic still returns `Ok(())`, deletes the child-bounty storage record, decrements `ParentChildBounties`, and emits a `Claimed` event as though the payout succeeded, permanently orphaning the funds in the child-bounty account with no remaining on-chain link to the intended recipient.

## Finding Description
In `claim_child_bounty`, `T::Currency::transfer` is called twice — once to pay the curator fee and once to pay the beneficiary — and both results are only checked with `debug_assert!`: [1](#0-0) 

`debug_assert!` compiles to a no-op when `debug_assertions` is disabled, which is the case for standard `--release` builds used for production runtime WASM blobs. Consequently, the `Result` returned by `T::Currency::transfer` is discarded in production: regardless of success or failure, execution proceeds to unconditionally emit `Event::Claimed` and delete the child-bounty's tracking state: [2](#0-1) 

The same anti-pattern exists in `pallet-tips::payout_tip`, confirming this is a recurring pattern rather than an isolated typo: [3](#0-2) 

The only origin check is `ensure_signed(origin)?`, so this is a fully public, unprivileged extrinsic once a child bounty reaches `PendingPayout`, with no restriction to curator/beneficiary. If a transfer fails for any `DispatchError` reason (e.g., destination hitting `MaxConsumers`, a hold/freeze interaction introduced by other pallets composed into a given runtime's `Currency` implementation, or existential-deposit edge cases even with `AllowDeath`), the call still succeeds, the `Claimed` event misreports settlement, and the storage record proving the earmarked relationship between `child_bounty_account_id(parent_bounty_id, child_bounty_id)` and `beneficiary`/`curator` is deleted — leaving funds stranded in the sovereign account with no remaining on-chain path to reclaim them through the normal bounty-claim flow.

## Impact Explanation
This maps to the "permanent user-fund lock" and "settle exactly once to the rightful beneficiary and amount" impact classes in the gate: the `Claimed` event and deleted storage falsely represent a completed payout with a named beneficiary and amount while funds may remain unmoved in the child-bounty sovereign account, and the record needed to retry or recover the payout is destroyed unconditionally right after the (possibly failed) transfer calls.

## Likelihood Explanation
Triggering the call itself requires no privilege — any signed account can call `claim_child_bounty` once the child bounty is `PendingPayout` and `unlock_at` has elapsed. However, actually causing the underlying `T::Currency::transfer` to fail requires a specific precondition on the currency implementation (e.g., a beneficiary account at `MaxConsumers`, or interaction with holds/freezes from other composed pallets) that is not trivially attacker-controlled in every runtime configuration, making this a low-to-moderate likelihood, environment-dependent issue — but the `debug_assert!` guard provides no protection whatsoever in any production build once such a condition is met.

## Recommendation
Replace both `debug_assert!` checks with proper error propagation (e.g., via `?` or `.map_err(...)?`) mapped to a dedicated error such as `Error::<T>::PayoutFailed`, and only mutate/delete the child-bounty storage entries and emit `Event::Claimed` after both transfers are confirmed to succeed — mirroring the pattern already used correctly in `cumulus/pallets/ah-ops/src/lib.rs`: [4](#0-3) 
Apply the equivalent fix to `pallet-tips::payout_tip`.

## Proof of Concept
1. Configure a test runtime where the `Currency` implementation used by `pallet-child-bounties` can be made to reject a transfer to a specific destination (e.g., a mock currency or a real `pallet-balances` setup where the beneficiary account is already at `MaxConsumers`, causing the implicit `inc_consumers` inside `transfer` to fail).
2. Create a parent bounty, add and approve a child bounty, assign a curator, and advance the child bounty to `PendingPayout` with `beneficiary` set to the engineered account.
3. Advance the block number past `unlock_at` and call `claim_child_bounty(origin, parent_bounty_id, child_bounty_id)` from any signed account, compiled without `debug_assertions` (release profile).
4. Observe that the call returns `Ok(())`, `Event::Claimed { payout, beneficiary, .. }` is emitted, `ChildBounties` and `ChildBountyDescriptionsV1` entries are removed, and `ParentChildBounties` is decremented — while the beneficiary's balance is unchanged and the funds remain in `child_bounty_account_id(parent_bounty_id, child_bounty_id)` with no remaining storage record linking them to the beneficiary.

### Citations

**File:** substrate/frame/child-bounties/src/lib.rs (L726-744)
```rust
						// Make payout to child-bounty curator.
						// Should not fail because curator fee is always less than bounty value.
						let fee_transfer_result = T::Currency::transfer(
							&child_bounty_account,
							curator,
							curator_fee,
							AllowDeath,
						);
						debug_assert!(fee_transfer_result.is_ok());

						// Make payout to beneficiary.
						// Should not fail.
						let payout_transfer_result = T::Currency::transfer(
							&child_bounty_account,
							beneficiary,
							payout,
							AllowDeath,
						);
						debug_assert!(payout_transfer_result.is_ok());
```

**File:** substrate/frame/child-bounties/src/lib.rs (L746-760)
```rust
						// Trigger the Claimed event.
						Self::deposit_event(Event::<T>::Claimed {
							index: parent_bounty_id,
							child_index: child_bounty_id,
							payout,
							beneficiary: beneficiary.clone(),
						});

						// Update the active child-bounty tracking count.
						ParentChildBounties::<T>::mutate(parent_bounty_id, |count| {
							count.saturating_dec()
						});

						// Remove the child-bounty description.
						ChildBountyDescriptionsV1::<T>::remove(parent_bounty_id, child_bounty_id);
```

**File:** substrate/frame/tips/src/lib.rs (L587-600)
```rust
		if tip.finders_fee && tip.finder != tip.who {
			// pay out the finder's fee.
			let finders_fee = T::TipFindersFee::get() * payout;
			payout -= finders_fee;
			// this should go through given we checked it's at most the free balance, but still
			// we only make a best-effort.
			let res = T::Currency::transfer(&treasury, &tip.finder, finders_fee, KeepAlive);
			debug_assert!(res.is_ok());
		}

		// same as above: best-effort only.
		let res = T::Currency::transfer(&treasury, &tip.who, payout, KeepAlive);
		debug_assert!(res.is_ok());
		Self::deposit_event(Event::TipClosed { tip_hash: hash, who: tip.who, payout });
```

**File:** cumulus/pallets/ah-ops/src/lib.rs (L536-539)
```rust
			// Now the actual balance transfer to the new account
			<T as Config>::Currency::transfer(from, to, total, Preservation::Expendable)
				.defensive()
				.map_err(|_| Error::<T>::FailedToTransfer)?;
```
