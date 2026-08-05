### Title
`claim_child_bounty` silently accepts failed payouts because `debug_assert!` on the transfer result compiles to a no-op in production builds - (File: `substrate/frame/child-bounties/src/lib.rs`)

### Summary
`ChildBounties::claim_child_bounty` (and its sibling `Bounties::claim_bounty`) computes a curator-fee and beneficiary payout from the child-bounty sub-account's `free_balance`, then calls `T::Currency::transfer(...)` twice and only verifies success with `debug_assert!`. In a release/production runtime `debug_assert!` is compiled to nothing, so a failing transfer is silently ignored: the pallet still deletes the child-bounty record, decrements the active-count, and emits `Event::Claimed` as if the payout succeeded — exactly the unchecked-transfer pattern described in the external report (transfer return/result ignored → state advances → beneficiary receives nothing).

### Finding Description [1](#0-0) 

```
let balance = T::Currency::free_balance(&child_bounty_account);
let curator_fee = child_bounty.fee.min(balance);
let payout = balance.saturating_sub(curator_fee);
...
let fee_transfer_result = T::Currency::transfer(&child_bounty_account, curator, curator_fee, AllowDeath);
debug_assert!(fee_transfer_result.is_ok());
...
let payout_transfer_result = T::Currency::transfer(&child_bounty_account, beneficiary, payout, AllowDeath);
debug_assert!(payout_transfer_result.is_ok());
...
*maybe_child_bounty = None;   // state finalized unconditionally
```

`debug_assert!` is a `cfg(debug_assertions)` macro: on any production build (`cargo build --release`, and every node binary shipped by `polkadot-sdk`) it expands to nothing. The `DispatchResult` of `T::Currency::transfer` is therefore never checked, and regardless of the outcome the function proceeds to delete `ChildBounties`, decrement `ParentChildBounties`, remove the description, and fire `Event::Claimed { payout, beneficiary, .. }`.

`free_balance` reflects total minus reserved balance, but it does **not** subtract balances constrained by `Freeze`/`Lock` (e.g. a vesting schedule). `T::Currency::transfer` internally calls `ensure_can_withdraw`, which does check locks, so a transfer can fail with `Error::LiquidityRestrictions` (or similar) even though `free_balance` reported the funds as available.

