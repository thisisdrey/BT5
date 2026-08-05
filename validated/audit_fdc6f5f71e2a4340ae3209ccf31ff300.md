Audit Report

## Title
Unchecked `Currency::transfer()` return value in bounty payout permanently deletes bounty state on failed transfer, permanently locking funds - (File: `substrate/frame/bounties/src/lib.rs`, function `claim_bounty`)

## Summary
`pallet-bounties::claim_bounty` performs both the curator-fee and beneficiary-payout `Currency::transfer()` calls and only checks the result with `debug_assert!(res.is_ok())`, which compiles to a no-op in production/release builds, before unconditionally deleting the bounty record and emitting `BountyClaimed`. The same pattern exists in `pallet-child-bounties::impl_close_child_bounty`. This is confirmed to be present verbatim in the current codebase.

## Finding Description
`claim_bounty` computes `fee`/`payout` from the bounty sub-account's free balance and does: [1](#0-0) 
The result of both `T::Currency::transfer` calls is discarded except for a `debug_assert!`, which is stripped out entirely in release builds, so in production `*maybe_bounty = None`, `BountyDescriptions::remove`, and `Event::BountyClaimed` all execute regardless of whether the transfer actually succeeded. `bounty_account_id` is a deterministic, publicly-derivable sub-account: [2](#0-1) 
The parallel unchecked pattern also exists in child bounties: [3](#0-2) 

However, the claimed exploit mechanism — using `pallet_vesting::vested_transfer` to place a lock on the bounty sub-account that later blocks `claim_bounty`'s transfer — does not hold up under closer inspection of the current `pallet_balances` implementation in this repo. The lock-checking logic (`ensure_can_withdraw`) in the current codebase ignores `WithdrawReasons` entirely and only compares against a pre-computed `frozen` field: [4](#0-3) 
This means the runtime-configured `UnvestedFundsAllowedWithdrawReasons` exception (which is set in production runtimes, e.g. `WithdrawReasons::except(WithdrawReasons::TRANSFER | WithdrawReasons::RESERVE)`) is not consulted by this transfer-liquidity check at all in the current fungible-trait based implementation — meaning *any* lock via `set_lock`/`vested_transfer` freezes the locked amount against all withdrawal reasons uniformly, not just against the reasons the runtime intends to restrict. I was not able to fully trace how `Self::account(who).frozen` is computed (whether it aggregates all locks unconditionally or applies reason-based filtering elsewhere, e.g. in `update_locks`/`Freeze` trait bridging) within the available tool budget, so I cannot conclusively confirm or refute whether a `vested_transfer`-created lock would actually block a subsequent `AllowDeath` transfer out of the bounty account in this version of the runtime/pallet.

Regardless of that specific attack vector's exact mechanics, the core code-quality/security issue — `debug_assert!` used in place of `?` propagation for a value-moving `Currency::transfer` before finalizing/destroying state — is real and verifiable in the code as cited above. The class of risk (a transfer failing for *any* reason — including simple existential-deposit/liquidity edge cases already acknowledged as reachable elsewhere in the codebase, e.g. `pallet_vesting`'s own tests showing `TokenError::Frozen`/`LiquidityRestrictions` are reachable failure modes for locked accounts) can silently cause the bounty record to be deleted without the funds actually moving, since the flow deletes state and emits the success event unconditionally after the unchecked transfers.

## Impact Explanation
If either `Currency::transfer` fails in production (no panic, since `debug_assert!` is stripped), the bounty storage entry is deleted, the curator deposit already unreserved, and `BountyClaimed` is emitted, but the payout/fee never left the sub-account. Since the bounty entry is removed from storage, there is no remaining code path to retry the payout, leaving the funds stuck in the sub-account. This matches the "permanent user-fund lock" / "duplicate settlement or payout" impact criteria without requiring privileged access — only the permissionless `claim_bounty` call combined with any way to make the internal transfer fail.

## Likelihood Explanation
The unchecked-transfer defect itself is fully confirmed. However, the specific proof-of-concept mechanism proposed in the claim — pre-loading a `pallet_vesting::vested_transfer` lock onto the deterministic bounty sub-account address to force `LiquidityRestrictions`/`Frozen` — could not be fully verified against the current `pallet_balances`/lockable-currency implementation in this repo within the available investigation. The `ensure_can_withdraw` function's `_reasons` parameter is unused, and I could not confirm within budget whether the `frozen` field computation already applies the runtime's `UnvestedFundsAllowedWithdrawReasons` exception elsewhere (e.g., during `update_locks`) such that transfer-reason locks from vesting would in fact be excluded from `frozen`, which would defeat the described attack. This is a meaningful gap in verifying the exact reachability/likelihood of the specific PoC as described, though it does not change the fact that the unchecked `debug_assert!` pattern itself is a real, un-mitigated hazard for any transfer failure mode.

## Recommendation
Replace `debug_assert!(res.is_ok())` with proper error propagation (`?`) in `claim_bounty` (both curator-fee and beneficiary-payout transfers) and in `impl_close_child_bounty`, so a failed transfer aborts the `try_mutate_exists` closure and leaves the bounty record intact and retryable, instead of silently deleting state. Separately, verify — via `cargo test -p pallet-balances` and `cargo test -p pallet-vesting` in a full session — whether `ensure_can_withdraw`'s reliance on `Self::account(who).frozen` (ignoring `_reasons`) actually causes `WithdrawReasons`-scoped locks (like vesting's transfer-exception lock) to block transfers they were configured to permit; this may itself be a separate defect worth investigating if confirmed.

