Found a strong local analog in `pallet-multi-asset-bounties`.

### Title
Curator deposit and stranded bounty-account funds are permanently lost on multi-asset bounty close paths - ([File: substrate/frame/multi-asset-bounties/src/lib.rs])

### Summary
The external report's core invariant is: a pallet-controlled account receives value on behalf of the protocol (fees), but no code path exists to move that value back out, permanently locking it. The Polkadot SDK repository shows this exact bug class recurring across the bounty family of pallets. `pallet-bounties` needed a dedicated `reclaim_bounty_funds` fix and a `close_bounty` refund fix because closed bounty accounts retained un-returned native/asset balances [1](#0-0) [2](#0-1) , and `pallet-multi-asset-bounties` needed a fix because `unassign_curator` could leak a curator's native balance hold permanently when authorization was mishandled [3](#0-2) .

### Finding Description
The identical class of "value moved into a pallet-owned sub-account with no unconditional exit path" persists in `pallet-multi-asset-bounties`'s refund/payout state-machine. `check_status` on a bounty in `RefundAttempted`/`PayoutAttempted` state releases the `CuratorDeposit` consideration only inside the `PaymentState::Succeeded` branch via `CuratorDeposit::<T, I>::take(...)` and `T::Consideration::drop(curator_deposit, curator)` [4](#0-3) . For every other branch (`Pending`, `Failed`, `Attempted`), the bounty simply cycles back into `RefundAttempted`/`PayoutAttempted` and the `CuratorDeposit` entry (and the balance it represents, held via `T::Consideration`) stays parked, waiting indefinitely for `Succeeded`. If the underlying `T::Paymaster`/consideration payment never resolves to `Succeeded` (a stalled foreign-asset payout, an unsupported asset kind, or a payout target that can never satisfy the payment predicate — none of which require a malicious actor, only routine asset/paymaster misbehavior that is squarely in-scope as "runtime bugs that compromise intended behavior"), the curator's deposit is held by the pallet forever with no permissionless extraction function analogous to `pallet-bounties`'s newly added `reclaim_bounty_funds`. This mirrors `MarketFactory#fund`/`Market#claimFee` exactly: value is correctly routed into a pallet-controlled holding position, but the only release valve is gated behind a payment-success branch that is not guaranteed to be reached, and there is no fallback withdraw/reclaim call in this pallet.

### Impact Explanation
Curator deposits (real native currency held via `T::Consideration`) can become permanently inaccessible to their rightful owner (the curator) if a refund/payout never transitions to `Succeeded`. This is a direct "permanent user-fund lock" impact matching the required-impact list, analogous to the sibling `pallet-bounties` bug that was serious enough to require both a dedicated permissionless `reclaim_bounty_funds` extrinsic [1](#0-0)  and a `close_bounty` balance-return fix [2](#0-1) . `pallet-multi-asset-bounties` has no equivalent unconditional reclaim path for stuck `CuratorDeposit` consideration entries.

### Likelihood Explanation
No privileged actor, governance action, or malicious relayer/validator is required — a routine stuck/never-succeeding `Paymaster`/consideration payment status (e.g., an asset kind whose payment target permanently reports `Pending`/`Failed`, or a beneficiary account state that never allows `Succeeded`) is sufficient to trap the curator deposit indefinitely, since `check_status` only releases the hold on the `Succeeded` arm.

### Recommendation
Add a permissionless reclaim/withdraw path for `CuratorDeposit` entries analogous to `pallet-bounties::reclaim_bounty_funds`, allowing deposits to be released back to the curator (or swept to the treasury) once a bounty has been closed or once a payment has definitively expired/failed beyond retry, rather than only inside the `Succeeded` branch of `check_status`.

### Proof of Concept
1. Create a multi-asset bounty, assign and accept a curator (curator deposit is placed on hold via `T::Consideration`).
2. Drive the bounty into `RefundAttempted` or `PayoutAttempted` state.
3. Configure/observe a `Paymaster`/consideration backend whose `check_payment` status never resolves to `Success` for the chosen `asset_kind`/beneficiary combination (e.g., an asset kind removed from the runtime's supported paymaster set, or a beneficiary account that cannot receive the asset) — this only requires calling `check_status` repeatedly; each call re-enters the same branch shown at [5](#0-4) .
4. Observe that `CuratorDeposit::<T, I>` for that `(parent_bounty_id, child_bounty_id)` is never `take`n and `T::Consideration::drop` is never called, so the curator's held balance remains locked indefinitely with no other extrinsic in the pallet capable of releasing it.

Note: I was not able to fully trace every possible `T::Consideration`/`Paymaster` backend configured in downstream runtimes to confirm whether some concrete implementation always guarantees eventual `Succeeded` transition; this assessment is based on the pallet's own state machine as written, which does not itself guarantee that guarantee.

### Citations

**File:** prdoc/pr_11045.prdoc (L1-18)
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
```

**File:** prdoc/stable2603/pr_10729.prdoc (L1-15)
```text
title: '[FRAME] Bounties return balance and assets on close'
doc:
- audience: Runtime Dev
  description: |-
    Ensures that bounties that got closed with `close_bounty` will return the maximal possible
    amount of Native balance and specific relevant Assets.  
    This fixes an issue where closed bounties would not refund any balance to the treasury because
    assets were blocking the withdrawal through account references.
crates:
- name: pallet-bounties
  bump: major
- name: rococo-runtime
  bump: major
- name: pallet-child-bounties
  bump: patch
```

**File:** prdoc/stable2603-1/pr_11612.prdoc (L1-23)
```text
title: "fix(multi-asset-bounties): enforce authorization in unassign_curator when parent bounty is not Active"
doc:
- audience: Runtime Dev
  description: |-
    Fix an authorization bypass in `unassign_curator` where any signed account could forcibly
    unassign an active child bounty's curator when the parent bounty was not in `Active` state.
    The child curator's native balance hold (deposit) was also permanently leaked in this path.

    The `BountyStatus::Active` catch-all arm now uses `parent_curator.ok_or(BadOrigin)?` to
    explicitly reject callers when no parent curator is available, matching the defensive pattern
    already used in the `Funded` arm. `CuratorDeposit::take()` is also moved after authorization
    to prevent storage mutation on unauthorized calls.

    A regression test is added covering the full attack scenario.
- audience: Runtime User
  description: |-
    Previously, any account could unassign a child bounty curator without authorization when the
    parent bounty's curator had been unassigned. This is now correctly rejected. If you were
    affected by a permanently locked curator deposit from this bug, a migration or manual
    intervention may be needed to release the held balance.
crates:
- name: pallet-multi-asset-bounties
  bump: patch
```

**File:** substrate/frame/multi-asset-bounties/src/lib.rs (L1221-1250)
```rust
					let new_status = match new_payment_status {
						PaymentState::Succeeded => {
							if let Some(curator) = curator {
								// Drop the curator deposit when payment succeeds
								// If the parent curator is also the child curator, there
								// is no deposit
								if let Some(curator_deposit) =
									CuratorDeposit::<T, I>::take(parent_bounty_id, child_bounty_id)
								{
									T::Consideration::drop(curator_deposit, curator)?;
								}
							}
							if let Some(_) = child_bounty_id {
								// Revert the value back to parent bounty
								ChildBountiesValuePerParent::<T, I>::mutate(
									parent_bounty_id,
									|total_value| *total_value = total_value.saturating_sub(value),
								);
							}
							// refund succeeded, cleanup the bounty
							Self::remove_bounty(parent_bounty_id, child_bounty_id, metadata);
							return Ok(Pays::No.into());
						},
						PaymentState::Pending |
						PaymentState::Failed |
						PaymentState::Attempted { .. } => BountyStatus::RefundAttempted {
							payment_status: new_payment_status,
							curator: curator.clone(),
						},
					};
```
