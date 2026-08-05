Audit Report

## Title
`claim_child_bounty` ignores transfer failure via `debug_assert!`, permanently trapping bounty funds - (File: `substrate/frame/child-bounties/src/lib.rs`)

## Summary
In `claim_child_bounty`, the two payout transfers from `child_bounty_account` to the `curator` and `beneficiary` are checked only with `debug_assert!`, which is compiled out in release builds. Because `Currency::transfer` with `ExistenceRequirement::AllowDeath` errors (`TokenError::BelowMinimum` / `Error::ExistentialDeposit`) when the destination is a new account and the transferred amount is below the Existential Deposit (ED), a silent transfer failure is fully reachable, and the pallet subsequently deletes all child-bounty bookkeeping, permanently orphaning the funds still sitting in `child_bounty_account`.

## Finding Description
`claim_child_bounty` computes `curator_fee` and `payout` and issues two transfers guarded only by `debug_assert!`: [1](#0-0) 

`debug_assert!` is a `cfg(debug_assertions)`-gated no-op in release builds, so any transfer failure is silently ignored while execution proceeds to emit `Event::Claimed`, decrement `ParentChildBounties`, remove `ChildBountyDescriptionsV1`, and set `*maybe_child_bounty = None`: [2](#0-1) 

The transfer failure condition is real and confirmed by the balances pallet: `deposit_creating`/`can_deposit`/dispatchable transfer paths reject creating a new account with a balance below `ExistentialDeposit`, returning `DepositConsequence::BelowMinimum` / `TokenError::BelowMinimum`: [3](#0-2) [4](#0-3) 

The `beneficiary` account in particular is entirely attacker-controllable and unconstrained: it is set by the child-bounty curator/parent curator in `award_child_bounty` with no funding or ED requirement whatsoever: [5](#0-4) 

So a curator can award the child bounty to a brand-new, unfunded `beneficiary` address, then anyone can call `claim_child_bounty` after `unlock_at`. If `payout` (balance minus `curator_fee`) is below ED and `beneficiary` never held a balance, `T::Currency::transfer(&child_bounty_account, beneficiary, payout, AllowDeath)` fails, the `debug_assert!` is a no-op in release, and the pallet still emits `Claimed` and wipes the child-bounty record, deleting the only state that could support a retry or recovery of the leftover balance in `child_bounty_account`.

## Impact Explanation
This matches the "permanent user-fund lock" / "settle exactly once to the rightful beneficiary and amount" impact category. When the payout transfer to a fresh, unfunded beneficiary fails silently, the pallet still fires `Claimed`, decrements `ParentChildBounties`, removes `ChildBountyDescriptionsV1`, and sets the child-bounty entry to `None`. Since no other extrinsic path references `child_bounty_account` once the child-bounty record is gone, the value never reaches the intended beneficiary and cannot be recovered through any subsequent call — a genuine, structural fund-lock bug.

## Likelihood Explanation
The exploit requires only unprivileged, ordinary extrinsic calls: `add_child_bounty`/`propose_curator`/`accept_curator` by the parent curator, `award_child_bounty` naming a fresh never-funded `beneficiary`, and a call to `claim_child_bounty` by any signed account after `unlock_at`. No malicious validator, relayer, or governance action is needed — a curator with normal bounty-management permissions can trigger this deterministically and repeatedly (once per child bounty) in a production (release) build, where `debug_assert!` is compiled out.

## Recommendation
Replace the `debug_assert!` checks with real error propagation, e.g. use `?` on both `T::Currency::transfer` calls so failures abort the `try_mutate_exists` closure and the child-bounty state is not wiped. If atomic best-effort settlement is desired, do not emit `Claimed`, decrement counters, or clear the child-bounty record unless both transfers succeed; alternatively route any residual balance below ED to a sink (e.g., treasury) instead of silently discarding it.

## Proof of Concept
1. Parent curator creates a child bounty with `value` set so that `payout = balance - curator_fee` is nonzero but below the chain's `ExistentialDeposit` (e.g., `payout = 1`, `ED = 1_000_000_000_000`).
2. Parent curator calls `award_child_bounty` naming a `beneficiary` account that has never held a balance (does not exist in `System::Account`).
3. After `unlock_at`, any signed account calls `claim_child_bounty(parent_bounty_id, child_bounty_id)`.
4. `T::Currency::transfer(&child_bounty_account, beneficiary, payout, AllowDeath)` returns `Err(TokenError::BelowMinimum)` because the destination is new and `payout < ED`; in a release build `debug_assert!(payout_transfer_result.is_ok())` is a no-op.
5. `Event::Claimed` is still emitted, `ParentChildBounties` is decremented, `ChildBountyDescriptionsV1` is removed, and `*maybe_child_bounty = None`.
6. `payout` remains stranded in `child_bounty_account`'s free balance with no remaining code path to reclaim it, verifiable via a Rust test asserting `Balances::free_balance(child_bounty_account) > 0` after the claim call returns `Ok(())`.

### Citations

**File:** substrate/frame/child-bounties/src/lib.rs (L619-651)
```rust
		pub fn award_child_bounty(
			origin: OriginFor<T>,
			#[pallet::compact] parent_bounty_id: BountyIndex,
			#[pallet::compact] child_bounty_id: BountyIndex,
			beneficiary: AccountIdLookupOf<T>,
		) -> DispatchResult {
			let signer = ensure_signed(origin)?;
			let beneficiary = T::Lookup::lookup(beneficiary)?;

			// Ensure parent bounty exists, and is active.
			let (parent_curator, _) = Self::ensure_bounty_active(parent_bounty_id)?;

			ChildBounties::<T>::try_mutate_exists(
				parent_bounty_id,
				child_bounty_id,
				|maybe_child_bounty| -> DispatchResult {
					let child_bounty =
						maybe_child_bounty.as_mut().ok_or(BountiesError::<T>::InvalidIndex)?;

					// Ensure child-bounty is in active state.
					if let ChildBountyStatus::Active { ref curator } = child_bounty.status {
						ensure!(
							signer == *curator || signer == parent_curator,
							BountiesError::<T>::RequireCurator,
						);
						// Move the child-bounty state to pending payout.
						child_bounty.status = ChildBountyStatus::PendingPayout {
							curator: signer,
							beneficiary: beneficiary.clone(),
							unlock_at: Self::treasury_block_number() +
								T::BountyDepositPayoutDelay::get(),
						};
						Ok(())
```

**File:** substrate/frame/child-bounties/src/lib.rs (L726-744)
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

**File:** substrate/frame/child-bounties/src/lib.rs (L746-763)
```rust
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

**File:** substrate/frame/balances/src/impl_fungible.rs (L87-92)
```rust
		let account = Self::account(who);
		let new_free = match account.free.checked_add(&amount) {
			None => return DepositConsequence::Overflow,
			Some(x) if x < T::ExistentialDeposit::get() => return DepositConsequence::BelowMinimum,
			Some(x) => x,
		};
```

**File:** substrate/frame/balances/src/tests/dispatchable_tests.rs (L31-45)
```rust
#[test]
fn default_indexing_on_new_accounts_should_not_work2() {
	ExtBuilder::default()
		.existential_deposit(10)
		.monied(true)
		.build_and_execute_with(|| {
			// account 5 should not exist
			// ext_deposit is 10, value is 9, not satisfies for ext_deposit
			assert_noop!(
				Balances::transfer_allow_death(Some(1).into(), 5, 9),
				TokenError::BelowMinimum,
			);
			assert_eq!(Balances::free_balance(1), 100);
		});
}
```
