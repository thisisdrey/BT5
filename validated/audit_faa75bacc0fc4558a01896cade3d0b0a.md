The code in the repository confirms every element of this claim.

`propose_curator` in `pallet_child_bounties` only validates the child fee against the child bounty's own `value`, with no check against the parent's `bounty.fee` budget, before accumulating into `ChildrenCuratorFees`: [1](#0-0) 

`claim_bounty` in `pallet_bounties` only guards the invariant via `debug_assert!`, which is stripped in release builds, and then computes `final_fee` and `payout` independently before deleting the bounty record: [2](#0-1) 

`add_child_bounty` only checks the new child value against the parent bounty sub-account's current balance, not against any reserved fee budget, confirming there's no other enforcement point for the fee-vs-fee invariant: [3](#0-2) 

This produces a real, reachable exploit path: an ordinary bounty curator (an unprivileged role obtained through the standard `propose_curator`/`accept_curator` flow, not requiring governance/root at exploit time) can add multiple child bounties and set each child fee close to its own value, driving `ChildrenCuratorFees` above `bounty.fee`. In a release build (the standard production configuration, since `debug-assertions` default to disabled), `final_fee = fee.saturating_sub(children_fee)` collapses to zero while `payout` is computed independently from the original `fee`, and the shortfall equal to `fee` is stranded in `bounty_account_id` at the exact moment `*maybe_bounty = None` deletes the only on-chain reference to that account/bounty_id. This matches the "permanent user-fund lock" impact category, is caused by an insufficient invariant in production code (not by governance/admin misbehavior), and is fully reproducible via a Rust integration test in release mode.

Audit Report

## Title
Unbounded aggregation of child-bounty curator fees permanently strands treasury funds in the parent bounty account - (File: `substrate/frame/bounties/src/lib.rs`)

## Summary
`pallet_child_bounties::propose_curator` only bounds each child-bounty curator fee against that child bounty's own `value`, never against the parent bounty's `fee` budget, allowing the cumulative `ChildrenCuratorFees` to exceed `bounty.fee` by an arbitrary margin. `pallet_bounties::claim_bounty` assumes `children_fee <= fee` but enforces this only via `debug_assert!`, which is compiled out in release builds; when violated, `final_fee` collapses to `0` via `saturating_sub` while `payout` is computed independently from the original `fee`, permanently stranding the shortfall in the deleted bounty's sub-account.

## Finding Description
`propose_curator` for a child bounty checks only `ensure!(fee < child_bounty.value, ...)` before adding to the running total: [1](#0-0) . No code path checks `ChildrenCuratorFees::<T>::get(parent_bounty_id).saturating_add(fee) <= parent_bounty.fee`. `add_child_bounty` only bounds new child values against the parent sub-account's current balance, not against any reserved curator-fee amount: [3](#0-2) . The only place the aggregated child fee is checked against the parent's fee is in `claim_bounty`, via a `debug_assert!` that is a no-op in release/production builds: [4](#0-3) . When the invariant is broken, `final_fee = fee.saturating_sub(children_fee)` becomes `0`, but `payout = balance.saturating_sub(fee)` still uses the original, un-adjusted `fee`, so the sum of the two transfers (`final_fee + payout`) is short by `fee` relative to the account's actual balance. Immediately after, `*maybe_bounty = None` deletes the bounty record, removing the only on-chain path that could reference `bounty_account_id(bounty_id)` again.

## Impact Explanation
This causes a permanent lock of treasury-derived funds with no recovery path, since the bounty index is deleted at the same moment the shortfall occurs and bounty indices are never reused. This matches the accepted "permanent user-fund or bridge-state lock" impact category, and the root cause is a missing runtime invariant check (not governance/admin misbehavior).

## Likelihood Explanation
Exploitable by any account holding the curator role for a bounty (obtained through the standard, unprivileged `propose_curator`/`accept_curator` extrinsic flow), using only public extrinsics `add_child_bounty`, `propose_curator`, `accept_curator`, `award_bounty`, and `claim_bounty`, bounded only by `MaxActiveChildBountyCount`. The condition requires a release/production build (standard for deployed chains) so that `debug_assert!` is stripped.

## Recommendation
Replace the `debug_assert!(children_fee <= fee)` in `claim_bounty` with a runtime-enforced check that clamps `payout` and `final_fee` correctly (or errors out), and additionally enforce at `propose_curator` (child-bounties) time that `ChildrenCuratorFees::<T>::get(parent_bounty_id).saturating_add(fee) <= parent_bounty.fee`.

## Proof of Concept
1. Create/fund a parent bounty with `value = 1000`, `bounty.fee = 100` via `propose_curator`/`accept_curator`.
2. As curator, call `add_child_bounty` multiple times (e.g., 5x, `value = 150` each) — passes since it only checks against current sub-account balance.
3. For each child, call `propose_curator` with `fee` just under its own value (e.g., `140`), passing `ensure!(fee < child_bounty.value, ...)` each time; have curators `accept_curator`.
4. `ChildrenCuratorFees` accumulates to `700`, exceeding parent `bounty.fee = 100`.
5. `award_bounty` then `claim_bounty` in a release build: `debug_assert!` is skipped, `final_fee = 100.saturating_sub(700) = 0`, `payout = balance - 100` (unaffected). Transferred sum is `100` short of the account's actual balance; the bounty record is deleted, permanently stranding that `100`.

### Citations

**File:** substrate/frame/child-bounties/src/lib.rs (L298-312)
```rust
			// Read parent bounty account info.
			let parent_bounty_account =
				pallet_bounties::Pallet::<T>::bounty_account_id(parent_bounty_id);

			// Ensure parent bounty has enough balance after adding child-bounty.
			let bounty_balance = T::Currency::free_balance(&parent_bounty_account);
			let new_bounty_balance = bounty_balance
				.checked_sub(&value)
				.ok_or(Error::<T>::InsufficientBountyBalance)?;
			T::Currency::ensure_can_withdraw(
				&parent_bounty_account,
				value,
				WithdrawReasons::TRANSFER,
				new_bounty_balance,
			)?;
```

**File:** substrate/frame/child-bounties/src/lib.rs (L383-391)
```rust
					// Ensure child-bounty curator fee is less than child-bounty value.
					ensure!(fee < child_bounty.value, BountiesError::<T>::InvalidFee);

					// Add child-bounty curator fee to the cumulative sum. To be
					// subtracted from the parent bounty curator when claiming
					// bounty.
					ChildrenCuratorFees::<T>::mutate(parent_bounty_id, |value| {
						*value = value.saturating_add(fee)
					});
```

**File:** substrate/frame/bounties/src/lib.rs (L807-831)
```rust
					ensure!(Self::treasury_block_number() >= unlock_at, Error::<T, I>::Premature);
					let bounty_account = Self::bounty_account_id(bounty_id);
					let balance = T::Currency::free_balance(&bounty_account);
					let fee = bounty.fee.min(balance); // just to be safe
					let payout = balance.saturating_sub(fee);
					let err_amount = T::Currency::unreserve(&curator, bounty.curator_deposit);
					debug_assert!(err_amount.is_zero());

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

					*maybe_bounty = None;

					BountyDescriptions::<T, I>::remove(bounty_id);
					T::ChildBountyManager::bounty_removed(bounty_id);
```
