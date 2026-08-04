## Finding

### Title
Silent transfer failure in bounty payout permanently locks funds in `claim_bounty`/`claim_child_bounty` - (File: `substrate/frame/bounties/src/lib.rs`, `substrate/frame/child-bounties/src/lib.rs`)

### Summary
The external report describes `OrderBook.collectFees()` delivering fees to two recipients (DAO + host) "atomically" where a revert on one leg can brick the whole payout. The Polkadot SDK analog is the opposite failure mode with the same broken invariant — **two sequential currency transfers to two different recipients inside one dispatchable, whose failure is not actually checked** — leading to permanent fund lock rather than a revert-DoS. In `pallet-bounties::claim_bounty` and `pallet-child-bounties::claim_child_bounty`, the curator-fee transfer and the beneficiary-payout transfer are only guarded by `debug_assert!(res.is_ok())`, which compiles to a no-op in release/production builds. If either transfer fails for a mundane, unprivileged reason (destination account doesn't exist and the amount is below the Existential Deposit), the call still succeeds, the bounty record is deleted, and the funds remain permanently stranded in the bounty's derived sovereign account with no code path left to reclaim them.

### Finding Description
In `claim_bounty`: [1](#0-0) 

```rust
let bounty_account = Self::bounty_account_id(bounty_id);
let balance = T::Currency::free_balance(&bounty_account);
let fee = bounty.fee.min(balance);
let payout = balance.saturating_sub(fee);
let err_amount = T::Currency::unreserve(&curator, bounty.curator_deposit);
debug_assert!(err_amount.is_zero());
...
let res = T::Currency::transfer(&bounty_account, &curator, final_fee, AllowDeath); // should not fail
debug_assert!(res.is_ok());
let res = T::Currency::transfer(&bounty_account, &beneficiary, payout, AllowDeath); // should not fail
debug_assert!(res.is_ok());

*maybe_bounty = None;
``` [2](#0-1) 

The identical pattern exists in `claim_child_bounty`: [3](#0-2) 

Both functions:
1. Perform two independent `T::Currency::transfer(..., AllowDeath)` calls — one to the curator (fee) and one to the beneficiary (payout).
2. Check the result only with `debug_assert!`, which is stripped in release builds (`#[cfg(debug_assertions)]`), so a `Result::Err` from either transfer is silently discarded — the dispatchable does **not** propagate the error with `?` and does **not** abort.
3. Unconditionally clear the bounty (`*maybe_bounty = None` / bounty description removal, decrementing child-bounty counters), regardless of whether the transfers actually succeeded.

`Currency::transfer` (as implemented in `pallet_balances`) fails when the destination account doesn't yet exist and the transferred amount is below `ExistentialDeposit` — this is an entirely ordinary condition, not a malicious-token or blacklist scenario, but the practical Substrate analog of "an address rejects the incoming transfer." Because the check is a `debug_assert!` rather than a propagated error, this failure is invisible in production: the extrinsic returns `Ok(())`, a `BountyClaimed`/`Claimed` event fires as if payment succeeded, and the bounty/child-bounty storage entry is deleted.

### Impact Explanation
Once the bounty (or child-bounty) record is removed, there is no remaining on-chain reference to the derived bounty account (`bounty_account_id(bounty_id)` / `child_bounty_account_id(...)`) holding the un-transferred `payout` or `final_fee`. There is no retry dispatchable, no governance recovery path wired to this specific derived account, and no way for the beneficiary/curator to claim the stuck balance — this is a **permanent user-fund lock**, matching the "permanent user-fund or bridge-state lock" category in the impact gate. The trigger requires no malicious peer, validator, collator, relayer, or governance actor — any ordinary curator/proposer flow that results in a sub-ED residual payout to a fresh account reproduces it.

### Likelihood Explanation
Likelihood is realistic but conditional: it requires the final `payout` (bounty value minus curator fee) or `final_fee` to be smaller than `ExistentialDeposit` while the recipient account has zero prior balance (common for freshly generated beneficiary/curator accounts, especially on chains with a non-trivial ED, or for very small bounties/fees). A curator can trivially engineer this by proposing a curator fee close to the full bounty value, leaving a sub-ED beneficiary payout, and then calling `award_bounty`/`claim_bounty` — no privileged action or governance abuse needed since the curator role is an ordinary, unprivileged (albeit assigned) actor in this flow. `debug_assert!` will catch this in any debug/test build (hence it likely evaded testing), but production runtimes ship with `debug_assertions` disabled.

### Recommendation
Mirroring the OrderBook fix: separate and make retryable the delivery of each recipient's share, and don't destroy claim state until each transfer is confirmed. Concretely:
- Replace `debug_assert!(res.is_ok())` with real error propagation (`?`), and do not delete the bounty/child-bounty record until both transfers succeed.
- Alternatively, adopt the pattern already used in `pallet-multi-asset-bounties` (`do_process_payout_payment` with per-beneficiary `PaymentState` and a `retry_payment`/`check_status` flow), which pays and tracks each recipient (curator fee vs. beneficiary payout) independently and retryably instead of assuming both transfers "should not fail."

### Proof of Concept
1. Propose and fund a bounty with `value = V` (treasury `spend_local`/`propose_bounty` + `approve_bounty`).
2. `propose_curator` with `fee` such that `V - fee < ExistentialDeposit` (e.g. `fee = V - 1` on a chain where `ED > 1`).
3. `accept_curator`, then `award_bounty(curator, beneficiary)` where `beneficiary` is a brand-new account with zero balance.
4. After the payout delay, call `claim_bounty(bounty_id)`.
5. In a release build: `T::Currency::transfer(&bounty_account, &beneficiary, payout, AllowDeath)` returns `Err` (payout below ED, account doesn't exist) — `debug_assert!` is compiled out, so execution continues; `BountyClaimed` event fires, bounty entry is deleted.
6. Verify: `beneficiary`'s free balance is unchanged (0), while `Balances::free_balance(bounty_account_id(bounty_id))` still holds the undelivered `payout` amount, and `Bounties::<T>::get(bounty_id)` returns `None` — the funds are now permanently unreachable through any exposed dispatchable. [4](#0-3) [5](#0-4)

### Citations

**File:** substrate/frame/bounties/src/lib.rs (L797-844)
```rust
			origin: OriginFor<T>,
			#[pallet::compact] bounty_id: BountyIndex,
		) -> DispatchResult {
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
