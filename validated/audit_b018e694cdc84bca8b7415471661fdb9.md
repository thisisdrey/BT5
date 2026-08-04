Based on the evidence gathered, I found a strong local analog in `pallet-revive`'s storage deposit refund path, where the "unlock accounting" is decremented by the full logical amount while the actual balance movement off the hold is best-effort and can come up short — exactly the Buffer Pool pattern (send less than the tracked/locked amount, but the "locked" bookkeeping still gets reduced by the full amount, permanently stranding the difference).

### Title
Storage-deposit refund under-transfers via `Precision::BestEffort` while ContractInfo/hold accounting is already decremented by the full amount, permanently stranding the difference - ([File: substrate/frame/revive/src/metering/meter.rs])

### Summary
In `pallet-revive`, when a contract's storage shrinks, the metering layer computes a `Deposit::Refund(amount)` and applies it to the contract's `ContractInfo` (decrementing the tracked deposit fields) via `Diff::update_contract` *before* the actual currency movement happens. The actual token movement is then performed by `ReservingExt::charge` → `Pallet::refund_deposit` → `T::Deposit::refund_on_hold`, which calls `T::Currency::transfer_on_hold(..., Precision::BestEffort, ...)`. If the transfer moves less than `amount` (e.g. because of a lock/freeze/existential-deposit conflict on the destination, analogous to the Buffer Finance scenario where price factors cause a partial payout), the code only logs an error and proceeds — it never reconciles the shortfall back into `ContractInfo` or re-holds it under a reason still tracked by the deposit accounting. [1](#0-0) 

### Finding Description
This is structurally the same bug class as `BufferBinaryPool#send`: an internal ledger ("lockedAmount" / storage-deposit accounting in `ContractInfo`) is unconditionally reduced by the *intended* amount, while the actual value transfer that is supposed to correspond 1:1 to that reduction can legitimately be partial. In `ReservingExt::charge`:
```
Deposit::Refund(amount) => {
    let transferred = T::Currency::transfer_on_hold(
        &HoldReason::StorageDepositReserve.into(),
        contract, origin, *amount,
        Precision::BestEffort, Restriction::Free, Fortitude::Polite,
    )?;
    ...
    if transferred < *amount {
        log::error!(..."This should never happen, if it does it means that there is a bug in the runtime logic...");
    }
},
``` [1](#0-0) 

The comment itself acknowledges this "should never happen" but is defended only with `BestEffort` and a log — there is no assertion, no re-crediting of the shortfall to the contract's hold-tracking, and no error propagation that would roll back the `ContractInfo` decrement that already happened in `finalize_own_contributions` / `Diff::update_contract`. [2](#0-1) 

The similar pattern is also seen and was explicitly called out as a real, fixed bug elsewhere in the same codebase: `pallet-assets::transfer_ownership` used to discard the remainder from `repatriate_reserved`, which the PR author fixed by treating a nonzero remainder as a hard failure (`IncompleteDepositTransfer`) rather than silently proceeding. [3](#0-2) 

The `pallet-revive` refund path does not receive the equivalent fix: it still treats a short transfer as a "shouldn't happen" edge case handled only by logging, exactly the failure mode the Buffer report flags — the ledger ("locked"/deposit-tracked amount) is unlocked/decremented by the full amount while only part of the actual value moves, permanently orphaning the rest under a hold reason (`HoldReason::StorageDepositReserve`) that the pallet's own accounting (`ContractInfo`) no longer references, so it can never be refunded through normal contract logic again.

### Impact Explanation
If the transfer of the refund is short — for instance the origin account has a competing lock/freeze that limits how much can land as free/reducible balance, similar to how `Balances::repatriate_reserved` or `transfer_on_hold` can return a nonzero remainder under `Fortitude::Polite` — a portion of the contract's reserved/held storage deposit becomes permanently stuck. This is real value that is neither refunded to the depositor nor recoverable by the contract, because `ContractInfo`'s deposit fields have already been reduced to reflect a state where that value doesn't exist. This matches the "permanent user-fund lock" impact category from the SDK program (funds held under a runtime hold reason with no code path left that will ever release them).

### Likelihood Explanation
The likelihood is bounded by how often the destination account (`origin`) has a lock/freeze/ED conflict that limits `transfer_on_hold` under `Precision::BestEffort`/`Fortitude::Polite` — this is plausible for accounts participating in governance/staking with active locks (the same scenario the analogous `refund_deposit` warning path in `pallet-revive` itself calls out for *rejected* refunds: "This could be the case if the contract participates in government"). Since the maintainers' own code comment for this exact `charge` path admits "this should never happen... if it does it means there is a bug," this indicates the invariant is assumed rather than enforced, and no test in the visible index exercises the under-transfer branch to confirm it is unreachable. [4](#0-3) 

### Recommendation
Do not allow `Deposit::Refund` accounting to be finalized against `ContractInfo` before the actual token movement succeeds in full. Either: (1) perform the currency transfer with `Precision::Exact` and propagate the error (rolling back the storage-deposit decrement) so a short transfer aborts the whole operation atomically, mirroring the fix applied in `pallet-assets::transfer_ownership` (`IncompleteDepositTransfer`), or (2) if `BestEffort` must be retained, capture the actual `transferred` amount and re-credit the shortfall back into a value the pallet still tracks (e.g., keep it counted in `ContractInfo`'s deposit fields, or re-hold it under a reason that a follow-up refund attempt can pick up), rather than only logging an error.

### Proof of Concept
Not independently reproducible from the indexed excerpts alone — the exact preconditions needed to make `transfer_on_hold(..., Precision::BestEffort, ...)` return `transferred < amount` for the storage-deposit hold reason (e.g., specific lock/freeze combinations on the `origin` account) were not confirmed to be reachable in this pass due to tool/index limitations (file reads for the surrounding `metering/storage.rs` and `deposit_payment.rs` context could not be completed in the final iteration). A Devin session with full repository access should reconstruct the exact scenario (e.g., a contract-deposit-paying account that also holds a competing balance lock) and add/execute a test analogous to `nested_clear_refund_matches_direct_clear`/`burn_held_on_sub_ed_hold_partial_refund` but targeting `ReservingExt::charge`'s `Deposit::Refund` branch to confirm whether `transferred < amount` is actually reachable and whether the shortfall is truly unrecoverable.

### Citations

**File:** substrate/frame/revive/src/metering/storage.rs (L487-494)
```rust
	/// Determine the actual final charge from the own contributions
	pub fn finalize_own_contributions(&mut self, info: Option<&mut ContractInfo<T>>) {
		let deposit = self.own_contribution.update_contract(info);
		self.own_contribution = Contribution::Checked(deposit);

		// no need to recalculate max_charged here as the consumed amount cannot increase
		// when taking removed bytes/items into account
	}
```

**File:** substrate/frame/revive/src/metering/storage.rs (L567-594)
```rust

```

**File:** prdoc/pr_12366.prdoc (L1-11)
```text
title: 'pallet-assets: enforce full deposit transfer in transfer_ownership'
doc:
- audience: Runtime Dev
  description: |-
    `transfer_ownership` previously discarded the remainder returned by `repatriate_reserved`.
    Under `Polite` fortitude, a lock or freeze on the current owner that overlaps their free
    balance can leave part of the reserved deposit behind, leaving the asset's recorded deposit
    out of sync with what is actually reserved. The call now captures the returned remainder and
    rejects the dispatch with a new `IncompleteDepositTransfer` error when it is non-zero;
    the storage layer rolls back the partial move. To recover, the current owner must clear or
    reduce the offending locks/freezes and retry.
```

**File:** substrate/frame/revive/src/lib.rs (L2807-2830)
```rust
		let result = T::Deposit::refund_on_hold(hold_reason, from, dst, amount);

		result.defensive_map_err(|err| {
			let available = T::Deposit::total_on_hold(hold_reason, from);
			if available < amount {
				// The storage deposit accounting got out of sync with the balance: This would be a
				// straight up bug in this pallet.
				log::error!(
					target: LOG_TARGET,
					"Failed to refund storage deposit {amount:?} from contract {from:?} to origin {to:?}. Not enough deposit: {available:?}. This is a bug.",
				);
				Error::<T>::StorageRefundNotEnoughFunds.into()
			} else {
				// There are some locks preventing the refund. This could be the case if the
				// contract participates in government. The consequence is that if a contract votes
				// with its storage deposit it would no longer be possible to remove storage without first
				// reducing the lock.
				log::warn!(
					target: LOG_TARGET,
					"Failed to refund storage deposit {amount:?} from contract {from:?} to origin {to:?}: {err:?}. First remove locks (staking, governance) from the contracts account.",
				);
				Error::<T>::StorageRefundLocked.into()
			}
		})
```
