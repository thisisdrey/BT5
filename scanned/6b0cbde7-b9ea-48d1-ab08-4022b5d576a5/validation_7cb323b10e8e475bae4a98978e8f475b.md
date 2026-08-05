Based on my research, the strongest local analog to the TON "stranded/unrecoverable order balance" bug is in the Substrate bounty pallets — specifically the same "funds get parked in a per-item sub-account whose future life-cycle can dead-end, with no recovery path" pattern that was explicitly identified and fixed in `pallet-bounties` (PR fixing issue #10996), but which I could not confirm has an equivalent fix in the newer `pallet-multi-asset-bounties`.

### Title
Bounty/child-bounty sub-accounts can strand transferred value with no recovery path once the parent bounty leaves the active state - ([File: substrate/frame/multi-asset-bounties/src/lib.rs])

### Summary
The TON report's core broken invariant is: a contract-held balance is funded via a forwarded message value into a dedicated sub-account (`order` contract) whose payout path can be permanently invalidated (parameter change, expiry) by a later, ordinary action, and no code path lets anyone move that value back out. The same invariant class exists in Substrate's bounty pallets: value is transferred into a deterministic per-bounty/child-bounty sub-account (`bounty_account_id` / `child_bounty_account_id`), and if the bounty is closed/cancelled/unassigned while a refund or payout is in-flight or fails, the sub-account can retain balance with no dedicated permissionless sweep.

### Finding Description
`pallet-bounties` had exactly this bug: value sent to `Self::bounty_account_id(bounty_id)` could become stranded once the bounty was removed from `Bounties` storage (e.g. after `close_bounty`, or after partial `TransferAllAssets` failures caused by locks), because there was no dispatchable that could later reach into a bounty account no longer tracked by storage. This was fixed by adding the permissionless `reclaim_bounty_funds` extrinsic [1](#0-0) , referencing the underlying issue about "stranded funds from closed bounty accounts" [2](#0-1) .

`pallet-multi-asset-bounties`, a newer, parallel implementation that manages both parent and child bounties with asset-kind-aware, asynchronous payments via `T::Paymaster` (`close_bounty` → `do_process_refund_payment` → `BountyStatus::RefundAttempted { payment_status, curator }`) [3](#0-2) , follows the identical sub-account-funding pattern (`CuratorDeposit` holds, per-bounty asset accounts) but was not part of the crate bump list for the `reclaim_bounty_funds` fix [4](#0-3) . Because refunds here go through an async `Paymaster` rather than a synchronous `Currency::transfer`, the same "payment fails/never completes, sub-account balance is unreachable" scenario that motivated `reclaim_bounty_funds` in `pallet-bounties` is structurally present, and I could not locate an equivalent permissionless sweep call in the sections of `substrate/frame/multi-asset-bounties/src/lib.rs` I reviewed (call indices 0–6, ending at `close_bounty`).

This mirrors the TON scenario precisely: legitimate, non-malicious lifecycle actions (closing/cancelling a bounty, unassigning a curator, a payment provider permanently failing) leave value in a dedicated on-chain account whose associated bookkeeping entry has already been removed or transitioned to a terminal state, with no guaranteed path back to the rightful owner (the treasury / originating asset pool).

### Impact Explanation
If confirmed, this is a permanent user/treasury-fund lock: value paid into a bounty or child-bounty sub-account (native or non-native asset) becomes permanently unreachable once the bounty record is closed and refund payments do not settle, with no privileged or permissionless mechanism to reclaim it. This falls under "permanent user-fund or bridge-state lock" in the impact gate and does not require a malicious peer/validator/governance actor — it is triggered by routine bounty closure combined with a stalled/failed asynchronous payment, i.e., normal protocol operation exposing a design gap, exactly as in the original TON report.

### Likelihood Explanation
Medium-to-low confidence: I verified the pattern that motivated the `pallet-bounties` fix and confirmed `pallet-multi-asset-bounties` shares the same sub-account/async-payment structure without seeing an analogous reclaim extrinsic in the code I was able to inspect. However, due to tool truncation I was **not able to fully enumerate** all dispatchables in `pallet-multi-asset-bounties` (a `grep_search` for `retry_payment`/`check_status`/`reclaim` returned 51 matches whose content I could not read before the session ended). It is possible the pallet already implements `retry_payment`/`check_status` flows that eventually resolve stuck payments, or a reclaim-style call beyond call index 6 that I did not view. This uncertainty should be resolved before treating this as a confirmed, exploitable finding.

### Recommendation
- Audit `pallet-multi-asset-bounties` for a permissionless fallback that can sweep any remaining native/asset balance out of a bounty or child-bounty account once its status is terminal (`RefundAttempted` fully settled/failed with no further retries possible, or the record has been removed), analogous to `reclaim_bounty_funds` in `pallet-bounties`.
- Ensure `check_status`/`retry_payment` (if present) cannot leave a bounty in a state where the sub-account balance is orphaned (record removed) without the balance having actually moved.
- Add regression tests mirroring `reclaim_bounty_funds_respects_native_locks` and the closed-bounty scenarios in `substrate/frame/bounties/src/tests.rs` for the multi-asset pallet.

### Proof of Concept
Not executable — this requires confirming, via full read of `substrate/frame/multi-asset-bounties/src/lib.rs`, whether an equivalent to `reclaim_bounty_funds` exists. The conceptual PoC (subject to that confirmation) is:
1. Create and fund a bounty in `pallet-multi-asset-bounties`; assign and activate a curator.
2. Call `close_bounty`, triggering `do_process_refund_payment`, which dispatches an async `Paymaster` refund and sets `BountyStatus::RefundAttempted`.
3. Force the `Paymaster` refund to fail permanently (e.g., destination account removed/frozen) so `check_status`/`retry_payment` cannot make progress.
4. Observe that the bounty's asset sub-account retains the original value with no remaining dispatchable able to move it back to the treasury or originating pool — mirroring the TON order-contract balance that becomes stuck after the enclosing multisig's parameters change.

Given the unresolved uncertainty above, if a maintainer confirms `pallet-multi-asset-bounties` already has an equivalent reclaim path, this finding should be downgraded to informational/no-vulnerability.

### Citations

**File:** substrate/frame/bounties/src/lib.rs (L1058-1080)
```rust
		#[pallet::call_index(11)]
		#[pallet::weight(<T as Config<I>>::WeightInfo::reclaim_bounty_funds())]
		pub fn reclaim_bounty_funds(
			origin: OriginFor<T>,
			#[pallet::compact] bounty_id: BountyIndex,
		) -> DispatchResultWithPostInfo {
			ensure_signed(origin)?;

			// A live bounty still manages its account, so leave it untouched.
			ensure!(!Bounties::<T, I>::contains_key(bounty_id), Error::<T, I>::BountyStillActive);

			debug_assert!(
				T::ChildBountyManager::child_bounties_count(bounty_id) == 0,
				"child bounties should not exist for a closed bounty"
			);

			let bounty_account = Self::bounty_account_id(bounty_id);
			let treasury_account = Self::account_id();

			let transferred = T::TransferAllAssets::force_transfer_all_assets(
				&bounty_account,
				&treasury_account,
			)?;
```

**File:** prdoc/pr_11045.prdoc (L1-19)
```text
title: '[pallet-bounties]: add `reclaim_bounty_funds` to reclaim stranded funds from
  closed bounty accounts'
doc:
- audience: Runtime Dev
  description: |-
    fixes https://github.com/paritytech/polkadot-sdk/issues/10996

    This PR adds a permissionless `reclaim_bounty_funds` extrinsic that moves all
    funds stranded in a closed bounty's account back to the treasury in a single
    call. It reclaims both the native token and any fungible assets configured via
    the `TransferAllAssets` associated type. Native funds are moved using
    `transfer_all` semantics (reducible balance with `Expendable` preservation) so
    locks and freezes are respected. The call is free on success and paid on a no-op,
    so it cannot be used to grief the network.
crates:
- name: pallet-bounties
  bump: major
- name: rococo-runtime
  bump: major
```

**File:** substrate/frame/multi-asset-bounties/src/lib.rs (L1126-1137)
```rust
			let payment_status = Self::do_process_refund_payment(
				parent_bounty_id,
				child_bounty_id,
				asset_kind,
				value,
				None,
			)?;
			let new_status = BountyStatus::RefundAttempted {
				payment_status: payment_status.clone(),
				curator: maybe_curator.clone(),
			};
			Self::update_bounty_status(parent_bounty_id, child_bounty_id, new_status)?;
```
