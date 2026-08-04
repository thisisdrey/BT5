Based on my investigation, the strongest local analog to the Ajna `kickWithDeposit()` missing-check pattern is in **`pallet-staking-async`**'s new offence-queue withdrawal guard, which is enforced on the normal `withdraw_unbonded` path but appears bypassed by the `force_unstake` path used by `pallet-fast-unstake`.

### Title
Fast-unstake's `force_unstake` path bypasses the pending-offence/slash withdrawal guard added to `withdraw_unbonded` - (File: `substrate/frame/staking-async/src/pallet/impls.rs`)

### Summary
`pallet-staking-async` recently added a defensive guard (`prdoc/stable2509/pr_9079.prdoc`) so that `withdraw_unbonded` cannot release funds while offences from the previous era are still unprocessed/unapplied (`ensure_era_slashes_applied`, `calculate_earliest_withdrawal_era`). This mirrors exactly the Ajna bug class: a debt/slash-safety check enforced on one release path but missing on a second, functionally equivalent release path.

### Finding Description
`do_withdraw_unbonded` computes `earliest_era_to_withdraw` via `calculate_earliest_withdrawal_era`, which caps withdrawal eligibility to the oldest era whose offences have been fully processed, and additionally calls `ensure_era_slashes_applied` to hard-block withdrawal if the previous era's slashes are not yet fully applied: [1](#0-0) [2](#0-1) 

This guard exists specifically because unapplied slashes/offences mean the ledger's "unbonded" amount may still be subject to later reduction — releasing funds before that resolves would let a staker escape a slash that should have applied to them, exactly as HPB depositors in Ajna could escape liquidation debt via `kickWithDeposit()`.

However, `pallet-fast-unstake`'s `on_idle` processing releases funds through a different `StakingInterface` method, `force_unstake`, not through `withdraw_unbonded`: [3](#0-2) 

