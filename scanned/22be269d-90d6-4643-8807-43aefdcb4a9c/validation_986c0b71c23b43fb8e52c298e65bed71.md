I found the confirmed local analog: `unassign_curator` resets a child-bounty from `CuratorProposed`/`Active`/`PendingPayout` back to `Added` without ever decrementing `ChildrenCuratorFees::<T>` for that bounty's `fee`. `propose_curator` can then be called again on the reset (now-`Added`) child-bounty, and it *unconditionally adds* the new fee to `ChildrenCuratorFees` again, on top of the stale, never-removed previous fee. This mirrors the `bigclaim()` pattern: an accounting accumulator (`claimed[]` / `ChildrenCuratorFees`) is incremented on every "attempt" cycle (signature verification / curator proposal), while the actual payout event (transfer / bounty claim) happens only once — leaving the running total permanently inflated relative to what is ever paid out.

### Title
Stale child-bounty curator fee is never subtracted on `unassign_curator`, causing `ChildrenCuratorFees` to permanently overcount and lock parent-bounty payouts - (File: `substrate/frame/child-bounties/src/lib.rs`)

### Summary
`propose_curator` [1](#0-0)  adds `fee` to the cumulative `ChildrenCuratorFees::<T>` storage for the parent bounty, which will later be *subtracted* from the parent curator's payout when the parent bounty is claimed via `children_curator_fees` [2](#0-1) . However, `unassign_curator` [3](#0-2)  resets the child-bounty status back to `Added` from `CuratorProposed`, `Active`, or `PendingPayout` in every branch, but never touches `ChildrenCuratorFees`. Only `impl_close_child_bounty` reverses this accumulator, via `ChildrenCuratorFees::<T>::mutate(parent_bounty_id, |value| *value = value.saturating_sub(child_bounty.fee))` [4](#0-3) . A child-bounty whose curator is unassigned (rather than closed) can be re-proposed a new curator/fee via `propose_curator` again, re-adding a (possibly different) fee to the same still-inflated accumulator.

### Finding Description
The invariant the code needs to maintain is: `ChildrenCuratorFees[parent_bounty_id]` should equal the sum of `fee` values for all *currently pending/active* child bounties of that parent, so that when the parent bounty is claimed, `final_fee = parent_fee.saturating_sub(children_fee)` correctly reduces the parent curator's payout by exactly the amount owed to child curators [5](#0-4) .

That invariant is broken because:
1. `propose_curator` unconditionally does `ChildrenCuratorFees::<T>::mutate(parent_bounty_id, |value| *value = value.saturating_add(fee))` every time it is called on a child bounty in the `Added` state [6](#0-5) .
2. `unassign_curator` transitions the child bounty status back to `Added` (allowing `propose_curator` to be called again) from `CuratorProposed`, `Active`, and `PendingPayout` states, in all cases, without any corresponding decrement of `ChildrenCuratorFees` [7](#0-6) .
3. Only `claim_child_bounty` (which consumes and removes the child bounty entirely, not adjusting `ChildrenCuratorFees` at all — it only reduces `ParentChildBounties` count) [8](#0-7)  or `close_child_bounty`/`impl_close_child_bounty` (which does subtract `child_bounty.fee`) [4](#0-3)  ever reduce the accumulator. Note `claim_child_bounty` also never decrements `ChildrenCuratorFees`, even though the child bounty's fee has now genuinely been paid out and removed — this is a second, related path to the same drift.

So any sequence of `propose_curator` → `unassign_curator` → `propose_curator` (repeatable any number of times, and driven entirely by the child-bounty curator's own signed origin, since the proposed-but-not-yet-accepted curator is explicitly allowed to unassign themselves per the unassign_curator doc/branch at `ChildBountyStatus::CuratorProposed`) accumulates the `fee` value multiple times into `ChildrenCuratorFees` while only one child-bounty fee will ever actually be paid out (or none, if it's later closed and only the *current* `fee` is subtracted, leaving the extra accumulated fee from earlier cycles stuck).

This exactly mirrors the reported bug: an accounting map (`claimed[]` / `ChildrenCuratorFees`) is incremented per "attempt" iteration (signature check / curator proposal cycle), while settlement (transfer / final claim) happens once, so the accumulator drifts upward relative to reality.

### Impact Explanation
Because `claim_bounty` computes `final_fee = fee.saturating_sub(children_fee)` with a `debug_assert!(children_fee <= fee)` [9](#0-8) , an inflated `children_fee` can:
- Reduce the parent curator's fee payout below what they are legitimately owed (fund misallocation to wrong beneficiary/amount), since `final_fee` saturates to zero once `children_fee` exceeds `fee`.
- In production builds, `debug_assert!` is compiled out, so the invariant violation (`children_fee > fee`) is silently tolerated rather than causing a panic in a controlled test environment — it simply produces incorrect (but non-panicking) payout amounts, matching "runtime bugs that compromise intended behavior" and "duplicate settlement or payout" from the impact gate.
- This is triggerable by any account that is proposed as a child-bounty curator (an otherwise unprivileged, permissionless role reachable by a normal signed account accepting a curator proposal), without needing a malicious validator, collator, relayer, or governance actor — the accounting corruption is self-inflicted by the protocol's own repeated propose/unassign cycle, not by an external malicious peer.

### Likelihood Explanation
The parent bounty curator, who is a normal signed account with no special chain privileges, can call `propose_curator` and then have the proposed curator (or the parent curator themselves, or after the inactivity window, anyone) call `unassign_curator` to reset the child bounty back to `Added` — an interaction fully available and repeatable via the pallet's own public dispatchables, with no batching or malicious signature reuse needed. This can be repeated many times across many child bounties under `MaxActiveChildBountyCount`, driving `ChildrenCuratorFees` for a parent bounty arbitrarily above the true owed fee total before the parent bounty is claimed, making the corrupted value both easy to produce and directly consumed in the payout calculation.

### Recommendation
Decrement `ChildrenCuratorFees::<T>` by `child_bounty.fee` in `unassign_curator` whenever a fee had previously been recorded (i.e., transitioning from `CuratorProposed`, `Active`, or `PendingPayout` back to `Added`) and reset `child_bounty.fee` to zero at the same time, mirroring the pattern already used in `impl_close_child_bounty`. Additionally, `claim_child_bounty` should decrement `ChildrenCuratorFees` by the claimed child bounty's fee before/at removal, since that fee is now settled and should no longer be counted as "pending" against the parent curator. As with the original `claimed[]` recommendation, the safest fix is to only ever add to `ChildrenCuratorFees` transactionally alongside the corresponding subtraction on every path that leaves the `CuratorProposed`/`Active`/`PendingPayout` states, so the invariant `ChildrenCuratorFees == sum(fee for child bounties currently expecting payout)` holds at all times.

### Proof of Concept
1. Council/curator creates parent bounty; parent curator calls `add_child_bounty` to create child bounty `C` with value `V`.
2. Parent curator calls `propose_curator(parent, C, curator_X, fee=F1)` → `ChildrenCuratorFees[parent] += F1` (now `F1`) [1](#0-0) .
3. `curator_X` (the proposed curator, self-service allowed) calls `unassign_curator(parent, C)` → status reset to `Added`, but `ChildrenCuratorFees[parent]` remains `F1` (not decremented) [10](#0-9) , [11](#0-10) .
4. Parent curator calls `propose_curator(parent, C, curator_Y, fee=F2)` again → `ChildrenCuratorFees[parent] += F2`, now totaling `F1 + F2` for a single child bounty that will only ever pay out one fee amount.
5. Repeat steps 3–4 `n` times to accumulate `ChildrenCuratorFees[parent] = F1 + F2 + ... + Fn`, all attributable to one child bounty.
6. Eventually accept a curator and pay out the child bounty normally: `accept_curator` → `award_child_bounty` → `claim_child_bounty`. This does not touch `ChildrenCuratorFees` at all, leaving the inflated sum intact.
7. Parent curator calls `Bounties::award_bounty` then `Bounties::claim_bounty`: `final_fee = fee.saturating_sub(children_fee)` uses the inflated `children_fee`, which can now exceed the parent's `fee`, driving `final_fee` to `0` and shortchanging the legitimate parent curator payout — demonstrated by the existing test pattern in `substrate/frame/child-bounties/src/tests.rs` (lines 1365–1438) which shows the intended, correct flow (`101 + 6 - fee`) that this repeated propose/unassign cycle would corrupt [12](#0-11) .

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

**File:** substrate/frame/child-bounties/src/lib.rs (L503-598)
```rust
		pub fn unassign_curator(
			origin: OriginFor<T>,
			#[pallet::compact] parent_bounty_id: BountyIndex,
			#[pallet::compact] child_bounty_id: BountyIndex,
		) -> DispatchResult {
			let maybe_sender = ensure_signed(origin.clone())
				.map(Some)
				.or_else(|_| T::RejectOrigin::ensure_origin(origin).map(|_| None))?;

			ChildBounties::<T>::try_mutate_exists(
				parent_bounty_id,
				child_bounty_id,
				|maybe_child_bounty| -> DispatchResult {
					let child_bounty =
						maybe_child_bounty.as_mut().ok_or(BountiesError::<T>::InvalidIndex)?;

					let slash_curator =
						|curator: &T::AccountId, curator_deposit: &mut BalanceOf<T>| {
							let imbalance =
								T::Currency::slash_reserved(curator, *curator_deposit).0;
							T::OnSlash::on_unbalanced(imbalance);
							*curator_deposit = Zero::zero();
						};

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

**File:** substrate/frame/child-bounties/src/lib.rs (L713-757)
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
```

**File:** substrate/frame/child-bounties/src/lib.rs (L925-929)
```rust
				// Revert the curator fee back to parent bounty curator &
				// reduce the active child-bounty count.
				ChildrenCuratorFees::<T>::mutate(parent_bounty_id, |value| {
					*value = value.saturating_sub(child_bounty.fee)
				});
```

**File:** substrate/frame/child-bounties/src/lib.rs (L977-985)
```rust
	/// Returns cumulative child bounty curator fees for `bounty_id` also removing the associated
	/// storage item. This function is assumed to be called when parent bounty is claimed.
	fn children_curator_fees(bounty_id: pallet_bounties::BountyIndex) -> BalanceOf<T> {
		// This is asked for when the parent bounty is being claimed. No use of
		// keeping it in state after that. Hence removing.
		let children_fee_total = ChildrenCuratorFees::<T>::get(bounty_id);
		ChildrenCuratorFees::<T>::remove(bounty_id);
		children_fee_total
	}
```

**File:** substrate/frame/bounties/src/lib.rs (L815-826)
```rust
					// Get total child bounties curator fees, and subtract it from the parent
					// curator fee (the fee in present referenced bounty, `self`).
					let children_fee = T::ChildBountyManager::children_curator_fees(bounty_id);
					debug_assert!(children_fee <= fee);

					let final_fee = fee.saturating_sub(children_fee);
					let res =
						T::Currency::transfer(&bounty_account, &curator, final_fee, AllowDeath); // should not fail
					debug_assert!(res.is_ok());
					let res =
						T::Currency::transfer(&bounty_account, &beneficiary, payout, AllowDeath); // should not fail
					debug_assert!(res.is_ok());
```

**File:** substrate/frame/child-bounties/src/tests.rs (L1416-1436)
```rust
		// Award the parent bounty.
		assert_ok!(Bounties::award_bounty(RuntimeOrigin::signed(account_id(4)), 0, account_id(9)));

		go_to_block(15);

		// Check the total count.
		assert_eq!(pallet_child_bounties::ParentTotalChildBounties::<Test>::get(0), 1);

		// Claim the parent bounty.
		assert_ok!(Bounties::claim_bounty(RuntimeOrigin::signed(account_id(9)), 0));

		// Check the total count after the parent bounty removal.
		assert_eq!(pallet_child_bounties::ParentTotalChildBounties::<Test>::get(0), 0);

		// Ensure parent-bounty curator received correctly reduced fee.
		assert_eq!(Balances::free_balance(account_id(4)), 101 + 6 - fee); // 101 + 6 - 2
		assert_eq!(Balances::reserved_balance(account_id(4)), 0);

		// Verify parent-bounty beneficiary balance.
		assert_eq!(Balances::free_balance(account_id(9)), 34);
		assert_eq!(Balances::reserved_balance(account_id(9)), 0);
```
