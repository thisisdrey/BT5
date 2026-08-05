Audit Report

## Title
Bounty payout silently fails below the existential deposit, permanently stranding funds in the bounty sub-account - (File: `substrate/frame/bounties/src/lib.rs`)

## Summary
`Pallet::claim_bounty` in `substrate/frame/bounties/src/lib.rs` and `claim_child_bounty` in `substrate/frame/child-bounties/src/lib.rs` pay out the curator fee and beneficiary payout via `T::Currency::transfer(..., AllowDeath)`, but only validate the result with `debug_assert!`, which compiles to a no-op in release builds. The bounty storage entry is unconditionally cleared immediately afterward, so if a transfer fails (e.g., because the payout is below the beneficiary account's existential deposit), the funds remain stuck in the bounty's derived sub-account with no remaining state referencing them.

## Finding Description
In `claim_bounty`, `payout = balance.saturating_sub(fee)` is transferred to the beneficiary with `AllowDeath`, and the curator fee is transferred with `AllowDeath` as well: [1](#0-0) 

Both transfer results are discarded through `debug_assert!(res.is_ok())` rather than propagated with `?`, and the bounty storage entry, description, and child-bounty bookkeeping are removed unconditionally right after, with no check on transfer success: [2](#0-1) 

The same pattern is present in `claim_child_bounty`: [3](#0-2) 

`claim_bounty` is callable by any signed account once the bounty is in `PendingPayout`: [4](#0-3) 

`pallet_balances` rejects transfers into a new account below the existential deposit with `TokenError::BelowMinimum`, as shown by the existing balances test. In a release build, `debug_assert!` does not execute, so this error is silently swallowed while `*maybe_bounty = None;` still executes, permanently deleting the only state pointing to the stranded funds in the bounty's `PalletId`-derived sub-account.

## Impact Explanation
This is a permanent user-fund lock: value intended for the beneficiary (or curator) is stranded irrecoverably in the bounty sub-account once the corresponding storage entry referencing it is deleted, with no governance or recovery path remaining. This matches the accepted "permanent user-fund lock" impact category for the Polkadot SDK program, and requires no privileged or malicious actor — only ordinary, permissionless dispatch of `claim_bounty`/`claim_child_bounty` once a bounty reaches `PendingPayout` with a sub-ED residual payout to a previously nonexistent account.

## Likelihood Explanation
Triggering the bug requires: (1) a bounty/child-bounty reaching `PendingPayout` via `award_bounty`, and (2) the computed `payout` (or `final_fee`) falling below the chain's `ExistentialDeposit` for a beneficiary/curator account with no prior existence. This is a plausible outcome of bounty value/fee arithmetic on chains with non-trivial EDs (e.g., Polkadot/Kusama-scale deposits), and since `claim_bounty` is permissionlessly callable by any signed account, any observer can trigger the loss once such a bounty exists — no special privileges, timing races, or validator/relayer compromise needed.

## Recommendation
Propagate the `DispatchResult` from both `T::Currency::transfer` calls with `?` instead of discarding it via `debug_assert!`, and only clear the bounty/child-bounty storage (and description) after both transfers succeed. As a defense-in-depth alternative, floor `payout`/`final_fee` at the `ExistentialDeposit` or reject bounty configurations (at `award_bounty` time) that would produce a sub-ED payout to a nonexistent account.

## Proof of Concept
1. Set a chain with a non-trivial `ExistentialDeposit = ED`.
2. Propose and fund a bounty where `value - fee < ED`, with a fresh (zero-balance) `beneficiary` account, then call `award_bounty`.
3. Wait past `unlock_at`.
4. Call `claim_bounty(bounty_id)` from any signed account in a release build.
5. `T::Currency::transfer(&bounty_account, &beneficiary, payout, AllowDeath)` returns `Err(TokenError::BelowMinimum)`; `debug_assert!(res.is_ok())` is compiled out and does not panic.
6. `*maybe_bounty = None;` executes regardless, deleting the bounty record and description.
7. `payout` remains permanently stuck in `bounty_account` with no on-chain state referencing it and no recovery mechanism.

### Citations

**File:** substrate/frame/bounties/src/lib.rs (L794-800)
```rust
		#[pallet::call_index(6)]
		#[pallet::weight(<T as Config<I>>::WeightInfo::claim_bounty())]
		pub fn claim_bounty(
			origin: OriginFor<T>,
			#[pallet::compact] bounty_id: BountyIndex,
		) -> DispatchResult {
			ensure_signed(origin)?; // anyone can trigger claim
```

**File:** substrate/frame/bounties/src/lib.rs (L820-826)
```rust
					let final_fee = fee.saturating_sub(children_fee);
					let res =
						T::Currency::transfer(&bounty_account, &curator, final_fee, AllowDeath); // should not fail
					debug_assert!(res.is_ok());
					let res =
						T::Currency::transfer(&bounty_account, &beneficiary, payout, AllowDeath); // should not fail
					debug_assert!(res.is_ok());
```

**File:** substrate/frame/bounties/src/lib.rs (L827-831)
```rust

					*maybe_bounty = None;

					BountyDescriptions::<T, I>::remove(bounty_id);
					T::ChildBountyManager::bounty_removed(bounty_id);
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