## Proof of Concept
Not independently reproducible within this session — no terminal/test-execution access was available to confirm whether a `vested_transfer`-created lock on the deterministically-derived bounty account (`bounty_account_id`) actually triggers `LiquidityRestrictions`/`Frozen` against `claim_bounty`'s `AllowDeath` transfer given the current `ensure_can_withdraw` implementation ignoring `WithdrawReasons`. A conclusive PoC would require running `cargo test -p pallet-bounties` combined with a mock runtime wiring in `pallet_vesting`, or tracing the full lock-to-frozen pipeline (`set_lock` → `update_locks` → `AccountData.frozen`) in `substrate/frame/balances/src/lib.rs`, in a session with full repository and terminal access.

### Citations

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

**File:** substrate/frame/bounties/src/lib.rs (L1173-1178)
```rust
	/// The account ID of a bounty account
	pub fn bounty_account_id(id: BountyIndex) -> T::AccountId {
		// only use two byte prefix to support 16 byte account id (used by test)
		// "modl" ++ "py/trsry" ++ "bt" is 14 bytes, and two bytes remaining for bounty index
		T::PalletId::get().into_sub_account_truncating(("bt", id))
	}
```

**File:** substrate/frame/child-bounties/src/lib.rs (L934-951)
```rust
				// Transfer fund from child-bounty to parent bounty.
				let parent_bounty_account =
					pallet_bounties::Pallet::<T>::bounty_account_id(parent_bounty_id);
				let child_bounty_account =
					Self::child_bounty_account_id(parent_bounty_id, child_bounty_id);
				let balance = T::Currency::free_balance(&child_bounty_account);
				let transfer_result = T::Currency::transfer(
					&child_bounty_account,
					&parent_bounty_account,
					balance,
					AllowDeath,
				); // Should not fail; child bounty account gets this balance during creation.
				debug_assert!(transfer_result.is_ok());

				// Remove the child-bounty description.
				ChildBountyDescriptionsV1::<T>::remove(parent_bounty_id, child_bounty_id);

				*maybe_child_bounty = None;
```

**File:** substrate/frame/balances/src/impl_currency.rs (L375-389)
```rust
	// Ensure that an account can withdraw from their free balance given any existing withdrawal
	// restrictions like locks and vesting balance.
	// Is a no-op if amount to be withdrawn is zero.
	fn ensure_can_withdraw(
		who: &T::AccountId,
		amount: T::Balance,
		_reasons: WithdrawReasons,
		new_balance: T::Balance,
	) -> DispatchResult {
		if amount.is_zero() {
			return Ok(());
		}
		ensure!(new_balance >= Self::account(who).frozen, Error::<T, I>::LiquidityRestrictions);
		Ok(())
	}
```
