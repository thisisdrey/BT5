## Summary

The KintoID bug's core pattern is: **a single global "last-updated" timestamp gates a security/behavior invariant, but the invariant that should always progress in lock-step with real elapsed time is instead allowed to silently drift or be clamped, and the timestamp is advanced unconditionally regardless of whether the corresponding state was actually kept correct.** The local analog to this pattern is `pallet-dap`'s `LastIssuanceTimestamp` / `drip_issuance()` logic, which gates and "settles" chain-wide token issuance (staking rewards, validator incentive, treasury/buffer share) for every recipient.

## Finding Description

`LastIssuanceTimestamp` is the sole state tracking how much time has "been credited" toward inflation. It is only ever advanced from one place, `drip_issuance()`: [1](#0-0) 

Key defect: the elapsed time used for minting is clamped by `T::MaxElapsedPerDrip::get()` when it exceeds the ceiling (lines 385-392), but `LastIssuanceTimestamp` is then set to the *actual* `now` (line 395), not to `last + max_elapsed`. Any elapsed time beyond the ceiling is **silently and permanently dropped** — there is no accounting mechanism to "carry forward" the un-minted remainder, and the clock hand-off guarantees the next drip starts counting from `now`, so that lost window can never be recovered.

Additionally, `mint_and_distribute()` does not roll back or retry on a per-recipient mint failure: [2](#0-1) 

Because `LastIssuanceTimestamp` was already advanced in `drip_issuance()` *before* the mint outcome is known, a failed `mint_into` for any recipient (staking pot, validator incentive pot, treasury buffer) permanently loses that recipient's share for that elapsed window — the pallet only emits `Event::MintFailed`/`defensive!` and moves on; there's no other public or privileged call that can re-drip the lost window, exactly mirroring the KintoID situation where no other function keeps the gating timestamp/state consistent once the "monitor" cadence is missed.

This is not purely a migration-only issue: the project's own prdoc confirms this exact "silently drop the rest" behavior was observed once already, at the V1→V2 migration seed point: [3](#0-2) 

but the fix in `migrations.rs` only special-cases the *one-time migration seeding* scenario (`InnerMigrateV1ToV2`) by doing a catch-up mint and re-seeding `LastIssuanceTimestamp = now`: [4](#0-3) 

The underlying `drip_issuance()` clamp-and-advance behavior in normal, ongoing block production was **not** fixed — any real-world gap in block production (network outage, long governance-driven pause, runtime upgrade freeze, or simply `on_initialize` not being reached during any halted period) longer than `MaxElapsedPerDrip` (600_000 ms / 10 minutes on Asset Hub Westend and the staking-async parachain runtime) reproduces the exact same "silently drop the rest" defect on every subsequent occurrence, not just once at migration time. [5](#0-4) 

## Impact Explanation

This breaks the "conserve value and settle exactly once" invariant for runtime issuance: `LastIssuanceTimestamp` state (the payout/settlement marker) advances unconditionally regardless of whether the intended inflation was actually credited to the rightful beneficiaries (stakers, validators, treasury). Any halt or delay beyond `MaxElapsedPerDrip` permanently and silently under-pays every registered `BudgetRecipient` for that period, with no recovery path and no alerting beyond a low-visibility event — this is a runtime bug compromising intended reward/inflation behavior for the whole chain, degrading staking incentives and treasury funding chain-wide.

## Likelihood Explanation

No malicious actor, governance abuse, or privileged action is required — this triggers deterministically any time real elapsed time between successful `on_initialize` executions of `pallet-dap` exceeds `MaxElapsedPerDrip` (10 minutes in the wired configs), which can occur from ordinary operational events (network partition, collator outage, runtime-upgrade freeze window, or a temporarily halted parachain). This is a pure implementation bug in the pallet's core hook logic, always reachable via normal on_initialize execution once the described gap occurs.

## Recommendation

Do not advance `LastIssuanceTimestamp` past `last + max_elapsed` when the elapsed time is clamped — instead advance it to `last.saturating_add(max_elapsed)`, leaving the remainder outstanding so subsequent drips continue to credit it (mirroring the bug report's recommendation to keep the gating timestamp consistent with the real state it protects, rather than jumping straight to `now`). Additionally, make `mint_and_distribute` failures retryable/accounted for (e.g., accrue a pending-mint balance per recipient) instead of only emitting `MintFailed` and discarding the shortfall.

## Proof of Concept

1. Configure `IssuanceCadence = 60_000` and `MaxElapsedPerDrip = 600_000` as in the Asset Hub Westend runtime.
2. Let block production halt (e.g., simulate governance pause / runtime-upgrade freeze / any on_initialize skip) for 30 minutes of wall-clock/`pallet_timestamp` time.
3. On resumption, `drip_issuance()` runs: `elapsed = now - last = 1_800_000`ms, clamped to `max_elapsed = 600_000`ms (lines 385-392 of `substrate/frame/dap/src/lib.rs`).
4. `LastIssuanceTimestamp` is set to `now` (line 395), discarding the remaining 1_200_000ms of inflation forever — verifiable via `mint_and_distribute(600_000)` minting only 1/3 of what a continuous-inflation model would have credited, with no subsequent drip ever re-crediting the missing 1_200_000ms window.

### Citations

**File:** substrate/frame/dap/src/lib.rs (L365-399)
```rust
		pub(crate) fn drip_issuance() -> Weight {
			let now_moment = T::Time::now();
			let now: u64 = now_moment.saturated_into();
			let last = LastIssuanceTimestamp::<T>::get();
			let mut elapsed = now.saturating_sub(last);

			let cadence = T::IssuanceCadence::get();
			if cadence > 0 && elapsed < cadence {
				return T::DbWeight::get().reads(2);
			}

			// First block after genesis: initialize timestamp, don't drip.
			// For existing chains, use `migrations::MigrateV1ToV2` to seed this
			// value from ActiveEra.start so this branch is never hit post-upgrade.
			if last == 0 {
				LastIssuanceTimestamp::<T>::put(now);
				return T::DbWeight::get().reads_writes(2, 2);
			}

			// Apply safety ceiling on elapsed time.
			let max_elapsed = T::MaxElapsedPerDrip::get();
			if elapsed > max_elapsed {
				Self::deposit_event(Event::Unexpected(UnexpectedKind::ElapsedClamped {
					actual_elapsed: elapsed,
					ceiling: max_elapsed,
				}));
				elapsed = max_elapsed;
			}

			// Always advance the clock so elapsed time doesn't accumulate across skipped drips.
			LastIssuanceTimestamp::<T>::put(now);

			let _ = Self::mint_and_distribute(elapsed);
			T::WeightInfo::drip_issuance()
		}
```

**File:** substrate/frame/dap/src/lib.rs (L432-446)
```rust
			for (key, account) in &recipients {
				let perbill = budget.get(key).copied().unwrap_or(Perbill::zero());
				let amount = perbill.mul_floor(issuance);
				if !amount.is_zero() {
					if let Err(_) = T::Currency::mint_into(account, amount) {
						Self::deposit_event(Event::Unexpected(UnexpectedKind::MintFailed));
						defensive!("Issuance mint should not fail");
					} else {
						total_minted = total_minted.saturating_add(amount);
						if *account == buffer {
							Self::deactivate_buffer_funds(amount);
						}
					}
				}
			}
```

**File:** prdoc/stable2606/pr_11809.prdoc (L1-13)
```text
title: '[DAP] Catch-up drip on V1->V2 migration'
doc:
- audience: Runtime Dev
  description: |-
    The DAP V2 migration seeded `LastIssuanceTimestamp` to a point in the past
    (typically the active era start) so the next regular drip would credit
    elapsed time back to that point. That elapsed is then clamped by
    `MaxElapsedPerDrip`, so only up to one cap's worth of inflation is actually
    credited on the first drip, and the rest is silently dropped.

    This migration now performs a one-shot catch-up drip for the full
    `[last_inflation, now]` window and seeds `LastIssuanceTimestamp` to `now`, so
    regular drips start a fresh cadence from this point.
```

**File:** substrate/frame/dap/src/migrations.rs (L53-99)
```rust
{
	fn on_runtime_upgrade() -> frame_support::weights::Weight {
		let mut weight = T::DbWeight::get().reads(3);

		// Seed BudgetAllocation first so the catch-up drip has recipients to distribute to.
		let current_budget = BudgetAllocation::<T>::get();
		if current_budget.is_empty() {
			BudgetAllocation::<T>::put(B::get());
			weight = weight.saturating_add(T::DbWeight::get().writes(1));
			log::info!(target: LOG_TARGET, "Initialized BudgetAllocation with default budget");
		}

		let now: u64 = T::Time::now().saturated_into();

		// Only inflate if `LastIssuanceTimestamp` not set.
		if !LastIssuanceTimestamp::<T>::get().is_zero() {
			log::warn!(
				target: LOG_TARGET,
				"DAP V1->V2: LastIssuanceTimestamp already set; skipping catch-up drip"
			);
			return weight;
		}

		let last_inflation = P::get();
		let raw_elapsed = now.saturating_sub(last_inflation);
		let elapsed = raw_elapsed.min(M::get());
		if elapsed < raw_elapsed {
			log::info!(
				target: LOG_TARGET,
				"DAP V1->V2: elapsed {raw_elapsed}ms clamped to bound {elapsed}ms"
			);
		}
		let minted = pallet::Pallet::<T>::mint_and_distribute(elapsed);
		weight = weight.saturating_add(<T as Config>::WeightInfo::drip_issuance());

		// Regular drips resume from `now`.
		LastIssuanceTimestamp::<T>::put(now);
		weight = weight.saturating_add(T::DbWeight::get().writes(1));

		log::info!(
			target: LOG_TARGET,
			"DAP V1->V2: elapsed={elapsed}ms, total_minted={minted:?}, \
			 seeded LastIssuanceTimestamp={now}"
		);

		weight
	}
```

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/src/staking.rs (L355-361)
```rust
parameter_types! {
	pub const DapPalletId: frame_support::PalletId = pallet_dap::DAP_PALLET_ID;
	/// Minimum time (ms) between issuance drips. 60s = drip at most once per minute.
	pub const IssuanceCadence: u64 = 60_000;
	/// Safety ceiling (ms) for elapsed time in a single drip. Prevents over-minting after stalls.
	pub const MaxElapsedPerDrip: u64 = 600_000;
}
```
