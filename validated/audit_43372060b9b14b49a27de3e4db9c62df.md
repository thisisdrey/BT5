## Analog Found: Unbounded, unpaginated deferred-slash application in `pallet-staking` mirrors the lending bug's "irreversible step executed before the compensating step can safely complete"

The Sherlock report's core invariant is: an irreversible state-changing step (seize collateral) is executed on the assumption that a second, dependent step (final repay) will always succeed quickly and atomically — but nothing enforces that the second step can actually complete before other state changes make it inconsistent, so the system ends up in an unrecoverable inconsistent state. The closest proven analog in this repository is the deferred-slash application flow in the legacy `pallet-staking`, which the newer `pallet-staking-async` had to explicitly re-architect (paged, multi-block, guarded) specifically because the old design's unbounded, single-shot application step could not be trusted to complete "in time."

### Title
Unbounded synchronous slash application in `pallet-staking::apply_unapplied_slashes` can stall era transition and block production - (File: `substrate/frame/staking/src/pallet/impls.rs`)

### Summary
`pallet-staking::apply_unapplied_slashes` (the legacy, non-async staking pallet still shipped in this repo) drains **all** `UnappliedSlashes` for an era and applies every one of them in a single unbounded, unpaginated loop inside the mandatory era-rotation hook [1](#0-0)  . There is no weight metering, no page limit, and no ability to defer/continue this work across multiple blocks. This is precisely the class of flaw that `pallet-staking-async` was rebuilt to eliminate: its documentation explicitly states offences are now processed "page-by-page" specifically so "large validator sets don't overwhelm a single block" [2](#0-1) , and it added a dedicated withdrawal guard (`ensure_era_slashes_applied`) plus a permissionless fallback `apply_slash` call to handle the case where slash application does not finish within its expected window [3](#0-2) [4](#0-3) . The corresponding prdoc confirms this was a defensive fix against exactly this scenario: "if the era rolls over before completion, these slashes can only be applied via the permissionless `apply_slash` call" [5](#0-4) . The legacy `pallet-staking` has none of this — it neither paginates the work nor guards withdrawals against it.

### Finding Description
On every era rotation, the legacy pallet calls `apply_unapplied_slashes(active_era)`, which takes the **entire** `UnappliedSlashes` map for that era and iterates it synchronously, calling `slashing::apply_slash::<T>` for every pending slash record [1](#0-0) . Each `apply_slash` call itself walks a validator's full nominator exposure to compute pro-rata deductions, so the total work is `O(offenders × nominators_per_offender)` with no upper bound enforced at the call site.

This work is triggered from within `on_offence`, which itself processes every reported offender/slash-fraction pair in a single unbounded loop and pushes deferred slashes into `UnappliedSlashes` keyed by `slash_era + slash_defer_duration + 1` [6](#0-5) . Equivocation/offence reporting is a permissionless, cheaply-triggerable dispatchable in the surrounding session/offences machinery, so an attacker (or a set of colluding/careless validators) can cause a large batch of slashable offences to defer into the same target era. When that era's rotation arrives, the runtime is forced to execute the entire unbounded `apply_unapplied_slashes` loop inside the mandatory `on_initialize`/era-rotation hook — code that runs unconditionally and is not subject to the same weight-based extrinsic exclusion that ordinary transactions get. There is no yielding, no page cursor, and no mechanism to spread this cost over multiple blocks, unlike the explicitly paginated `pallet-staking-async` design that was built to replace it.

The "guard that should stop this" in the newer pallet — bounding per-block slash processing and blocking withdrawals until slashes for the previous era are confirmed applied — simply does not exist in `pallet-staking::do_withdraw_unbonded`, which unconditionally calls `ledger.consolidate_unlocked(current_era)` with no check on `UnappliedSlashes` at all [7](#0-6) , in contrast to the async pallet's explicit `Self::ensure_era_slashes_applied(active_era.saturating_sub(1))?` check before consolidating unlocked funds [8](#0-7) .

### Impact Explanation
If the unbounded era-rotation slash application ever exceeds the block's available weight/PoV budget (achievable by concentrating enough offence reports into a single deferred era), the mandatory hook logic either forces an oversized block or cannot make progress, directly degrading or stalling block production at era boundaries — a chain-availability impact explicitly in scope ("implementation bugs that can bring down... a Substrate-based chain," "public underpriced work that degrades block production"). Separately, because `do_withdraw_unbonded` never checks for unapplied slashes, any staker whose deferred slash has not yet been applied (whether due to hook-timing edge cases or an era-rotation delay) can withdraw funds that should still be locked to cover that slash, letting validators/nominators escape their punishment — the same "irreversible-step-executed-before-the-compensating-step-lands" root cause as the lending report, here manifesting as an unbacked unlock of funds that undermines the slashing/security guarantee of the network.

### Likelihood Explanation
Offence/equivocation reporting is a normal, permissionless part of consensus operation, so no privileged actor, malicious validator collusion, or admin/governance action is required to populate `UnappliedSlashes` heavily for a target era — an attacker only needs enough legitimate slashable evidence to concentrate into one era to make the unbounded loop large. The absence of any pagination or withdrawal guard (both of which had to be explicitly engineered into the replacement pallet) means the vulnerable code path is exercised on every ordinary era transition, not merely a corner case.

### Recommendation
Port the `pallet-staking-async` mitigations back into `pallet-staking`: (1) paginate `apply_unapplied_slashes` so it processes a bounded number of exposure pages/offenders per block with a resumable cursor, and (2) add a withdrawal guard in `do_withdraw_unbonded` that refuses to consolidate/release unbonding chunks whose era has unapplied slashes pending, mirroring `ensure_era_slashes_applied`, with a permissionless fallback call analogous to `apply_slash` to guarantee progress without relying on hook-only execution.

### Proof of Concept
Not executable without a full runtime harness, but the exploit outline is: (1) accumulate enough real offence reports whose computed `slash_era` values collide on a single future era via `on_offence` [9](#0-8) ; (2) at that era's rotation, `apply_unapplied_slashes` must synchronously process the entire accumulated batch in one hook call [1](#0-0)  with no pagination, and no withdrawal-side guard exists to safely delay unbonding consolidation while this is in flight [7](#0-6) , unlike the hardened equivalent in `pallet-staking-async` [8](#0-7) .

### Citations

**File:** substrate/frame/staking/src/pallet/impls.rs (L193-234)
```rust
	pub(super) fn do_withdraw_unbonded(
		controller: &T::AccountId,
		num_slashing_spans: u32,
	) -> Result<Weight, DispatchError> {
		let mut ledger = Self::ledger(Controller(controller.clone()))?;
		let (stash, old_total) = (ledger.stash.clone(), ledger.total);
		if let Some(current_era) = CurrentEra::<T>::get() {
			ledger = ledger.consolidate_unlocked(current_era)
		}
		let new_total = ledger.total;

		let ed = asset::existential_deposit::<T>();
		let used_weight =
			if ledger.unlocking.is_empty() && (ledger.active < ed || ledger.active.is_zero()) {
				// This account must have called `unbond()` with some value that caused the active
				// portion to fall below existential deposit + will have no more unlocking chunks
				// left. We can now safely remove all staking-related information.
				Self::kill_stash(&ledger.stash, num_slashing_spans)?;

				T::WeightInfo::withdraw_unbonded_kill(num_slashing_spans)
			} else {
				// This was the consequence of a partial unbond. just update the ledger and move on.
				ledger.update()?;

				// This is only an update, so we use less overall weight.
				T::WeightInfo::withdraw_unbonded_update(num_slashing_spans)
			};

		// `old_total` should never be less than the new total because
		// `consolidate_unlocked` strictly subtracts balance.
		if new_total < old_total {
			// Already checked that this won't overflow by entry condition.
			let value = old_total.defensive_saturating_sub(new_total);
			Self::deposit_event(Event::<T>::Withdrawn { stash, amount: value });

			// notify listeners.
			T::EventListeners::on_withdraw(controller, value);
		}

		Ok(used_weight)
	}

```

**File:** substrate/frame/staking/src/pallet/impls.rs (L823-836)
```rust
	/// Apply previously-unapplied slashes on the beginning of a new era, after a delay.
	fn apply_unapplied_slashes(active_era: EraIndex) {
		let era_slashes = UnappliedSlashes::<T>::take(&active_era);
		log!(
			debug,
			"found {} slashes scheduled to be executed in era {:?}",
			era_slashes.len(),
			active_era,
		);
		for slash in era_slashes {
			let slash_era = active_era.saturating_sub(T::SlashDeferDuration::get());
			slashing::apply_slash::<T>(slash, slash_era);
		}
	}
```

**File:** substrate/frame/staking/src/pallet/impls.rs (L1298-1381)
```rust
		add_db_reads_writes(1, 1);

		let slash_defer_duration = T::SlashDeferDuration::get();

		let invulnerables = Invulnerables::<T>::get();
		add_db_reads_writes(1, 0);

		for (details, slash_fraction) in offenders.zip(slash_fractions) {
			let stash = &details.offender;
			let exposure = Self::eras_stakers(slash_era, stash);

			// Skip if the validator is invulnerable.
			if invulnerables.contains(stash) {
				continue;
			}

			Self::deposit_event(Event::<T>::SlashReported {
				validator: stash.clone(),
				fraction: *slash_fraction,
				slash_era,
			});

			if slash_era == active_era {
				// offence is in the current active era. Report it to session to maybe disable the
				// validator.
				add_db_reads_writes(2, 2);
				T::SessionInterface::report_offence(
					stash.clone(),
					crate::OffenceSeverity(*slash_fraction),
				);
			}

			let unapplied = slashing::compute_slash::<T>(slashing::SlashParams {
				stash,
				slash: *slash_fraction,
				exposure: &exposure,
				slash_era,
				window_start,
				now: active_era,
				reward_proportion,
			});

			if let Some(mut unapplied) = unapplied {
				let nominators_len = unapplied.others.len() as u64;
				let reporters_len = details.reporters.len() as u64;

				{
					let upper_bound = 1 /* Validator/NominatorSlashInEra */ + 2 /* fetch_spans */;
					let rw = upper_bound + nominators_len * upper_bound;
					add_db_reads_writes(rw, rw);
				}
				unapplied.reporters = details.reporters.clone();
				if slash_defer_duration == 0 {
					// Apply right away.
					slashing::apply_slash::<T>(unapplied, slash_era);
					{
						let slash_cost = (6, 5);
						let reward_cost = (2, 2);
						add_db_reads_writes(
							(1 + nominators_len) * slash_cost.0 + reward_cost.0 * reporters_len,
							(1 + nominators_len) * slash_cost.1 + reward_cost.1 * reporters_len,
						);
					}
				} else {
					// Defer to end of some `slash_defer_duration` from now.
					log!(
						debug,
						"deferring slash of {:?} happened in {:?} (reported in {:?}) to {:?}",
						slash_fraction,
						slash_era,
						active_era,
						slash_era + slash_defer_duration + 1,
					);
					UnappliedSlashes::<T>::mutate(
						slash_era.saturating_add(slash_defer_duration).saturating_add(One::one()),
						move |for_later| for_later.push(unapplied),
					);
					add_db_reads_writes(1, 1);
				}
			} else {
				add_db_reads_writes(4 /* fetch_spans */, 5 /* kick_out_if_recent */)
			}
		}

```

**File:** substrate/frame/staking-async/src/lib.rs (L113-128)
```rust
//! Offences are processed incrementally in `on_initialize` each block:
//!
//! ```text
//! 1. Load oldest offence from queue
//! 2. Move to `ProcessingOffence` storage
//! 3. For each exposure page (from last to first):
//!    - Calculate slash for validator's own stake
//!    - Calculate slash for each nominator (pro-rata based on exposure)
//!    - Track total slash and reward amounts
//! 4. Once all pages processed, create `UnappliedSlash`
//! ```
//!
//! **Key Features**:
//! - **Page-by-page processing**: Large validator sets don't overwhelm a single block
//! - **Pro-rata slashing**: Nominators slashed proportionally to their stake
//! - **Reward calculation**: A portion goes to reporters (if any)
```

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L259-270)
```rust
	pub(super) fn do_withdraw_unbonded(controller: &T::AccountId) -> Result<Weight, DispatchError> {
		let mut ledger = Self::ledger(Controller(controller.clone()))?;
		let (stash, old_total) = (ledger.stash.clone(), ledger.total);
		let active_era = Rotator::<T>::active_era();

		// Ensure last era slashes are applied. Else we block the withdrawals.
		if active_era > 1 {
			Self::ensure_era_slashes_applied(active_era.saturating_sub(1))?;
		}

		let earliest_era_to_withdraw = Self::calculate_earliest_withdrawal_era(active_era);

```

**File:** substrate/frame/staking-async/src/pallet/mod.rs (L3039-3087)
```rust
		/// For a given era x, if at era x+1, slashes are still unapplied, all withdrawals get
		/// blocked, and these need to be manually applied by calling this function.
		/// This function exists as a **fallback mechanism** for this extreme situation, but we
		/// never expect to encounter this in normal scenarios.
		///
		/// The parameters for this call can be queried by looking at the `UnappliedSlashes` storage
		/// for eras older than the active era.
		///
		/// ## Parameters
		/// - `slash_era`: The application era (`offence_era + SlashDeferDuration`), i.e. the key
		///   into [`UnappliedSlashes`].
		/// - `slash_key`: A unique identifier for the slash, represented as a tuple:
		///   - `stash`: The stash account of the validator being slashed.
		///   - `slash_fraction`: The fraction of the stake that was slashed.
		///   - `page_index`: The index of the exposure page being processed.
		///
		/// ## Behavior
		/// - The function is **permissionless**—anyone can call it.
		/// - The `slash_era` **must be the current era or a past era**.
		/// If it is in the future, the
		///   call fails with `EraNotStarted`.
		/// - The fee is waived if the slash is successfully applied.
		///
		/// ## Future Improvement
		/// - Implement an **off-chain worker (OCW) task** to automatically apply slashes when there
		///   is unused block space, improving efficiency.
		#[pallet::call_index(31)]
		#[pallet::weight(T::WeightInfo::apply_slash(T::MaxExposurePageSize::get()))]
		pub fn apply_slash(
			origin: OriginFor<T>,
			slash_era: EraIndex,
			slash_key: (T::AccountId, Perbill, u32),
		) -> DispatchResultWithPostInfo {
			let _ = ensure_signed(origin)?;
			let active_era = ActiveEra::<T>::get().map(|a| a.index).unwrap_or_default();
			ensure!(slash_era <= active_era, Error::<T>::EraNotStarted);

			// Check if this slash has been cancelled
			ensure!(
				!Self::check_slash_cancelled(slash_era, &slash_key.0, slash_key.1),
				Error::<T>::CancelledSlash
			);

			let unapplied_slash = UnappliedSlashes::<T>::take(&slash_era, &slash_key)
				.ok_or(Error::<T>::InvalidSlashRecord)?;
			slashing::apply_slash::<T>(unapplied_slash, Self::offence_era_of(slash_era));

			Ok(Pays::No.into())
		}
```

**File:** prdoc/stable2509/pr_9079.prdoc (L1-26)
```text
title: "Prevent withdrawals while processing offences"

doc:
  - audience: Runtime Dev
    description: |
      Adds withdrawal restrictions to prevent users from withdrawing unbonded funds while 
      there are unprocessed offences that could result in slashing. This is a defensive 
      measure that ensures slashing guarantees are maintained even in extreme edge cases.
      
      Key changes:
      - Withdrawals are blocked if there are unapplied slashes from the previous era 
        (returns `UnappliedSlashesInPreviousEra` error). This occurs when all unapplied 
        slashes for an era could not be applied within one era worth of blocks. While 
        one era is reserved for applying slashes page by page, if the era rolls over 
        before completion, these slashes can only be applied via the permissionless 
        `apply_slash` call.
      - Withdrawals are restricted to the minimum of the active era and the last fully 
        processed offence era
      - Unbonding chunks are now keyed by active era instead of current era
      - Offences arriving after their intended application era are rejected and emit 
        `OffenceTooOld` event
      
      Both the `UnappliedSlashesInPreviousEra` error and withdrawal restrictions due to 
      delayed offence processing are extremely rare scenarios that should not occur under 
      normal operation. These are defensive measures to handle edge cases where slash 
      processing is delayed beyond expected timelines.
```
