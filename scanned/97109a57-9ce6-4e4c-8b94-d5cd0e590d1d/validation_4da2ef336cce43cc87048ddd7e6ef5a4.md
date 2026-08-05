## Finding: Bounty and child-bounty payout finalize storage state via `debug_assert!` instead of a real conservation check on transfer success

### Title
Silent transfer failures in `claim_bounty`/`claim_child_bounty` are only guarded by `debug_assert!`, letting bounty state finalize and funds vanish - (File: `substrate/frame/bounties/src/lib.rs`, `substrate/frame/child-bounties/src/lib.rs`)

### Summary
Both `Bounties::claim_bounty` and `ChildBounties::claim_child_bounty` perform the currency transfer that is supposed to pay the curator fee and the beneficiary, then "check" the transfer's success with `debug_assert!(res.is_ok())`. `debug_assert!` is compiled out entirely in release builds, which is what production runtimes use. Regardless of whether the transfer actually succeeded, the code unconditionally proceeds to delete the bounty record, remove its description, decrement counters, and emit a `BountyClaimed`/`Claimed` event as if payment had been made — exactly the "check happens after the point where funds are already considered moved" pattern from the referenced Lido `_withdrawFromYieldPool` bug, except here the "check" never gates execution at all in production.

### Finding Description
In `claim_bounty`: [1](#0-0) 

the flow is:
1. `T::Currency::transfer(&bounty_account, &curator, final_fee, AllowDeath)` — result discarded except for `debug_assert!(res.is_ok())`.
2. `T::Currency::transfer(&bounty_account, &beneficiary, payout, AllowDeath)` — same pattern.
3. `*maybe_bounty = None;` unconditionally deletes the bounty.
4. `BountyDescriptions::remove`, `T::ChildBountyManager::bounty_removed`, and `Self::deposit_event(Event::BountyClaimed { .. })` all execute unconditionally.

The identical structure exists in `claim_child_bounty`: [2](#0-1) 

`debug_assert!` is a no-op in `--release` builds (the build profile used for any real Substrate-based chain runtime), so in production there is effectively **no check at all** — the code path is functionally identical to "return before checking success" from the external report: the state-finalizing writes (`Bounties::insert`/removal, event emission, counter updates) happen regardless of whether `Currency::transfer` returned `Ok` or `Err`.

`claim_bounty` is a public, permissionless extrinsic — "anyone can trigger claim" per its own comment (`ensure_signed(origin)?; // anyone can trigger claim`), so no privileged actor is required to reach this path.

### Impact Explanation
If either transfer fails for any reason (e.g. `ExistentialDeposit`/dust edge cases on the `bounty_account`, a `Currency` implementation that enforces additional withdraw preconditions such as freezes/holds/locks that `free_balance` does not account for, or future currency trait changes), the extrinsic still returns `Ok(())`. The bounty (or child bounty) record is deleted and the "Claimed"/"BountyClaimed" event is emitted, falsely signaling that the beneficiary/curator were paid. The funds remain stranded in the bounty sub-account with no bounty record left to reference them, and there is no retry mechanism since the bounty entry no longer exists. This is a permanent, irrecoverable loss/lock of treasury-derived reward funds — matching the required impact category of "permanent user-fund lock" / "duplicate settlement or payout" state mismatch in a reward payout flow.

### Likelihood Explanation
Under the currently exercised test/benchmark conditions, the transfers are expected to succeed, which is why this has not manifested as an obvious bug. However, the invariant is enforced only by a debug-only assertion, not by returning an error, propagating it with `?`, or rolling back the storage mutation. Any deviation in account state, or a future change to `Currency`/`fungible` trait implementations (e.g. holds, freezes, or additional preconditions on withdrawal) silently reintroduces the fund-loss condition without any test failing in production builds, since `debug_assert!` is stripped from release binaries. This is a structural robustness bug independent of a specific triggering scenario, directly analogous in class to the reported finding.

### Recommendation
Replace `debug_assert!(res.is_ok())` / discarding `fee_transfer_result` and `payout_transfer_result` with real error propagation (e.g. `res?` or `.map_err(...)?`) *before* mutating and finalizing any storage (`*maybe_bounty = None`, `ChildBountyDescriptionsV1::remove`, `ParentChildBounties::mutate`, event emission). Ensure the entire claim function is atomic — i.e., either both transfers succeed and the bounty state is finalized, or the whole extrinsic errors out and the bounty remains claimable/retryable.

### Proof of Concept
1. Construct or force a scenario where `T::Currency::transfer(&bounty_account, &curator, final_fee, AllowDeath)` or the beneficiary transfer returns `Err` while still passing the earlier `ensure!` checks (e.g. by using a `Currency`/`fungible` implementation in the runtime configuration that imposes a lock/freeze/hold on the bounty sub-account that `free_balance` doesn't reflect, or a custom `Currency` impl that fails destructive `AllowDeath` withdrawals under specific conditions).
2. Call `claim_bounty(origin, bounty_id)` from any signed account after `unlock_at` has passed.
3. Observe: extrinsic returns `Ok(())`, `BountyClaimed` event is emitted, `Bounties::<T,I>::get(bounty_id)` is now `None`, but the curator/beneficiary balances are unchanged and the bounty account still holds the funds with no addressable bounty record to reclaim them — because in a release build `debug_assert!(res.is_ok())` is a no-op and never halts execution on `Err`. [3](#0-2) [4](#0-3)

### Citations

**File:** substrate/frame/bounties/src/lib.rs (L800-844)
```rust
			ensure_signed(origin)?; // anyone can trigger claim

			Bounties::<T, I>::try_mutate_exists(bounty_id, |maybe_bounty| -> DispatchResult {
				let bounty = maybe_bounty.take().ok_or(Error::<T, I>::InvalidIndex)?;
				if let BountyStatus::PendingPayout { curator, beneficiary, unlock_at } =
					bounty.status
				{
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

					Self::deposit_event(Event::<T, I>::BountyClaimed {
						index: bounty_id,
						payout,
						beneficiary,
					});
					Ok(())
				} else {
					Err(Error::<T, I>::UnexpectedStatus.into())
				}
			})?;
			Ok(())
		}
```

**File:** substrate/frame/child-bounties/src/lib.rs (L686-771)
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
					} else {
						Err(BountiesError::<T>::UnexpectedStatus.into())
					}
				},
			)
		}
```