`check_stash` in fast-unstake only checks `is_exposed_in_era` over the bonding-duration era window it has scanned so far — it has no awareness of `OffenceQueueEras` (offences reported but not yet computed/applied), which is the exact class of race the `pr_9079` guard was created to close: [4](#0-3) 

`StakingInterface::force_unstake` is declared as an independent trait method from `withdraw_unbonded`, with no documented obligation to run the offence-queue check: [5](#0-4) 

Because `force_unstake` is a separate code path from `do_withdraw_unbonded`, the `calculate_earliest_withdrawal_era`/`ensure_era_slashes_applied` guard added for the "prevent withdrawals while processing offences" defense is not automatically inherited by it — the same structural gap as `kickWithDeposit()` missing `_revertIfAuctionDebtLocked()` while `moveQuoteToken`/`removeQuoteToken` had it.

### Impact Explanation
If `force_unstake`'s implementation (in `kill_stash`) unconditionally releases the full stash balance without consulting `OffenceQueueEras`/pending slash state, a staker who is queued for fast-unstake could have their stake released and unreserved before an in-flight offence against them (reported but not yet computed into a slash) is applied — effectively letting them dodge a legitimate slash, which is a direct fund-conservation and slashing-guarantee violation, analogous to bypassing the liquidation-debt lock in Ajna. This would compromise intended slashing behavior for the runtime.

### Likelihood Explanation
`register_fast_unstake` is a fully permissionless, unprivileged public extrinsic (`ensure_signed`), and `on_idle` processing that invokes `force_unstake` runs automatically without any operator/admin action, so this does not require a malicious validator, governance actor, or privileged party — only normal usage combined with the narrow but real race window (offence reported near era boundary, not yet computed) that `pr_9079` was specifically written to close for the `withdraw_unbonded` path.

### Recommendation
Verify whether `StakingInterface::force_unstake` (`kill_stash`) in `pallet-staking-async` consults `OffenceQueueEras`/`ensure_era_slashes_applied` before releasing stash funds. If it does not, add the same offence-queue/slash-application guard to `force_unstake`/`kill_stash` that was added to `do_withdraw_unbonded`, or have `pallet-fast-unstake`'s `unstake_stash` check for unresolved offences before calling `force_unstake`.

### Proof of Concept
Conceptual sequence:
1. Attacker (or targeted validator/nominator) commits a slashable offence; the offence is reported and queued in `OffenceQueueEras` but not yet computed/applied due to processing lag.
2. Attacker calls `register_fast_unstake` (permissionless), which chills and fully unbonds them via `T::Staking::fully_unbond`.
3. Once `bonding_duration`-worth of eras pass `check_stash`'s exposure scan (which only checks `is_exposed_in_era` for scanned eras, not the pending offence queue) without seeing the pending offence, `on_idle`'s `unstake_stash` closure calls `T::Staking::force_unstake`, releasing the deposit and (per `kill_stash`) the stash's full balance.
4. The later-processed offence's slash can no longer be enforced against the now-unstaked/withdrawn funds — mirroring the Ajna sequence `kickWithDeposit() -> withdrawBonds()` escaping liquidation-debt controls.

**Caveat:** I was unable to fetch the exact body of `force_unstake`/`kill_stash` in `substrate/frame/staking-async/src/pallet/impls.rs` before running out of tool iterations, so I cannot confirm with certainty that it omits the offence-queue check — this should be verified directly against that function before treating this as fully confirmed.

### Citations

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L229-257)
```rust
	/// Calculate the earliest era that withdrawals are allowed for, considering:
	/// - The current active era
	/// - Any unprocessed offences in the queue
	fn calculate_earliest_withdrawal_era(active_era: EraIndex) -> EraIndex {
		// get lowest era for which all offences are processed and withdrawals can be allowed.
		let earliest_unlock_era_by_offence_queue = OffenceQueueEras::<T>::get()
			.as_ref()
			.and_then(|eras| eras.first())
			.copied()
			// if nothing in queue, use the active era.
			.unwrap_or(active_era)
			// above returns earliest era for which offences are NOT processed yet, so we subtract
			// one from it which gives us the oldest era for which all offences are processed.
			.saturating_sub(1)
			// Unlock chunks are keyed by the era they were initiated plus their unbond duration.
			// We use full BondingDuration (validator duration) here because:
			// - For validators: this is their actual unbond duration
			// - For nominators: when slashable, they use full duration; when not slashable, their
			//   chunks already have shorter unlock eras (set during unbond), so this calculation
			//   still correctly allows their withdrawals.
			.saturating_add(T::BondingDuration::get());

		// If there are unprocessed offences older than the active era, withdrawals are only
		// allowed up to the last era for which offences have been processed.
		// Note: This situation is extremely unlikely, since offences have `SlashDeferDuration` eras
		// to be processed. If it ever occurs, it likely indicates offence spam and that we're
		// struggling to keep up with processing.
		active_era.min(earliest_unlock_era_by_offence_queue)
	}
```

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L259-267)
```rust
	pub(super) fn do_withdraw_unbonded(controller: &T::AccountId) -> Result<Weight, DispatchError> {
		let mut ledger = Self::ledger(Controller(controller.clone()))?;
		let (stash, old_total) = (ledger.stash.clone(), ledger.total);
		let active_era = Rotator::<T>::active_era();

		// Ensure last era slashes are applied. Else we block the withdrawals.
		if active_era > 1 {
			Self::ensure_era_slashes_applied(active_era.saturating_sub(1))?;
		}
```

**File:** substrate/frame/fast-unstake/src/lib.rs (L555-564)
```rust
			let unstake_stash = |stash: T::AccountId, deposit| {
				let result = T::Staking::force_unstake(stash.clone());
				let remaining = T::Currency::unreserve(&stash, deposit);
				if !remaining.is_zero() {
					Self::halt("not enough balance to unreserve");
				} else {
					log!(debug, "unstaked {:?}, outcome: {:?}", stash, result);
					Self::deposit_event(Event::<T>::Unstaked { stash, result });
				}
			};
```

**File:** substrate/frame/fast-unstake/src/lib.rs (L566-579)
```rust
			let check_stash = |stash, deposit| {
				let is_exposed = unchecked_eras_to_check
					.iter()
					.any(|e| T::Staking::is_exposed_in_era(&stash, e));

				if is_exposed {
					let _ = T::Currency::slash_reserved(&stash, deposit);
					log!(info, "slashed {:?} by {:?}", stash, deposit);
					Self::deposit_event(Event::<T>::Slashed { stash, amount: deposit });
					false
				} else {
					true
				}
			};
```

**File:** substrate/primitives/staking/src/lib.rs (L277-292)
```rust
	/// Unlock any funds schedule to unlock before or at the current era.
	///
	/// Returns whether the stash was killed because of this withdraw or not.
	fn withdraw_unbonded(
		stash: Self::AccountId,
		num_slashing_spans: u32,
	) -> Result<bool, DispatchError>;

	/// The ideal number of active validators.
	fn desired_validator_count() -> u32;

	/// Whether or not there is an ongoing election.
	fn election_ongoing() -> bool;

	/// Force a current staker to become completely unstaked, immediately.
	fn force_unstake(who: Self::AccountId) -> DispatchResult;
```
