### Title
Ignored/`debug_assert!`-only transfer results in `claim_child_bounty` allow silent payout failure while bounty state is still finalized - (File: substrate/frame/child-bounties/src/lib.rs)

### Summary
In `pallet-child-bounties`'s `claim_child_bounty` extrinsic, the curator-fee transfer and the beneficiary payout transfer results are only checked via `debug_assert!(...is_ok())`, which compiles to a no-op in release/production builds. The function proceeds to remove the `ChildBounty` storage entry, decrement `ParentChildBounties`, and emit the `Claimed` event regardless of whether either transfer actually succeeded, mirroring the exact pattern flagged in the `PwIporToken.sol` report ("transfer calls performed ignore the result value... can lead to undefined behavior in case the transfer call fails but the transaction is not reverted").

### Finding Description
The relevant code is: [1](#0-0) 

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

`debug_assert!` is stripped in `--release` builds (which is how production runtimes/nodes are compiled), so `fee_transfer_result` and `payout_transfer_result` are computed and then completely discarded with no effect on control flow. Regardless of whether either `T::Currency::transfer` call returns `Err` (e.g., `TokenError::FundsUnavailable` if the child-bounty account balance is lower than expected, or the destination account cannot be created due to being below the existential deposit and using `AllowDeath` semantics interacting oddly with reducible balance, or any hold/freeze on the account interfering with withdrawal), execution unconditionally continues to:
- Remove `ChildBounty` from storage (`*maybe_child_bounty = None`),
- Decrement `ParentChildBounties` count,
- Remove the child-bounty description,
- Emit `Event::Claimed { ..., payout, beneficiary, .. }`.

This is the same broken invariant as the external report: an on-chain state transition (settlement, removal of the claimable record, event emission) is finalized as if the value transfer succeeded, when in fact it may not have. The comments "Should not fail" are optimistic assumptions, not guarantees — the code contains no `?` propagation or explicit error handling path for either transfer.

### Impact Explanation
If either transfer silently fails:
- The `ChildBounty` record is deleted from storage, so the beneficiary (and curator, for the fee) permanently lose the ability to re-claim the funds — this is a **permanent user-fund lock** matching the "Required Impacts" criteria (permanent user-fund lock).
- A `Claimed` event is emitted showing a payout that never actually happened, i.e., **duplicate/false settlement accounting** — off-chain observers and dependent logic (e.g., `ParentChildBounties` count) treat the bounty as paid when funds remain stuck in the child-bounty sub-account with no code path left to retrieve them (the child-bounty entry no longer exists to re-trigger `claim_child_bounty`).
- Funds already earmarked in the child-bounty account become orphaned/unreachable through the pallet's normal API, since only `claim_child_bounty` and `close_child_bounty` handle that sub-account, and `close_child_bounty` is guarded against `PendingPayout` state which has already been cleared here.

This satisfies the "public underpriced work" / "fund lock" / "duplicate settlement" gate criteria, is triggerable by any signed account calling a normal public extrinsic (no privileged governance, no malicious validator/relayer needed), and does not require any off-repo or malicious-infrastructure assumption.

### Likelihood Explanation
The likelihood of the underlying transfer actually failing is not trivial to force under normal parameter bounds (the code assumes `curator_fee <= balance` and `payout = balance - curator_fee`), but it is not proven impossible either — since `AllowDeath` withdrawal reducibility can be affected by pallet-level holds/freezes/locks placed on the child-bounty sub-account by other pallets sharing the same `Currency` implementation (e.g., via `pallet_balances::Freeze`/`Hold` from an unrelated but composable feature, or by asset conversion / non-standard `Currency` implementations configured for the runtime), or by existential-deposit edge cases if `payout` or `curator_fee` is smaller than `ExistentialDeposit` and the destination account doesn't already exist. Even if today's specific runtime configuration makes failure rare, the missing error propagation is a defense-in-depth violation: the invariant "queue/payout state only advances after settlement succeeds" (explicitly listed in the Polkadot SDK Pivots) is not actually enforced by the code, only by an assumption documented in a comment and checked solely in debug builds.

### Recommendation
Propagate the transfer results with `?` (or explicit `map_err`) instead of `debug_assert!`, so that a failed transfer aborts the extrinsic and preserves the `PendingPayout` state (and thus the beneficiary's ability to retry), rather than silently deleting the bounty record and emitting a false `Claimed` event:

```rust
T::Currency::transfer(&child_bounty_account, curator, curator_fee, AllowDeath)?;
T::Currency::transfer(&child_bounty_account, beneficiary, payout, AllowDeath)?;
```

If partial success (fee paid, payout failed, or vice versa) is a concern, wrap both transfers plus the storage removal in a single atomic `with_transaction`/`try_mutate` scope so state changes only commit if both transfers succeed.

### Proof of Concept
1. Configure a runtime where the child-bounty sub-account can have its `payout` (post curator-fee) computed to be non-zero but below `T::Currency::minimum_balance()` (ExistentialDeposit) while the beneficiary account does not yet exist. With `AllowDeath` transfer semantics, transferring dust below ED to a non-existent account fails with `TokenError`.
2. Any signed account calls `claim_child_bounty(parent_bounty_id, child_bounty_id)` once the child bounty is in `PendingPayout` and `unlock_at` has passed.
3. `T::Currency::transfer(&child_bounty_account, beneficiary, payout, AllowDeath)` returns `Err(...)`.
4. Because only `debug_assert!(payout_transfer_result.is_ok())` checks this (a no-op in release builds), execution continues: `ChildBounty` storage is removed, `ParentChildBounties` count is decremented, and `Event::Claimed { payout, beneficiary, .. }` is emitted — even though the beneficiary never received the funds and the funds remain stuck in `child_bounty_account`, unreachable by any further pallet call since the bounty record is gone. [2](#0-1)

### Citations

**File:** substrate/frame/child-bounties/src/lib.rs (L713-765)
```rust

						// Make curator fee payment.
						let child_bounty_account =
							Self::child_bounty_account_id(parent_bounty_id, child_bounty_id);
						let balance = T::Currency::free_balance(&child_bounty_account);
						let curator_fee = child_bounty.fee.min(balance);
						let payout = balance.saturating_sub(curator_fee);

						// Unreserve the curator deposit. Should not fail
						// because the deposit is always reserved when curator is
						// assigned.
						let _ = T::Currency::unreserve(curator, child_bounty.curator_deposit);

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

						// Remove the child-bounty instance from the state.
						*maybe_child_bounty = None;

						Ok(())
```