The `child_bounty_account_id` is a deterministic `PalletId`-derived sub-account with **no private key**, so any lock ever placed on it can never be removed (no one can sign a `vest`/`vest_other`-style unlock as that account). An unprivileged attacker can call `pallet_vesting::vested_transfer` (or `force_vested_transfer`, or any other pallet that can apply a lock to an arbitrary target without the target's signature) against the known, precomputable child-bounty sub-account address before the payout is claimed. This places a permanent lock on part of that sub-account's balance.

When `claim_child_bounty` is later executed:
1. `free_balance` still reports the full (locked+unlocked) balance, so `curator_fee`/`payout` are computed against the full amount.
2. The `transfer` calls to curator and/or beneficiary fail due to the lock.
3. Because the failure is only checked via `debug_assert!` (a no-op in release), the error is swallowed.
4. The pallet still deletes the child bounty, decrements counts, and emits `Claimed` with the full payout amount.
5. The locked funds remain stuck forever in the now-untracked, key-less sub-account — the bounty record that referenced them no longer exists, so there is no dispatchable path to retrieve or re-issue payout.

This produces both a false "successful settlement" record (the chain's own event log claims funds were paid when they were not) and a permanent fund lock, matching the "duplicate settlement or payout" / "permanent user-fund ... lock" impact categories, using only unprivileged, public dispatchables (`pallet_vesting::vested_transfer` + `claim_child_bounty`) — no malicious validator, relayer, or governance actor is required.

### Impact Explanation
- Funds allocated to a child bounty can become permanently unrecoverable while the runtime state and events report the bounty as successfully claimed/paid.
- The beneficiary/curator receive nothing, yet no error is surfaced to the caller and no retry mechanism exists because the bounty storage entry is deleted unconditionally.
- This is a direct fund-loss/fund-lock bug reachable by any signed account, fitting the "permanent user-fund ... lock" and "duplicate settlement or payout" impact categories in scope.

### Likelihood Explanation
- All actions required (`pallet_vesting::vested_transfer` targeting an arbitrary, precomputable `AccountId`, followed by calling the public `claim_child_bounty` extrinsic) are available to any signed, unprivileged account on chains that include `pallet_vesting` alongside `pallet_child_bounties`/`pallet_bounties` (as in the Polkadot/Kusama/Westend/Asset-Hub-style runtimes shipped in this repo).
- The sub-account address is fully deterministic (`PalletId::into_sub_account_truncating(("cb", parent_id, child_id))`), so the attacker can precompute the target address for any (or soon-to-exist) child bounty and front-run the curator's claim.
- The bug is masked in tests/debug builds (where `debug_assert!` panics and would be caught), but silently active in every production binary, making it likely to go unnoticed by standard CI while still being live on real deployed nodes.

### Recommendation
Replace all `debug_assert!(transfer_result.is_ok())` patterns for fund-moving operations in `substrate/frame/bounties/src/lib.rs` and `substrate/frame/child-bounties/src/lib.rs` with proper `?`-propagated `DispatchResult` handling (or explicit `ensure!`/early-return on error), so that a failed transfer aborts the extrinsic (via `try_mutate_exists`'s error path) rather than allowing state to advance as though payment succeeded. Where partial failure must be tolerated, compute the transferable amount using the account's actually-withdrawable balance (accounting for locks) rather than `free_balance`, or move to a asynchronous/`Paymaster`-style payment tracking similar to `pallet-treasury`'s `payout`/`check_status` flow which explicitly tracks payment attempt/success/failure state instead of assuming success.

### Proof of Concept
1. Runtime includes `pallet_vesting`, `pallet_bounties`, `pallet_child_bounties` (e.g. Westend/Polkadot-style runtime).
2. A parent bounty is created, funded, and a child bounty `(parent_bounty_id, child_bounty_id)` is created, assigned a curator, and awarded to a beneficiary, reaching `ChildBountyStatus::PendingPayout`.
3. Attacker computes `child_account = ChildBounties::child_bounty_account_id(parent_bounty_id, child_bounty_id)` (public, deterministic derivation, see [2](#0-1) ).
4. Attacker calls `Vesting::vested_transfer(origin: attacker, target: child_account, schedule)` with a schedule that locks most of the account's balance for a long duration — the target does not need to sign this call.
5. Once `unlock_at` has passed, anyone calls `ChildBounties::claim_child_bounty(parent_bounty_id, child_bounty_id)`.
6. `T::Currency::transfer(&child_account, curator/beneficiary, amount, AllowDeath)` fails internally due to the vesting lock (`LiquidityRestrictions`), but in a release build the `debug_assert!` check is compiled out, so the error is discarded.
7. `ChildBounties` entry is removed, `Event::Claimed` is emitted with the full payout amount, yet `Balances::free_balance(curator)`/`Balances::free_balance(beneficiary)` show no increase — the funds remain frozen in `child_account`, which has no corresponding signing key and no remaining bounty record to trigger any further payout attempt.

### Citations

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

**File:** substrate/frame/child-bounties/src/lib.rs (L853-862)
```rust
	/// The account ID of a child-bounty account.
	pub fn child_bounty_account_id(
		parent_bounty_id: BountyIndex,
		child_bounty_id: BountyIndex,
	) -> T::AccountId {
		// This function is taken from the parent (bounties) pallet, but the
		// prefix is changed to have different AccountId when the index of
		// parent and child is same.
		T::PalletId::get().into_sub_account_truncating(("cb", parent_bounty_id, child_bounty_id))
	}
```
