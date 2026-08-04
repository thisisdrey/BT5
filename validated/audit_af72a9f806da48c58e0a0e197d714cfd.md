### Title
`claim_bounty()`/`claim_child_bounty()` finalize payout state even when the curator-fee transfer fails, permanently losing the fee - (File: `substrate/frame/bounties/src/lib.rs`, `substrate/frame/child-bounties/src/lib.rs`)

### Summary
`pallet-bounties::claim_bounty` and `pallet-child-bounties::claim_child_bounty` transfer the curator fee and the beneficiary payout using `T::Currency::transfer(...)`, but the `Result` of each transfer is only checked with `debug_assert!(res.is_ok())` instead of being propagated with `?`. `debug_assert!` compiles to a no-op whenever `cfg!(debug_assertions)` is false, which is the case for release-mode runtime builds used in production. If the curator-fee transfer fails, the function still deletes the bounty/child-bounty storage entry and emits `BountyClaimed`/`Claimed` as if the payout succeeded, permanently losing the curator's fee — the same broken invariant as the reported `claimERC20Prize()` bug: state is advanced to "claimed" without confirming the underlying token/balance movement actually happened.

### Finding Description
In `claim_bounty`:
```rust
let res = T::Currency::transfer(&bounty_account, &curator, final_fee, AllowDeath); // should not fail
debug_assert!(res.is_ok());
let res = T::Currency::transfer(&bounty_account, &beneficiary, payout, AllowDeath); // should not fail
debug_assert!(res.is_ok());

*maybe_bounty = None;
BountyDescriptions::<T, I>::remove(bounty_id);
...
Self::deposit_event(Event::<T, I>::BountyClaimed { index: bounty_id, payout, beneficiary });
Ok(())
``` [1](#0-0) 

The identical pattern exists for the fee and payout transfers in `claim_child_bounty`: [2](#0-1) 

`T::Currency::transfer` is the legacy `frame_support::traits::Currency` trait, whose `transfer` returns a `DispatchResult` and can genuinely fail — most notably, sending a positive amount below the chain's `ExistentialDeposit` to an account that does not yet exist fails with an existential-deposit error (this is standard, well-known `pallet-balances` behavior; see the ED-centric logic in `substrate/frame/balances/src/impl_currency.rs`). The curator `final_fee` is derived from a bounty fee value set earlier by governance/curator assignment and can legitimately be smaller than the ED, and a freshly-designated curator account can easily have zero prior balance.

Because the transfer's `Result` is discarded (only asserted via `debug_assert!`, which is stripped in release/production builds), a failing fee transfer does not abort the extrinsic. Execution proceeds to unconditionally: remove the bounty from storage, remove its description, decrement/clean up child-bounty tracking, and emit the "claimed" event with the full `payout` amount — even though the fee portion never reached the curator. Since the bounty record is deleted (`*maybe_bounty = None` / child bounty removed from `ChildBounties`), there is no remaining state or code path to re-claim the lost fee: it is stranded in the (now-orphaned) bounty sub-account.

This exactly mirrors the reported vulnerability's core defect: a public claim/payout entrypoint updates accounting/state as though a transfer succeeded without verifying the transfer's return value, and the failure mode is silently swallowed rather than causing a revert.

### Impact Explanation
This falls under "treasury or reward payouts" and "duplicate settlement or payout / permanent user-fund lock" in the required impact list. A curator can permanently lose an owed fee with no recovery path once the bounty record is purged, and this can be triggered by unprivileged, ordinary use (any signed account can call `claim_bounty`/`claim_child_bounty`; the fee amount is set through normal bounty curation, not requiring an admin/governance attacker). The loss is a genuine runtime/economic-invariant bug: `debug_assert!` provides zero protection in a release-mode runtime, so the "should not fail" comments are misleading guarantees.

### Likelihood Explanation
Likelihood is bounded by needing a fee amount below `ExistentialDeposit` for a not-yet-existing curator account — a condition that is realistic for bounty proposals with small curator fees on chains with a non-trivial ED, but not guaranteed to occur on every bounty. It requires no adversarial coordination, malicious relayer/validator, or governance abuse — only a normal small-fee bounty and a curator account without prior balance, both of which are ordinary, permissionless states reachable in the standard bounty lifecycle.

### Recommendation
Propagate `T::Currency::transfer` results with `?` instead of `debug_assert!`, or use a safer transfer variant (e.g., `fungible::Mutate::transfer` with `Preservation::Expendable`, or an explicit "keep account alive with dust-handling" strategy) that cannot silently fail while updating bounty state, or handle the below-ED case explicitly (e.g., only transfer if `final_fee` clears the ED for the destination, otherwise round it into the beneficiary payout, or use `mint_into`/`deposit_creating` style fallback). At minimum, the two transfers should occur (or be validated as feasible) before the bounty record is removed and the "claimed" event emitted, and any transfer failure must abort the whole extrinsic via `try_mutate_exists`'s `DispatchResult` (letting storage changes roll back), not merely be asserted away.

### Proof of Concept
1. Chain configured with a non-trivial `ExistentialDeposit` (standard for most production runtimes).
2. Council/`T::SpendOrigin` proposes and funds a bounty; a curator is proposed and accepted with `bounty.fee` set to a small value less than `ExistentialDeposit` (this is fully within governance's normal, non-malicious discretion — no admin abuse needed for the mechanic itself, since the fee is a routine incentive parameter).
3. The designated curator address is fresh (never funded, e.g., a newly generated account chosen for the role) — a common real scenario.
4. Bounty is awarded, unlock delay elapses, and anyone (per `ensure_signed(origin)?; // anyone can trigger claim`) calls `claim_bounty(bounty_id)`. [3](#0-2) 
5. `T::Currency::transfer(&bounty_account, &curator, final_fee, AllowDeath)` fails internally (new account, deposit below ED) and returns `Err(...)`.
6. In a release-mode build (`cfg!(debug_assertions)` false, standard for production runtime WASM), `debug_assert!(res.is_ok())` is compiled out and does nothing.
7. Execution continues: the beneficiary payout transfer proceeds, `*maybe_bounty = None` removes the bounty, `BountyDescriptions` is cleared, and `BountyClaimed` is emitted reporting the full `payout` — while the curator fee remains stuck in the now-orphaned `bounty_account` with no bounty record left to reclaim it through. [1](#0-0) [4](#0-3)

### Citations

**File:** substrate/frame/bounties/src/lib.rs (L796-800)
```rust
		pub fn claim_bounty(
			origin: OriginFor<T>,
			#[pallet::compact] bounty_id: BountyIndex,
		) -> DispatchResult {
			ensure_signed(origin)?; // anyone can trigger claim
```

**File:** substrate/frame/bounties/src/lib.rs (L820-838)
```rust
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

					Self::deposit_event(Event::<T, I>::BountyClaimed {
						index: bounty_id,
						payout,
						beneficiary,
					});
					Ok(())
```

**File:** substrate/frame/child-bounties/src/lib.rs (L714-765)
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
