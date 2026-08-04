### Title
`claim_child_bounty` ignores failed `Currency::transfer` results, permanently locking bounty funds - ([File: substrate/frame/child-bounties/src/lib.rs])

### Summary
`pallet-child-bounties::claim_child_bounty` pays the curator fee and the beneficiary payout via `T::Currency::transfer(..., AllowDeath)`, but only checks the outcome with `debug_assert!`, which compiles to a no-op in production (release) runtime builds. The child-bounty curator freely picks the `beneficiary` address in `award_child_bounty` with no existence/ExistentialDeposit validation. If the resulting `payout` transfer to a fresh/zero-balance `beneficiary` account is below the chain's Existential Deposit, `Currency::transfer` returns an error (the new account cannot be created below ED) — yet the code proceeds to delete the child-bounty record and emit a `Claimed` event as if the payment succeeded, permanently orphaning the funds still sitting in the `child_bounty_account`.

### Finding Description
In `award_child_bounty` [1](#0-0) , any account holding the child-bounty curator role (or the parent curator) supplies an arbitrary `beneficiary: AccountIdLookupOf<T>` with zero validation of whether that account exists or can receive the eventual payout amount.

In `claim_child_bounty`, which is callable by **any signed origin** [2](#0-1) , the payout logic computes `curator_fee` and `payout = balance - curator_fee`, then performs two transfers whose results are discarded via `debug_assert!`: [3](#0-2) 

`debug_assert!` is stripped out unless the runtime is compiled with `debug-assertions` enabled, which is not the case for production Substrate-based chain WASM/native runtimes. Consequently, if `T::Currency::transfer(&child_bounty_account, beneficiary, payout, AllowDeath)` fails (the classic case: `beneficiary` has no existing account and `payout` is below `ExistentialDeposit`, causing the balances pallet to reject creation of a sub-ED account), the error is silently swallowed. Execution continues unconditionally to:
- emit `Event::Claimed { ..., payout, beneficiary, ... }` as if the transfer succeeded,
- decrement `ParentChildBounties`,
- remove `ChildBountyDescriptionsV1`,
- and delete the `ChildBounties` storage entry entirely [4](#0-3) .

Once the child-bounty record is removed, there is no remaining code path referencing `child_bounty_account`, so any balance still held there (the un-transferred `payout`, and potentially the `curator_fee` too if that first transfer also failed) becomes permanently stranded — nothing in the pallet can reach or redistribute it.

This directly mirrors the RubiconMarket analog: a value/address parameter under the control of a non-root role (curator ≈ owner setting `feeTo`) is used, unchecked, in an ERC20/Balances-style transfer whose failure mode (recipient rejects because sub-existence-deposit/zero-value transfer) is not defensively handled, and the surrounding logic proceeds as though the transfer had succeeded.

### Impact Explanation
This is a permanent user-fund lock: bounty treasury funds allocated to a child bounty are lost/unreachable once the record is deleted despite the transfer having failed. It satisfies the "permanent user-fund or bridge-state lock" and "duplicate settlement or payout state advancing without transfer success" impact categories, since the state (`Claimed`/removed) advances without the underlying payment actually completing.

### Likelihood Explanation
- `award_child_bounty` can be called by the child curator or parent curator — a bounty-specific role, not chain governance/root — with a completely attacker-chosen `beneficiary` and no restriction preventing a fresh account.
- `claim_child_bounty` is callable by any signed account once `unlock_at` has passed, so a curator (possibly a griever, or simply careless) triggering an award to a never-funded fresh account with a payout smaller than ED will hit this path deterministically; no relayer/validator/collator/node compromise or leaked keys are required.
- The bug requires no race condition and is 100% reproducible given: `payout < ExistentialDeposit` and `beneficiary` account has zero prior existence/balance.

### Recommendation
Do not use `debug_assert!` on the result of live currency transfers. Propagate transfer errors with `?` (or explicitly handle failure by keeping the child-bounty in a retryable "payout failed" state, similar to the `PaymentState::Failed` pattern already used in `pallet-multi-asset-bounties`), and only remove the child-bounty record / emit `Claimed` after confirming both transfers succeeded. Additionally, consider validating that `payout` (and `curator_fee`) will not create a sub-ED account before awarding, or use `KeepAlive`/explicit `Preservation` checks and reject payouts that cannot be delivered.

### Proof of Concept
1. Propose and fund a parent bounty, propose/accept a child bounty with a `fee` set close to the total `value` such that `payout = value - fee` is smaller than `ExistentialDeposit` (e.g., `ExistentialDeposit = 1, value = 1, fee = 1`, so `payout = 0` or a similarly tiny non-zero amount below ED).
2. As curator, call `award_child_bounty(parent_bounty_id, child_bounty_id, beneficiary)` where `beneficiary` is a brand-new AccountId with zero balance and no prior existence in the `System`/`Balances` pallet.
3. Wait for `unlock_at` (the `BountyDepositPayoutDelay`).
4. Any signed account calls `claim_child_bounty(parent_bounty_id, child_bounty_id)`.
5. Internally, `T::Currency::transfer(&child_bounty_account, &beneficiary, payout, AllowDeath)` returns `Err(_)` because creating `beneficiary`'s account with a balance below `ExistentialDeposit` is disallowed by `pallet-balances`.
6. `debug_assert!(payout_transfer_result.is_ok())` is a no-op in the release runtime, so no panic occurs.
7. Execution proceeds: `Event::Claimed` is emitted, `ChildBounties` entry is removed, `ParentChildBounties` decremented.
8. `child_bounty_account` still holds the un-transferred balance permanently, with no further pallet call able to recover or reference it (confirmable by checking `Balances::free_balance(child_bounty_account)` remains non-zero after the claim while the storage entry is gone).

### Citations

**File:** substrate/frame/child-bounties/src/lib.rs (L619-663)
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
					} else {
						Err(BountiesError::<T>::UnexpectedStatus.into())
					}
				},
			)?;

			// Trigger the event Awarded.
			Self::deposit_event(Event::<T>::Awarded {
				index: parent_bounty_id,
				child_index: child_bounty_id,
				beneficiary,
			});
```

**File:** substrate/frame/child-bounties/src/lib.rs (L686-691)
```rust
		pub fn claim_child_bounty(
			origin: OriginFor<T>,
			#[pallet::compact] parent_bounty_id: BountyIndex,
			#[pallet::compact] child_bounty_id: BountyIndex,
		) -> DispatchResult {
			ensure_signed(origin)?;
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
