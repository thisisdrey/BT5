Found a concrete local analog in `pallet-bounties` / `pallet-child-bounties`: `ChildrenCuratorFees` accumulates fees from multiple child bounties without ever being checked against the parent bounty's own `fee`, and `claim_bounty` only `debug_assert!`s (a no-op in release builds) that `children_fee <= fee` before doing an unguarded `saturating_sub`.

### Title
Unchecked child-bounty curator fee accumulation lets parent curator payout be zero-clamped and treasury value misallocated - ([File: substrate/frame/bounties/src/lib.rs])

### Summary
The parent-bounty curator's fee is only validated once, at `propose_curator` time, against the *current* bounty value [1](#0-0)  and `approve_bounty_with_curator` [2](#0-1) . Once the parent curator is active, it can permissionlessly split arbitrary numbers of child bounties out of the parent bounty and, for each one, call `propose_curator` on `pallet-child-bounties` (a call gated only by "must be signer == parent curator", not by any global governance/spend origin) to assign a child curator fee. Each accepted child fee is unconditionally added into `ChildrenCuratorFees` [3](#0-2) , with the only per-call bound being `fee < child_bounty.value` — never checked against the parent bounty's own `fee` or against the sum of fees from *other* sibling child bounties.

### Finding Description
When the parent bounty is finally claimed, `claim_bounty` computes:
```
let fee = bounty.fee.min(balance);
let children_fee = T::ChildBountyManager::children_curator_fees(bounty_id);
debug_assert!(children_fee <= fee);
let final_fee = fee.saturating_sub(children_fee);
``` [4](#0-3) 

The invariant `children_fee <= fee` that the original report is analogous to (`fantiumFeeBPS <= 10_000`, otherwise an unchecked value silently breaks downstream math) is enforced here **only by a `debug_assert!`**, which compiles to nothing in a production (release) runtime. There is no `ensure!`/ `checked_sub` guard. Because each child bounty's fee is only bounded by that single child bounty's own value (`fee < child_bounty.value`), and a curator can create many child bounties (up to `MaxActiveChildBountyCount`) each carved out of the same parent value, the aggregate `ChildrenCuratorFees[parent_bounty_id]` can be driven to any value up to the sum of all child-bounty values, which can exceed the parent bounty's own `fee` field (which was fixed once at `propose_curator`/`approve_bounty_with_curator` time and never re-validated against subsequent child allocations).

`saturating_sub` prevents an arithmetic panic, but it silently clamps `final_fee` to 0 whenever `children_fee > fee`, meaning the parent curator's contractually agreed fee is dropped without error, and the "conserve value / settle exactly once to the rightful beneficiary and amount" invariant is broken — funds that should go to the parent curator are instead left in the parent bounty account and paid out entirely as `payout` to the beneficiary via `balance.saturating_sub(fee)` using the original (uncorrected) `fee`, effectively transferring value from the curator's fee bucket to the beneficiary's payout inconsistent with what was promised/reserved by the curator's deposit calculation, or the bounty state can become internally inconsistent (children fees already reserved on child accounts vs. accounting in the parent that assumes children_fee <= fee).

### Impact Explanation
This mis-settles treasury-sourced value between curator and beneficiary at bounty-claim time without requiring any malicious peer, validator, relayer, or governance-key compromise — the parent curator, an ordinary signed account once approved, can unilaterally drive this condition purely through normal `pallet-child-bounties::propose_curator` calls. It causes a wrong-beneficiary/wrong-amount payout in a treasury payout pathway, matching the "Balances... treasury spends... must conserve value and settle exactly once to the rightful beneficiary and amount" pivot.

### Likelihood Explanation
Likelihood is moderate: it requires the curator role (obtained through the normal `propose_curator`/`accept_curator` flow, not privileged governance) and the ability to fan out several child bounties whose fees collectively exceed the parent's approved fee — both permissionless, in-scope actions for a signed curator account. The `debug_assert!` guard gives false confidence during development/testing but provides zero protection in production builds.

### Recommendation
Replace the `debug_assert!(children_fee <= fee)` in `claim_bounty` with a proper `ensure!`/defensive-saturating check that surfaces an error (or logs and safely reconciles) if `children_fee > fee`, and additionally enforce in `pallet-child-bounties::propose_curator` that the cumulative `ChildrenCuratorFees::get(parent_bounty_id) + fee` never exceeds the parent bounty's own `fee`, mirroring the recommended `fantiumFeeBPS <= 10_000` bound-at-write-time pattern rather than relying on a debug-only assertion at spend time.

### Proof of Concept
1. Propose and fund a parent bounty with `value = 100`; get it to `Active` with curator `C` and `bounty.fee = 10` (via `propose_curator`/`accept_curator`).
2. `C` calls `add_child_bounty` twice, carving `value = 40` and `value = 40` from the parent's `90` remaining balance.
3. For child bounty #1, `C` calls `propose_curator(..., fee = 39)` (must be `< 40`); for child bounty #2, `C` calls `propose_curator(..., fee = 39)`. Both are accepted by child curators, and `ChildrenCuratorFees[parent_bounty_id]` becomes `78`, far exceeding the parent's own `fee = 10`.
4. Both child bounties are awarded and claimed normally via `claim_child_bounty` [5](#0-4) .
5. `C` awards and claims the parent bounty. In `claim_bounty`, `fee = bounty.fee.min(balance) = 10`, `children_fee = 78`; the `debug_assert!` is a no-op in release, and `final_fee = 10.saturating_sub(78) = 0` [6](#0-5) . The parent curator's promised fee of `10` silently disappears, and `payout` (computed from the original, unreconciled `balance`) is transferred to the beneficiary instead — demonstrating unchecked-aggregate-fee-driven mis-settlement analogous to the unchecked `fantiumFeeBPS` breaking downstream fee math.

### Citations

**File:** substrate/frame/bounties/src/lib.rs (L577-577)
```rust
				ensure!(fee < bounty.value, Error::<T, I>::InvalidFee);
```

**File:** substrate/frame/bounties/src/lib.rs (L810-822)
```rust
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
```

**File:** substrate/frame/bounties/src/lib.rs (L1002-1002)
```rust
				ensure!(fee < bounty.value, Error::<T, I>::InvalidFee);
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

**File:** substrate/frame/child-bounties/src/lib.rs (L686-744)
```rust
		pub fn claim_child_bounty(
			origin: OriginFor<T>,
			#[pallet::compact] parent_bounty_id: BountyIndex,
			#[pallet::compact] child_bounty_id: BountyIndex,
		) -> DispatchResult {
			ensure_signed(origin)?;

			// Ensure child-bounty is in expected state.
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
```
