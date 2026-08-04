## Title
Unchecked `debug_assert!`-guarded payout transfers in `claim_bounty` / `claim_child_bounty` permanently strand treasury funds when the transfer fails - ([File: substrate/frame/bounties/src/lib.rs], [File: substrate/frame/child-bounties/src/lib.rs])

## Summary
The reNFT report's core broken invariant is: *a mandatory value transfer is bundled with an irreversible state transition (releasing the escrowed asset), and if that transfer fails the state transition either can never complete (funds/asset permanently stuck) or — worse — the state is advanced anyway and the failure is silently swallowed, causing permanent loss of the payout*. The local analog is `pallet-bounties::claim_bounty` and `pallet-child-bounties::claim_child_bounty`: both unconditionally delete the bounty record from storage while relying on `debug_assert!(res.is_ok())` to "check" the outcome of the beneficiary/curator payout transfer. `debug_assert!` compiles to a no-op in a release build, so any real-world transfer failure is silently ignored and the funds are abandoned in the (now untracked) bounty sub-account.

## Finding Description
`Bounties::claim_bounty` computes `final_fee` and `payout`, performs two `T::Currency::transfer(...)` calls, and unconditionally sets `*maybe_bounty = None` regardless of whether those transfers succeeded: [1](#0-0) 

The same unconditional-clear-after-transfer pattern exists in `ChildBounties::claim_child_bounty`: [2](#0-1) 

Both rely on comments such as "should not fail" and `debug_assert!(res.is_ok())` as the only verification of the transfer's outcome. `debug_assert!` is stripped out entirely unless `debug_assertions` is enabled at compile time; production runtime wasm blobs are ordinarily built without `debug_assertions` (no workspace-level override was found forcing it on for these pallets), so in a real deployment a failed transfer is **not detected, not retried, and not reported** — execution simply continues as if it succeeded.

This mirrors the reNFT bug precisely:
- In reNFT, `settlePayment()` uses a reverting `_safeTransfer()`, so the entire `stopRent()` transaction (including releasing the NFT) reverts — permanently blocking the state transition as long as the blocklist condition holds.
- In `pallet-bounties`/`pallet-child-bounties`, the transfer can fail for reasons outside the caller's control (e.g., the beneficiary or curator account does not yet exist and `payout`/`final_fee` is below `ExistentialDeposit`, so a deposit-creating `AllowDeath` transfer returns `Err(TokenError::BelowMinimum/CannotCreate)`), but instead of blocking the state transition, **the pallet advances the state anyway** (removes the bounty entry, unreserves the curator deposit, emits `BountyClaimed`/`Claimed` events claiming success) while the actual value transfer never happened. The residual balance sits in `bounty_account_id(...)` / `child_bounty_account_id(...)`, which after the storage entry is removed has no on-chain record pointing back to it — there is no retry path, no alternate claim function, and no governance-visible marker analogous to `pallet-treasury`'s `PaymentState::Failed` (which the treasury pallet correctly implements for exactly this class of failure, see `check_status`/`PaymentFailed`).

Unlike `pallet-treasury::payout`, which explicitly tracks `PaymentState::{Pending, Attempted, Failed}` and lets a caller retry via `check_status`, the bounties pallets have no analogous safety net — they assume the transfer can never fail and treat that assumption as strong enough to justify irreversibly clearing state before/without confirming success.

## Impact Explanation
An unprivileged party (the assigned curator or the beneficiary named by `award_bounty`, both of whom can be arbitrary accounts including brand-new/never-funded addresses) can trigger `claim_bounty`/`claim_child_bounty` with a `fee`/`payout` split small enough that the resulting transfer to a not-yet-existing account is below `ExistentialDeposit`. This silently zeroes out the on-chain bookkeeping for the bounty/child-bounty while stranding the actual DOT/KSM value in an orphaned sub-account address that no dispatchable can reach anymore (the `Bounties`/`ChildBounties` storage map no longer has any entry to re-derive a legitimate claim from). This is a permanent fund lock/loss matching the "permanent user-fund... lock" and "payout state must only advance after ... settlement succeed atomically" pivots in the impact gate.

## Likelihood Explanation
Triggering the ExistentialDeposit edge case only requires choosing a `fee` (in `propose_curator`) and a `beneficiary` (in `award_bounty`) such that `payout = balance - fee < ExistentialDeposit`, and having that beneficiary/curator account be non-existent at claim time — entirely achievable by a curator/proposer acting alone, with no admin, governance, or malicious-peer assumption required. The `claim_bounty`/`claim_child_bounty` calls themselves are permissionless ("anyone can trigger claim"), so exploitation needs no privileged role.

## Recommendation
Replace the `debug_assert!(res.is_ok())` "should not fail" pattern in `claim_bounty` and `claim_child_bounty` with an explicit `ensure!`/early-return on transfer failure (propagating the error and aborting the `try_mutate_exists`), so the bounty record is only cleared once both transfers have actually succeeded — mirroring the `PaymentState`-based retry design already used in `pallet-treasury`.

## Proof of Concept
Not independently executed against this repo (index/tooling access is read-only); a concrete PoC would be a unit test analogous to `substrate/frame/bounties/src/tests.rs::award_and_claim_bounty_works` at [3](#0-2) , modified so that:
1. `award_bounty` names a **fresh, never-funded** `beneficiary` account.
2. `fee`/bounty `value` are set so `payout = balance - fee` is below the test runtime's `ExistentialDeposit`.
3. Call `claim_bounty`; assert it returns `Ok(())` and emits `BountyClaimed`, while `Balances::free_balance(beneficiary) == 0` and `pallet_bounties::Bounties::<Test>::get(bounty_id) == None`, and the residual balance still sits in `Bounties::bounty_account_id(bounty_id)` with no way to reclaim it.

**Uncertainty note:** I could not verify from the indexed files whether the production runtime wasm builds in this repo explicitly force `debug-assertions = true` for pallet code (only a top-level `Cargo.toml` snippet was inspected; deeper build-profile configuration for individual runtime crates was not fully explored). If `debug_assertions` were force-enabled in the actual production wasm build profile, the `debug_assert!` would panic instead of being a no-op, which would turn this into a differently-shaped issue (extrinsic failure via panic rather than a silent, false-success state advance) — but even in that case, the underlying invariant violation (asset release being conditioned on an unretriable transfer with no `PaymentState`-style recovery path) still stands as the exploitable analog to the reNFT report.

### Citations

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

**File:** substrate/frame/bounties/src/tests.rs (L825-887)
```rust
#[test]
fn award_and_claim_bounty_works() {
	ExtBuilder::default().build_and_execute(|| {
		Balances::make_free_balance_be(&Treasury::account_id(), 101);
		Balances::make_free_balance_be(&4, 10);
		assert_ok!(Bounties::propose_bounty(RuntimeOrigin::signed(0), 50, b"12345".to_vec()));

		assert_ok!(Bounties::approve_bounty(RuntimeOrigin::root(), 0));

		go_to_block(2);

		let fee = 4;
		assert_ok!(Bounties::propose_curator(RuntimeOrigin::root(), 0, 4, fee));
		assert_ok!(Bounties::accept_curator(RuntimeOrigin::signed(4), 0));

		let expected_deposit = Bounties::calculate_curator_deposit(&fee);
		assert_eq!(Balances::free_balance(4), 10 - expected_deposit);

		assert_noop!(
			Bounties::award_bounty(RuntimeOrigin::signed(1), 0, 3),
			Error::<Test>::RequireCurator
		);

		assert_ok!(Bounties::award_bounty(RuntimeOrigin::signed(4), 0, 3));

		assert_eq!(
			pallet_bounties::Bounties::<Test>::get(0).unwrap(),
			Bounty {
				proposer: 0,
				fee,
				curator_deposit: expected_deposit,
				value: 50,
				bond: 85,
				status: BountyStatus::PendingPayout { curator: 4, beneficiary: 3, unlock_at: 5 },
			}
		);

		assert_noop!(Bounties::claim_bounty(RuntimeOrigin::signed(1), 0), Error::<Test>::Premature);

		go_to_block(5);

		assert_ok!(Balances::transfer_allow_death(
			RuntimeOrigin::signed(0),
			Bounties::bounty_account_id(0),
			10
		));

		assert_ok!(Bounties::claim_bounty(RuntimeOrigin::signed(1), 0));

		assert_eq!(
			last_event(),
			BountiesEvent::BountyClaimed { index: 0, payout: 56, beneficiary: 3 }
		);

		assert_eq!(Balances::free_balance(4), 14); // initial 10 + fee 4

		assert_eq!(Balances::free_balance(3), 56);
		assert_eq!(Balances::free_balance(Bounties::bounty_account_id(0)), 0);

		assert_eq!(pallet_bounties::Bounties::<Test>::get(0), None);
		assert_eq!(pallet_bounties::BountyDescriptions::<Test>::get(0), None);
	});
}
```
