### Title
`drip_issuance()` permanently drops inflation for the excess window beyond `MaxElapsedPerDrip`, causing lost issuance/rewards on every clamped drip — ([File: substrate/frame/dap/src/lib.rs])

### Summary
`pallet-dap`'s `drip_issuance` (the runtime analog of `_computeDripAmount()`) computes `elapsed = now - LastIssuanceTimestamp`, clamps it to `MaxElapsedPerDrip` when it exceeds the ceiling, mints/distributes based on the *clamped* value, but then unconditionally advances `LastIssuanceTimestamp` to `now` (the *unclamped* time) rather than to `last + elapsed_used`. Exactly like the `RewardsDistributor._computeDripAmount()` bug, the un-credited remainder of the time window (`raw_elapsed - max_elapsed`) is silently discarded and never distributed in any future drip.

### Finding Description
`drip_issuance()` in `substrate/frame/dap/src/lib.rs`:

```rust
pub(crate) fn drip_issuance() -> Weight {
    let now_moment = T::Time::now();
    let now: u64 = now_moment.saturated_into();
    let last = LastIssuanceTimestamp::<T>::get();
    let mut elapsed = now.saturating_sub(last);
    ...
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
``` [1](#0-0) 

This is structurally identical to the reported Solidity bug: a time-window computation truncates the credited interval to a cap, but instead of moving the "last processed" anchor forward only by the credited amount (`last + max_elapsed`), it snaps the anchor all the way to `now`. Any elapsed time beyond the cap (`raw_elapsed - max_elapsed`) is therefore never re-processed on a subsequent drip — it is permanently lost, exactly as `_computeDripAmount()` returning `0` at `timeLeft == 0` permanently stranded `rewardsLeftToDistribute`.

The project's own prdoc for the equivalent migration bug confirms this exact failure mode is a known, real issue class in this pallet:

> "The DAP V2 migration seeded `LastIssuanceTimestamp` to a point in the past ... That elapsed is then clamped by `MaxElapsedPerDrip`, so only up to one cap's worth of inflation is actually credited on the first drip, and the rest is silently dropped." [2](#0-1) 

The fix that was actually shipped only addresses the **one-time migration** path (`InnerMigrateV1ToV2`), which performs a bounded one-shot catch-up and then re-anchors `LastIssuanceTimestamp` to `now` deliberately (since the catch-up already consumed the whole backlog):

```rust
let raw_elapsed = now.saturating_sub(last_inflation);
let elapsed = raw_elapsed.min(M::get());
...
let minted = pallet::Pallet::<T>::mint_and_distribute(elapsed);
...
// Regular drips resume from `now`.
LastIssuanceTimestamp::<T>::put(now);
``` [3](#0-2) 

However, the **regular** `on_initialize`-driven `drip_issuance()` path (`substrate/frame/dap/src/lib.rs:364-399`) still has the identical clamp-then-jump-to-`now` pattern, and unlike the migration it has no "one-shot" justification — every single time it is triggered with `raw_elapsed > MaxElapsedPerDrip`, the excess is lost forever, not just once. This can occur any time block production is delayed past `MaxElapsedPerDrip` (network/relay-chain stall, collator outage, congested parachain, etc.) — a scenario with no malicious actor required, since `drip_issuance` runs unconditionally from `on_initialize`.

### Impact Explanation
This corrupts `LastIssuanceTimestamp` (the accrual anchor) to a value inconsistent with the amount actually minted, so a portion of intended token issuance for stakers/incentive/buffer recipients (`BudgetAllocation` split in `mint_and_distribute`) is permanently and silently never minted. This is a runtime bug that compromises intended economic behavior of the chain (under-issuance relative to spec) — falling under "runtime bugs that compromise intended behavior" in the impact gate. It is not a fund-theft/duplicate-payout issue, but it is a genuine, permanent loss of intended reward accrual with no recovery path, affecting every registered recipient's share proportionally (stakers, validator incentive pot, treasury/buffer).

### Likelihood Explanation
Any sustained block-production delay exceeding `MaxElapsedPerDrip` (chain halt/incident, congestion, migration/relaunch, or simply infrequent `on_initialize` execution under adverse conditions) will trigger the clamp branch with no attacker involvement. The pallet's own `elapsed_ceiling_is_applied` test demonstrates the clamp is reachable in normal operation, and the dedicated migration fix for the analogous startup case shows the maintainers were already aware this exact failure mode is a real, non-theoretical concern — they just didn't apply the equivalent "credit only up to what you consumed" correction to the ongoing `drip_issuance` path.

### Recommendation
In `drip_issuance()`, advance `LastIssuanceTimestamp` by the amount of elapsed time actually consumed for minting, not to `now`:
```rust
LastIssuanceTimestamp::<T>::put(last.saturating_add(elapsed));
```
This preserves the "safety ceiling per call" property (bounding how much can be minted in one hook invocation) while ensuring any un-credited backlog remains queued and gets picked up (subject to the same per-call ceiling) on the very next `on_initialize`, rather than being silently discarded.

### Proof of Concept
1. Configure `MaxElapsedPerDrip = 600_000ms` (10 min) and `IssuanceCadence` small enough to always be satisfied.
2. Let real chain time elapse without triggering `on_initialize`/drip for 30 minutes (simulate via `MockTime::set`), then call `drip_issuance()` once.
3. Observe `ElapsedClamped { actual_elapsed: 1_800_000, ceiling: 600_000 }` emitted and `mint_and_distribute(600_000)` executed — only 1/3 of the true elapsed window is minted.
4. Observe `LastIssuanceTimestamp` is set to the *current* `now` (30 min mark), not `last + 600_000` (10 min mark).
5. Call `drip_issuance()` again immediately: `elapsed = now - LastIssuanceTimestamp = 0`, so no further minting occurs for the remaining 20 minutes' worth of inflation — it is gone permanently, matching the `elapsed_ceiling_is_applied` test pattern at `substrate/frame/dap/src/tests/drip.rs:139-168` but showing the loss is never recovered on subsequent drips. [4](#0-3)

### Citations

**File:** substrate/frame/dap/src/lib.rs (L364-399)
```rust
		/// Core issuance drip logic, called from `on_initialize`.
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

**File:** substrate/frame/dap/src/migrations.rs (L76-89)
```rust
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
```

**File:** substrate/frame/dap/src/tests/drip.rs (L139-168)
```rust
#[test]
fn elapsed_ceiling_is_applied() {
	build_and_execute(true, || {
		System::set_block_number(1);

		// Set 100% to buffer.
		let allocs = budget_map(&[(b"buffer", 100)]);
		assert_ok!(Dap::set_budget_allocation(RuntimeOrigin::root(), allocs));

		let buffer = Dap::buffer_account();
		let buffer_before = Balances::balance(&buffer);

		// WHEN: 20 minutes pass but MaxElapsedPerDrip = 600_000ms (10 minutes)
		// Without clamping: 1_200_000ms → TestIssuanceCurve returns 2000
		// With clamping: 600_000ms → TestIssuanceCurve returns 1000
		advance_time_and_drip(1_200_000);

		// THEN: issuance based on clamped elapsed (1000, not 2000)
		assert_eq!(Balances::balance(&buffer) - buffer_before, 1000);

		// AND: ElapsedClamped event emitted
		System::assert_has_event(
			Event::<Test>::Unexpected(crate::UnexpectedKind::ElapsedClamped {
				actual_elapsed: 1_200_000,
				ceiling: 600_000,
			})
			.into(),
		);
	});
}
```
