## Finding: Silent swallow of failed bounty payout transfers in `claim_bounty` leads to permanent loss of beneficiary funds

### Title
Bounty `claim_bounty` performs a forced push-transfer to the beneficiary and beneficiary/curator fee accounts and silently ignores failure with `debug_assert!`, permanently deleting bounty state while leaving funds stranded/undeliverable — ([File: substrate/frame/bounties/src/lib.rs])

### Summary
`pallet-bounties::claim_bounty` mirrors the exact bug class from the external report: it forcefully pushes funds out of a pallet-controlled account (`bounty_account`) to a fixed recipient (`beneficiary`, and separately `curator`) instead of letting the recipient pull the funds. If the transfer to `beneficiary` reverts — e.g. because the beneficiary account is administratively frozen/blocklisted for the underlying asset type, or otherwise rejects the incoming transfer — the result is discarded via `debug_assert!`, which compiles to a no-op in release builds. The bounty record is deleted in the same atomic mutation regardless of whether the transfer succeeded. [1](#0-0) 

### Finding Description
`claim_bounty` is a permissionless call (`ensure_signed(origin)?; // anyone can trigger claim`) that, once a bounty is in `PendingPayout`, computes `final_fee` and `payout`, then does:

```rust
let res = T::Currency::transfer(&bounty_account, &curator, final_fee, AllowDeath); // should not fail
debug_assert!(res.is_ok());
let res = T::Currency::transfer(&bounty_account, &beneficiary, payout, AllowDeath); // should not fail
debug_assert!(res.is_ok());

*maybe_bounty = None;
BountyDescriptions::<T, I>::remove(bounty_id);
T::ChildBountyManager::bounty_removed(bounty_id);
``` [1](#0-0) 

This is exactly the "push" pattern flagged in the external report: the protocol forcibly transfers funds to a stored recipient address rather than letting the recipient claim/pull them. If `T::Currency::transfer` to `beneficiary` (or `curator`) fails for any reason — an administratively-frozen/blocklisted account for the configured `Currency`/fungible adapter, or any other transfer failure — the error is captured in `res` but only checked via `debug_assert!`, which is stripped out in release/production builds (`cfg(debug_assertions)`). The closure that performs this logic still returns `Ok(())`, so `try_mutate_exists` commits all storage changes: the bounty entry is removed, `BountyDescriptions` is removed, and the child-bounty removal hook runs — as if the payout succeeded.

Because the bounty's `Bounties` key no longer exists after this call, the pallet's own recovery mechanism, `reclaim_bounty_funds`, becomes reachable (it only requires `!Bounties::<T, I>::contains_key(bounty_id)`): [2](#0-1) 

`reclaim_bounty_funds` sweeps any remaining balance in the (now orphaned) `bounty_account` back to the **treasury**, not to the intended `beneficiary`. So the net effect of a reverted/failed beneficiary transfer is: the bounty is marked claimed/removed, the event `BountyClaimed` is still emitted claiming the beneficiary was paid, and the actual funds are permanently redirected to the treasury via `reclaim_bounty_funds` instead of the rightful beneficiary — a wrong-beneficiary/fund-loss outcome with no possibility for the affected beneficiary to retry or recover, since the pallet has no per-recipient retry mechanism (unlike the newer `pallet-multi-asset-bounties`, which explicitly uses an async `Pay`/`check_status`/`retry_payment` flow to handle exactly this failure mode).

Existing guards do not stop this path because:
- `claim_bounty` is callable by anyone (`ensure_signed`, no beneficiary-specific check needed to trigger it).
- The atomic `try_mutate_exists` only rolls back on `Err(..)`; since the closure returns `Ok(())` unconditionally after the transfers (regardless of `res`), the state-clearing writes are committed even on transfer failure.
- `debug_assert!` provides zero protection in production runtime builds.

### Impact Explanation
High. This can permanently reroute payout funds away from the intended `beneficiary` to the treasury and destroy the only state (`Bounties` entry) that could have been used to retry or repair the payout. This directly maps to the "theft or unbacked mint or unlock", "duplicate settlement or payout", and "public underpriced work / wrong beneficiary or amount" impact categories: the beneficiary never receives funds they were awarded, while the chain state asserts (via the `BountyClaimed` event and removed bounty) that the payout succeeded.

### Likelihood Explanation
Low-to-medium, matching the external report's likelihood profile: it requires a `Currency`/fungible backend where account-level administrative freezing/blocking of a specific account is possible (e.g. a runtime configuring `pallet-bounties::Currency` over a fungible adapter for an asset supporting freeze/blocklist semantics, or any other realistic transfer-failure condition for the configured currency), combined with a beneficiary that is on such a list or otherwise unable to receive the transfer at claim time.

### Recommendation
Do not use `debug_assert!` to guard a fallible economic operation. `claim_bounty` should:
- Propagate the `Result` of both currency transfers with `?` instead of `debug_assert!`, so a failed transfer aborts and rolls back the whole extrinsic (leaving the bounty in `PendingPayout` so it can be retried), or
- Adopt the pull-based `Pay`/`check_status`/`retry_payment` pattern already used by `pallet-multi-asset-bounties`, so a failed payout can be retried against a corrected beneficiary rather than silently discarding funds to the treasury via `reclaim_bounty_funds`.

### Proof of Concept
1. Configure `pallet-bounties::Currency` in a runtime backed by a fungible implementation that supports account freezing/blocking (e.g., an asset-backed adapter).
2. Create and fund a bounty, get it accepted by a curator, and have the curator call `award_bounty` with `beneficiary = B`.
3. Before `unlock_at`, freeze/block account `B` for the configured currency (administrative action independent of this call path).
4. After `unlock_at`, any signed account calls `claim_bounty(bounty_id)`.
5. In a release build, `T::Currency::transfer(&bounty_account, &B, payout, AllowDeath)` fails, but `debug_assert!(res.is_ok())` is compiled out; the closure still returns `Ok(())`; `Bounties::<T,I>` entry for `bounty_id` is removed and `Event::BountyClaimed` is emitted.
6. Any account later calls `reclaim_bounty_funds(bounty_id)`; the stranded `payout` amount in `bounty_account` is swept to the treasury instead of `B`, permanently denying `B` their awarded funds. [3](#0-2)

### Citations

**File:** substrate/frame/bounties/src/lib.rs (L796-843)
```rust
		pub fn claim_bounty(
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
```

**File:** substrate/frame/bounties/src/lib.rs (L1060-1090)
```rust
		pub fn reclaim_bounty_funds(
			origin: OriginFor<T>,
			#[pallet::compact] bounty_id: BountyIndex,
		) -> DispatchResultWithPostInfo {
			ensure_signed(origin)?;

			// A live bounty still manages its account, so leave it untouched.
			ensure!(!Bounties::<T, I>::contains_key(bounty_id), Error::<T, I>::BountyStillActive);

			debug_assert!(
				T::ChildBountyManager::child_bounties_count(bounty_id) == 0,
				"child bounties should not exist for a closed bounty"
			);

			let bounty_account = Self::bounty_account_id(bounty_id);
			let treasury_account = Self::account_id();

			let transferred = T::TransferAllAssets::force_transfer_all_assets(
				&bounty_account,
				&treasury_account,
			)?;

			// Free only if something moved, otherwise paid to prevent griefing.
			if !transferred {
				return Ok(Pays::Yes.into());
			}

			Self::deposit_event(Event::<T, I>::BountyFundsReclaimed { bounty_id });

			Ok(Pays::No.into())
		}
```
