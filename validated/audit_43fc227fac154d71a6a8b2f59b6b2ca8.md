Audit Report

## Title
Permissionless `claim_bounty` finalizes bounty removal without verifying transfer success, risking permanently stranded bounty funds - (File: `substrate/frame/bounties/src/lib.rs`)

## Summary
`claim_bounty` in the Bounties pallet is callable by any signed account and, after computing `payout`/`final_fee` from the bounty account's balance, performs `T::Currency::transfer` calls to the curator and beneficiary whose results are checked only via `debug_assert!`, which compiles to a no-op in release builds. `*maybe_bounty = None` and `BountyDescriptions::remove(bounty_id)` execute unconditionally in the same call regardless of whether either transfer actually succeeded, matching the code exactly as quoted in the claim. [1](#0-0) 

## Finding Description
`claim_bounty` only requires `ensure_signed(origin)?`, so any account can trigger it once a bounty is in `PendingPayout` and past `unlock_at`. [2](#0-1)  It computes `payout = balance.saturating_sub(fee)` and transfers `final_fee` to the curator and `payout` to the beneficiary using `AllowDeath`, checking the `Result` only with `debug_assert!(res.is_ok())`. [3](#0-2)  Immediately afterward, `*maybe_bounty = None`, `BountyDescriptions::<T, I>::remove(bounty_id)`, and `T::ChildBountyManager::bounty_removed(bounty_id)` run unconditionally. [4](#0-3)  Since `debug_assert!` is a no-op when `debug_assertions` is disabled (the normal state for release/production runtimes), a failing transfer (e.g., a `payout`/`final_fee` below existential deposit for a non-existent destination account, using `AllowDeath` semantics) is silently ignored while bookkeeping is finalized as if it succeeded. Once `*maybe_bounty = None` executes, the only pointer tying `bounty_account`'s residual balance to an owed payout is destroyed; unlike `close_bounty`, which calls `T::TransferAllAssets::force_transfer_all_assets` to sweep the account while the bounty entry still exists, there is no dispatchable that can recover funds from an orphaned `bounty_account` after `claim_bounty` completes.

## Impact Explanation
This matches the "permanent user-fund or bridge-state lock" impact class: value transferred out of `bounty_account` intended for the curator or beneficiary can be permanently stranded with no dispatchable path to recover it if a transfer to either destination fails and the runtime is built without debug assertions. The exact corrupted state is the `Bounties` storage entry for `bounty_id`, whose removal severs any tracking of the residual balance left in the derived `bounty_account`.

## Likelihood Explanation
The trigger condition (a computed `payout` or `final_fee` amount that fails to transfer, e.g., because it is below `ExistentialDeposit` to a beneficiary/curator account with zero existing balance) is reachable through ordinary bounty parameterization and requires no privileged actor — any signed account can invoke `claim_bounty` once the payout delay has elapsed. This is exactly what the claim describes, and the code matches verbatim.

## Recommendation
Replace `debug_assert!(res.is_ok())` with proper error propagation (e.g., `res?`) for both the curator-fee and beneficiary transfers before mutating `*maybe_bounty = None` and removing `BountyDescriptions`, so that bounty bookkeeping is only finalized once transfers are confirmed to have succeeded. Consider using `KeepAlive` or explicit handling/refund logic for sub-ED payouts instead of `AllowDeath`.

## Proof of Concept
1. Propose and fund a bounty; have the council `award_bounty` to a fresh `beneficiary` account with zero balance.
2. Set `curator_fee` such that `payout = balance.saturating_sub(fee)` is smaller than `ExistentialDeposit`.
3. Wait past `unlock_at`, then have any signed account call `claim_bounty(origin, bounty_id)` in a release build (`debug_assertions` disabled).
4. `T::Currency::transfer(&bounty_account, &beneficiary, payout, AllowDeath)` returns `Err` (deposit below ED) but is ignored via `debug_assert!`; `*maybe_bounty = None` and `BountyDescriptions::remove` still execute, and `Event::BountyClaimed` is emitted despite the beneficiary never receiving funds, leaving `payout` permanently stuck in `bounty_account` with no remaining `Bounty` entry referencing it.

### Citations

**File:** substrate/frame/bounties/src/lib.rs (L796-831)
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
```
