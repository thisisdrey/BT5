Audit Report

## Title
Curator deposit permanently locked if refund/payout payment never reaches `Succeeded` in `check_status` - ([File: substrate/frame/multi-asset-bounties/src/lib.rs])

## Summary
`check_status` only releases the held `CuratorDeposit` consideration (`CuratorDeposit::<T, I>::take(...)` + `T::Consideration::drop(...)`) inside the `PaymentState::Succeeded` arms of the `RefundAttempted` and `PayoutAttempted` branches. [1](#0-0) [2](#0-1)  For every other payment status (`Pending`, `Failed`, `Attempted`), the bounty simply re-enters the same `RefundAttempted`/`PayoutAttempted` state, leaving `CuratorDeposit` untouched, and the pallet has no other extrinsic that unconditionally releases this deposit.

## Finding Description
The state machine in `check_status` at [3](#0-2)  is the only path that drops `CuratorDeposit`, and it does so exclusively in the `PaymentState::Succeeded` arms. If the underlying `T::Paymaster`/`T::Consideration` payment mechanism never resolves to `Succeeded` for a given `asset_kind`/beneficiary combination (e.g. a stalled foreign-asset payout or an asset kind that is unsupported by the configured paymaster), `check_status` will repeatedly loop back into the same `RefundAttempted`/`PayoutAttempted` status via the `Pending | Failed | Attempted` arm, and the `CuratorDeposit` entry — and the balance it represents — remains parked indefinitely. There is no permissionless reclaim/withdraw call in this pallet analogous to `pallet-bounties::reclaim_bounty_funds`.

## Impact Explanation
This matches the "permanent user-fund lock" impact class in the accepted impact gate: a curator's real held balance (via `T::Consideration`) can become permanently inaccessible if the payment backend never reports `Succeeded`. This is a genuine gap in the pallet's own state machine as written — there is no code path outside the `Succeeded` branch that ever calls `T::Consideration::drop` for `CuratorDeposit`.

## Likelihood Explanation
No privileged or malicious actor is required. A routine/misconfigured paymaster backend, or an asset kind whose payment target can never satisfy the success predicate, is sufficient to keep re-entering the `Pending`/`Failed`/`Attempted` arm indefinitely, since `check_status` is a plain signed extrinsic callable by anyone, and each call only re-evaluates the same branch.

## Recommendation
Add a permissionless reclaim/expiry path for `CuratorDeposit` entries — e.g., allow the deposit to be released back to the curator once a bounty has been sitting in `RefundAttempted`/`PayoutAttempted` beyond a maximum retry count or expiry, rather than only inside the `Succeeded` arm of `check_status`, mirroring the fix applied to `pallet-bounties` via `reclaim_bounty_funds`.

## Proof of Concept
1. Create a multi-asset bounty, assign and accept a curator so `CuratorDeposit::<T, I>` is populated via `T::Consideration`.
2. Drive the bounty into `RefundAttempted` or `PayoutAttempted` state (e.g. via `close_bounty`/`process_payout`).
3. Configure a `Paymaster`/`Consideration` backend whose `check_payment` status never resolves to `Success` for the chosen `asset_kind`/beneficiary (e.g., unsupported asset kind or unreachable beneficiary).
4. Call `check_status` repeatedly; each call re-enters the `Pending | Failed | Attempted` arm at [4](#0-3)  or [5](#0-4) , and `CuratorDeposit::<T, I>` for that `(parent_bounty_id, child_bounty_id)` is never `take`n, so the curator's held balance remains locked indefinitely with no other extrinsic capable of releasing it.

### Citations

**File:** substrate/frame/multi-asset-bounties/src/lib.rs (L1214-1292)
```rust
				RefundAttempted { ref payment_status, ref curator } => {
					let new_payment_status = Self::do_check_refund_payment_status(
						parent_bounty_id,
						child_bounty_id,
						payment_status.clone(),
					)?;

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

					let weight = <T as Config<I>>::WeightInfo::check_status_refund();

					(new_status, weight)
				},
				PayoutAttempted { ref curator, ref beneficiary, ref payment_status } => {
					let new_payment_status = Self::do_check_payout_payment_status(
						parent_bounty_id,
						child_bounty_id,
						asset_kind,
						value,
						beneficiary.clone(),
						payment_status.clone(),
					)?;

					let new_status = match new_payment_status {
						PaymentState::Succeeded => {
							if let Some(curator_deposit) =
								CuratorDeposit::<T, I>::take(parent_bounty_id, child_bounty_id)
							{
								// Drop the curator deposit when both payments succeed
								// If the child curator is the parent curator, the
								// deposit is 0
								T::Consideration::drop(curator_deposit, curator)?;
							}
							// payout succeeded, cleanup the bounty
							Self::remove_bounty(parent_bounty_id, child_bounty_id, metadata);
							return Ok(Pays::No.into());
						},
						PaymentState::Pending |
						PaymentState::Failed |
						PaymentState::Attempted { .. } => BountyStatus::PayoutAttempted {
							curator: curator.clone(),
							beneficiary: beneficiary.clone(),
							payment_status: new_payment_status.clone(),
						},
					};

					let weight = <T as Config<I>>::WeightInfo::check_status_payout();

					(new_status, weight)
				},
```
