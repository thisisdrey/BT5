Confirmed. The code exactly matches the claim in every material respect: `propose_curator` [1](#0-0)  unconditionally adds `fee` to `ChildrenCuratorFees` on every call from the `Added` state, `unassign_curator` resets status to `Added` in all four branches (`CuratorProposed`, `Active` via three sub-branches, `PendingPayout`) without ever decrementing `ChildrenCuratorFees` [2](#0-1) , and `claim_child_bounty` removes the child bounty and pays the fee but never decrements `ChildrenCuratorFees` either [3](#0-2) . Only `impl_close_child_bounty` performs the corresponding subtraction. This allows the `propose_curator` → `unassign_curator` → `propose_curator` cycle (reachable by the parent bounty's own curator, an unprivileged signed account, and even self-service unassignment by the proposed curator) to inflate `ChildrenCuratorFees` beyond what will ever be paid, which is then consumed unguarded (`debug_assert!` only, compiled out in release) in `claim_bounty`'s `final_fee = fee.saturating_sub(children_fee)`.

Audit Report

## Title
Stale child-bounty curator fee is never subtracted on `unassign_curator`, causing `ChildrenCuratorFees` to permanently overcount and lock parent-bounty payouts - (File: `substrate/frame/child-bounties/src/lib.rs`)

## Summary
`propose_curator` unconditionally adds `fee` to the cumulative `ChildrenCuratorFees::<T>` storage for the parent bounty on every call, but `unassign_curator` resets the child-bounty status back to `Added` from `CuratorProposed`, `Active`, or `PendingPayout` in every branch without ever decrementing `ChildrenCuratorFees`. A repeated `propose_curator` → `unassign_curator` cycle on the same child bounty therefore accumulates multiple stale fee amounts into `ChildrenCuratorFees`, even though only one (or zero) child-bounty fee will ever actually be paid.

## Finding Description
The invariant required is that `ChildrenCuratorFees[parent_bounty_id]` equals the sum of `fee` for all currently pending/active child bounties, so that `claim_bounty`'s `final_fee = fee.saturating_sub(children_fee)` correctly deducts exactly the amount owed to child curators. This is broken because `propose_curator` mutates `ChildrenCuratorFees::<T>::mutate(parent_bounty_id, |value| *value = value.saturating_add(fee))` every time it transitions a child bounty from `Added` to `CuratorProposed` [1](#0-0) , while `unassign_curator` sets `child_bounty.status = ChildBountyStatus::Added` at the end of every match branch (`CuratorProposed`, `Active`, `PendingPayout`) with no corresponding decrement of `ChildrenCuratorFees` anywhere in the function [2](#0-1) . The proposed-but-not-accepted curator is explicitly permitted to self-unassign under the `CuratorProposed` branch, making the reset attacker/self-controllable without any privileged origin. `claim_child_bounty` also never decrements `ChildrenCuratorFees` when the fee is actually settled and the child bounty removed [3](#0-2) ; only `impl_close_child_bounty`'s `ChildrenCuratorFees::<T>::mutate(parent_bounty_id, |value| *value = value.saturating_sub(child_bounty.fee))` performs the reverse operation. Because of this asymmetry, repeated propose/unassign cycles permanently inflate the accumulator relative to the fee that will genuinely ever be paid out.

## Impact Explanation
`claim_bounty` in `pallet-bounties` computes `let final_fee = fee.saturating_sub(children_fee)` guarded only by `debug_assert!(children_fee <= fee)`, which is compiled out in release/production builds. An inflated `children_fee` can drive `final_fee` to zero, silently shortchanging the legitimate parent bounty curator's payout — a runtime bug that compromises intended payout behavior and produces incorrect fund allocation for the parent curator versus child curators.

## Likelihood Explanation
The parent bounty curator is an ordinary signed account with no special chain privileges. It can call `propose_curator` and have the proposed curator (or itself, or after the inactivity window, any signed account) call `unassign_curator` to reset the child bounty to `Added`, then call `propose_curator` again — a fully public, repeatable sequence using only the pallet's own dispatchables, requiring no malicious validator, collator, or off-chain infrastructure. This can be repeated across many child bounties up to `MaxActiveChildBountyCount` before the parent bounty is claimed.

## Recommendation
Decrement `ChildrenCuratorFees::<T>` by `child_bounty.fee` (and reset `child_bounty.fee` to zero) in `unassign_curator` whenever transitioning out of `CuratorProposed`, `Active`, or `PendingPayout` back to `Added`, mirroring the pattern in `impl_close_child_bounty`. Additionally, decrement `ChildrenCuratorFees` by the claimed fee in `claim_child_bounty` before/at removal of the child bounty, since the fee is now settled. Ensure every path that adds to `ChildrenCuratorFees` has a matching removal path so the accumulator never exceeds the sum of currently pending child-bounty fees.

## Proof of Concept
1. Create parent bounty and child bounty `C` with value `V`.
2. `propose_curator(parent, C, curator_X, fee=F1)` → `ChildrenCuratorFees[parent] = F1`.
3. `curator_X` calls `unassign_curator(parent, C)` → status reset to `Added`; `ChildrenCuratorFees[parent]` remains `F1`.
4. `propose_curator(parent, C, curator_Y, fee=F2)` → `ChildrenCuratorFees[parent] = F1 + F2` for the same single child bounty.
5. Repeat steps 3–4 `n` times to accumulate `ChildrenCuratorFees[parent] = F1 + F2 + ... + Fn`.
6. Accept curator, award, and claim the child bounty normally — `ChildrenCuratorFees` is untouched by `claim_child_bounty`, so the inflated sum persists.
7. Award and claim the parent bounty via `Bounties::claim_bounty`: `final_fee = fee.saturating_sub(children_fee)` uses the inflated `children_fee`, which can exceed the parent's actual `fee`, driving `final_fee` to `0` and underpaying the parent curator, contrary to the correct flow demonstrated in `substrate/frame/child-bounties/src/tests.rs` (lines 1416–1436) which expects `101 + 6 - fee`.

### Citations

**File:** substrate/frame/child-bounties/src/lib.rs (L386-398)
```rust
					// Add child-bounty curator fee to the cumulative sum. To be
					// subtracted from the parent bounty curator when claiming
					// bounty.
					ChildrenCuratorFees::<T>::mutate(parent_bounty_id, |value| {
						*value = value.saturating_add(fee)
					});

					// Update the child-bounty curator fee.
					child_bounty.fee = fee;

					// Update the child-bounty state.
					child_bounty.status =
						ChildBountyStatus::CuratorProposed { curator: child_bounty_curator };
```

**File:** substrate/frame/child-bounties/src/lib.rs (L527-598)
```rust
					match child_bounty.status {
						ChildBountyStatus::Added => {
							// No curator to unassign at this point.
							return Err(BountiesError::<T>::UnexpectedStatus.into());
						},
						ChildBountyStatus::CuratorProposed { ref curator } => {
							// A child-bounty curator has been proposed, but not accepted yet.
							// Either `RejectOrigin`, parent bounty curator or the proposed
							// child-bounty curator can unassign the child-bounty curator.
							ensure!(
								maybe_sender.map_or(true, |sender| {
									sender == *curator ||
										Self::ensure_bounty_active(parent_bounty_id)
											.map_or(false, |(parent_curator, _)| {
												sender == parent_curator
											})
								}),
								BadOrigin
							);
							// Continue to change bounty status below.
						},
						ChildBountyStatus::Active { ref curator } => {
							// The child-bounty is active.
							match maybe_sender {
								// If the `RejectOrigin` is calling this function, slash the curator
								// deposit.
								None => {
									slash_curator(curator, &mut child_bounty.curator_deposit);
									// Continue to change child-bounty status below.
								},
								Some(sender) if sender == *curator => {
									// This is the child-bounty curator, willingly giving up their
									// role. Give back their deposit.
									T::Currency::unreserve(curator, child_bounty.curator_deposit);
									// Reset curator deposit.
									child_bounty.curator_deposit = Zero::zero();
									// Continue to change bounty status below.
								},
								Some(sender) => {
									let (parent_curator, update_due) =
										Self::ensure_bounty_active(parent_bounty_id)?;
									if sender == parent_curator ||
										update_due < Self::treasury_block_number()
									{
										// Slash the child-bounty curator if
										// + the call is made by the parent bounty curator.
										// + or the curator is inactive.
										slash_curator(curator, &mut child_bounty.curator_deposit);
									// Continue to change bounty status below.
									} else {
										// Curator has more time to give an update.
										return Err(BountiesError::<T>::Premature.into());
									}
								},
							}
						},
						ChildBountyStatus::PendingPayout { ref curator, .. } => {
							let (parent_curator, _) = Self::ensure_bounty_active(parent_bounty_id)?;
							ensure!(
								maybe_sender.map_or(true, |sender| parent_curator == sender),
								BadOrigin,
							);
							slash_curator(curator, &mut child_bounty.curator_deposit);
							// Continue to change child-bounty status below.
						},
					};
					// Move the child-bounty state to Added.
					child_bounty.status = ChildBountyStatus::Added;
					Ok(())
				},
			)
		}
```

**File:** substrate/frame/child-bounties/src/lib.rs (L694-763)
```rust
			ChildBounties::<T>::try_mutate_exists(
				parent_bounty_id,
				child_bounty_id,
				|maybe_child_bounty| -> DispatchResult {
					let child_bounty =
						maybe_child_bounty.as_mut().ok_or(BountiesError::<T>::InvalidIndex)?;

					if let ChildBountyStatus::PendingPayout {
						ref curator,
						ref beneficiary,
						ref unlock_at,
					} = child_bounty.status
					{
						// Ensure block number is elapsed for processing the
						// claim.
						ensure!(
							Self::treasury_block_number() >= *unlock_at,
							BountiesError::<T>::Premature,
						);

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
```
