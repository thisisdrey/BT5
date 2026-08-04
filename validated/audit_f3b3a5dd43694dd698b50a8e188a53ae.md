### Title
`pallet-dap` `drip_issuance` permanently drops inflation whenever `elapsed > MaxElapsedPerDrip` because `LastIssuanceTimestamp` is advanced to `now` instead of `last + max_elapsed` - ([File: substrate/frame/dap/src/lib.rs])

### Summary
`pallet-dap`'s `drip_issuance` mints inflation as `IssuanceCurve::issue(total_issuance, elapsed)`, where `elapsed` is the time since the last drip clamped to a safety ceiling `MaxElapsedPerDrip`. This is the exact analog of the reported `calcShare` bug: elapsed time used for the payout computation is capped, so any accrual beyond the cap does not grow into more minted issuance. Unlike a well-formed cap, the pallet also unconditionally advances `LastIssuanceTimestamp` to `now` (the *uncapped* time) rather than to `last + max_elapsed` (the capped time actually accounted for), so the un-minted portion of elapsed time is silently and permanently discarded rather than deferred to the next drip.

### Finding Description
In `drip_issuance`: [1](#0-0) 

```
let now: u64 = now_moment.saturated_into();
let last = LastIssuanceTimestamp::<T>::get();
let mut elapsed = now.saturating_sub(last);
...
let max_elapsed = T::MaxElapsedPerDrip::get();
if elapsed > max_elapsed {
    Self::deposit_event(Event::Unexpected(UnexpectedKind::ElapsedClamped { ... }));
    elapsed = max_elapsed;
}

// Always advance the clock so elapsed time doesn't accumulate across skipped drips.
LastIssuanceTimestamp::<T>::put(now);

let _ = Self::mint_and_distribute(elapsed);
```

`mint_and_distribute(elapsed)` computes `T::IssuanceCurve::issue(total_issuance, elapsed)` and distributes it to budget recipients (staker rewards, treasury/buffer, etc.) proportionally to the clamped `elapsed`, not the real elapsed time.

The invariant that should hold is: total minted inflation over any window of real time should correspond to that real elapsed time (subject to intentional ceilings only meant to bound a single call's *risk*, not to permanently zero-out accrual). Instead:

1. `elapsed` used for minting is capped at `max_elapsed`.
2. `LastIssuanceTimestamp` is set to the *actual* `now`, not `last + max_elapsed`.

This means the delta `now - (last + max_elapsed)` — i.e., the excess time beyond the ceiling — is neither minted now nor carried forward to be minted on a subsequent drip. It vanishes. This exactly mirrors the reported bug's core broken invariant: "interest/accrual computed from a time-capped share function stops growing past the cap," except here the loss is worse because the tracked timestamp is *also* jumped forward past the un-accounted window, making the loss permanent and unrecoverable rather than merely deferred.

The repository's own `prdoc/stable2606/pr_11809.prdoc` confirms this exact failure mode was already identified for the V1→V2 migration seeding path ("elapsed is then clamped by `MaxElapsedPerDrip`, so only up to one cap's worth of inflation is actually credited on the first drip, and the rest is silently dropped") and a one-shot catch-up migration was added to fix that one specific case: [2](#0-1) 

However, that fix only addresses the migration's initial seeding; the regular `on_initialize`-driven `drip_issuance` path retains the identical clamp-and-jump-forward pattern for *every* subsequent drip, and is exercised directly by the pallet's own test: [3](#0-2) 

This test (`elapsed_ceiling_is_applied`) explicitly demonstrates that a 20-minute gap with a 10-minute ceiling mints only the 10-minute worth of issuance (1000 instead of 2000) and confirms `ElapsedClamped` fires — but it does not (and the code does not) recover the other 10 minutes of foregone inflation on any future drip, because `LastIssuanceTimestamp` was already advanced to `now`.

`on_initialize` is the sole caller of `drip_issuance`, and there is no privileged/attacker action required — any period in which block production stalls, is throttled, or intervals between drips otherwise exceed `MaxElapsedPerDrip` (e.g. chain downtime, congestion, or a deliberately low `MaxElapsedPerDrip` configuration) will trigger this loss automatically and silently for every affected recipient (buffer/treasury, staker reward pot, validator incentive pot), each time `elapsed` exceeds the ceiling. [4](#0-3) 

### Impact Explanation
This is a runtime bug that compromises intended issuance/payout behavior: budget recipients (staking rewards pot, treasury/buffer, validator incentive pot) permanently and silently receive less inflation than the configured `IssuanceCurve` intends whenever a gap between drips exceeds `MaxElapsedPerDrip`. Because the timestamp marker (`LastIssuanceTimestamp`) is advanced past the un-minted window rather than to the boundary of what was actually minted, the shortfall can never be caught up automatically — a governance-driven one-off migration (as done for V1→V2) is the only remedy, and it must be re-applied by hand every time this situation recurs. This is a "payout state advancing past what was actually settled" defect, i.e., permanent, protocol-wide under-payout of intended inflation with no attacker or privileged actor required — it happens purely from elapsed wall-clock time exceeding a governance-set constant during normal chain operation (e.g., an outage, throttled block production, or a small `MaxElapsedPerDrip`).

### Likelihood Explanation
Likelihood is non-trivial and does not require any adversarial action: any real-world chain stall, node/network outage, or misconfiguration of `IssuanceCadence`/`MaxElapsedPerDrip` relative to actual block-time variance triggers this path automatically on the very next `on_initialize`. The pallet's own integrity check only asserts `MaxElapsedPerDrip > IssuanceCadence`, not that it comfortably covers realistic outage windows, and the pallet authors clearly recognized this exact failure mode for the migration path (per `pr_11809.prdoc`) but left the general drip path with the same defect.

### Recommendation
When `elapsed` is clamped to `max_elapsed`, advance `LastIssuanceTimestamp` only by the clamped amount (`last.saturating_add(max_elapsed)`) rather than to the full `now`. This preserves the un-minted remainder as outstanding elapsed time to be credited (subject to the ceiling again) on the next drip, matching the "catch-up" semantics already implemented for the migration path, instead of silently discarding it. Alternatively, mint the un-minted delta as backlog state or emit a persistent deficit event that governance can settle deliberately, and ensure `try_state`/`do_try_state` validates that no unaccounted elapsed time is dropped between `LastIssuanceTimestamp` and `now`.

### Proof of Concept
Using the existing pallet test harness (`substrate/frame/dap/src/tests/drip.rs`):
1. Configure `MaxElapsedPerDrip = 600_000` ms (10 minutes) and `IssuanceCadence` smaller than that.
2. Set 100% budget allocation to `buffer`.
3. Advance mock time by 1,200,000 ms (20 minutes) and call `drip_issuance` (as the existing `elapsed_ceiling_is_applied` test does).
4. Observe: only 1000 units minted (elapsed clamped to 10 minutes) instead of 2000 units that the `IssuanceCurve` would have minted for the full 20-minute window — confirmed by the assertions in `elapsed_ceiling_is_applied`.
5. Advance a further normal cadence-length period and call `drip_issuance` again. Observe `LastIssuanceTimestamp` was already set to the full `now` from step 3, so the missing 10 minutes of accrual from step 3 is never minted in this or any subsequent call — it is permanently lost, with no code path to recover it outside of a manual, ad hoc storage migration like `MigrateV1ToV2` (`substrate/frame/dap/src/migrations.rs`).

### Citations

**File:** substrate/frame/dap/src/lib.rs (L124-146)
```rust
		/// Minimum elapsed time (ms) between issuance drips.
		///
		/// - `0` = drip every block
		/// - `60_000` = drip every minute (Recommended)
		///
		/// Should be small relative to era length.
		#[pallet::constant]
		type IssuanceCadence: Get<u64>;

		/// Safety ceiling: maximum elapsed time (ms) considered in a single drip.
		///
		/// If more time has passed than this, elapsed is clamped to this value.
		/// Prevents accidental over-minting from bugs, misconfiguration, or long
		/// periods without blocks.
		#[pallet::constant]
		type MaxElapsedPerDrip: Get<u64>;

		/// Origin that can update budget allocation percentages.
		type BudgetOrigin: EnsureOrigin<Self::RuntimeOrigin>;

		/// Weight information for extrinsics in this pallet.
		type WeightInfo: crate::weights::WeightInfo;
	}
```

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
