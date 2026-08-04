### Title
Unbounded `UnappliedSlashes` queue causes unmetered, unweighted mass-slash processing that can stall era rotation - ([File: substrate/frame/staking/src/pallet/impls.rs])

### Summary
`pallet-staking`'s deferred-slashing mechanism accumulates an unbounded `Vec` of pending slashes per era with no cap on how many entries can be queued, then drains and applies *all* of them in a single, unweighted loop at the start of the next era. This mirrors the Market bug exactly: a queue that has no admission limit, whose consumer performs unbounded work per invocation on a path that cannot be skipped or deferred, creating a DoS/self-bricking risk instead of a controlled, weight-metered drain (as `pallet-staking-async`'s replacement design or `pallet-message-queue` correctly implement).

### Finding Description
`UnappliedSlashes` is declared as an unbounded, un-paginated storage map keyed by era: [1](#0-0) 

Every time an offence is processed with a non-zero `SlashDeferDuration`, the computed slash is pushed onto this `Vec` with **no limit check whatsoever**: [2](#0-1) 

`on_offence` (the function containing this push) is reachable for every offender reported in a session; the number of pushes per era is bounded only by the number of active validators/offences reported, not by any pallet-enforced ceiling — analogous to Market's `_invariant` skipping the pending-limit check for `protected` (liquidation) updates.

At the start of the era to which slashes were deferred, `start_era` unconditionally calls `apply_unapplied_slashes`: [3](#0-2) 

which `take()`s the *entire* `Vec` for that era and iterates over every entry in one unweighted loop, invoking `slashing::apply_slash` (which itself iterates all nominators of the exposure, e.g., up to `MaxNominatorRewardedPerValidator`) for each: [4](#0-3) 

This mirrors the Market pattern precisely:
- an admission path (`on_offence`/liquidation-style path) that bypasses the pending-limit check,
- a queue with no cap (`UnappliedSlashes`, analogous to `_pendingPosition`),
- a mandatory consumer (`start_era`, analogous to `_settle`) that must fully drain the queue in one unmetered pass before normal operation (era rotation / `update`) can continue.

Unlike `pallet-message-queue` (which explicitly uses a `WeightMeter` and "bails" mid-processing to guarantee `on_initialize` respects its weight limit — see the module docs at `substrate/frame/message-queue/src/lib.rs:89-97,169-182`) or the newer `pallet-staking-async` (which replaced this exact mechanism with paginated `OffenceQueue`/`UnappliedSlashes` processing one page per block — see `substrate/frame/staking-async/src/pallet/mod.rs:986-1020` and `apply_unapplied_slashes` there processing `.take(1)` per call at `substrate/frame/staking-async/src/pallet/mod.rs:1596-1636`), legacy `pallet-staking`'s `apply_unapplied_slashes` has no per-block/per-call bound and no ability to bail partway.

Notably, a fix analogous to `pallet-staking-async`'s paginated design was attempted for legacy `pallet-staking` in a PR titled "Bounded Slashing: Paginated Offence Processing & Slash Application," but its own prdoc records it was reverted: [5](#0-4) 

confirming the unbounded behavior in `pallet-staking` remains live in this codebase, not superseded.

### Impact Explanation
If enough offences accumulate for a single era (e.g., during a mass-slashing event across many validators, each with large nominator sets), `apply_unapplied_slashes` must synchronously process the entire backlog inside `start_era`, which itself runs inside the mandatory, non-skippable era-rotation path. Because this loop has no `WeightMeter`/bailing mechanism, it can consume arbitrarily large computation and storage-write volume in what is nominally a single block's mandatory hook. This can stall or fail block production/era rotation for the affected chain — the runtime equivalent of Market's "every `update` call reverts because `_settle` cannot complete," i.e., the chain cannot make progress past this era boundary without a forced runtime upgrade or intervention, which is a chain-availability DoS squarely in scope ("public underpriced work that degrades block production or stalls... processing").

### Likelihood Explanation
Triggering requires many offences to be reported and deferred within one era-length window (bounded by `SlashDeferDuration`), which needs either a large-scale equivocation/validity-attack event or a validator set that is broadly compromised/misbehaving — an external condition rather than a single unprivileged transaction, similar to the Sherlock panel's "medium, not high" conclusion for the Market bug (requires a specific, rare-but-possible state, not an attacker with a low-cost single call). It does not require a malicious peer/relayer/validator collusion to *exploit* — it can occur from normal but unusually large-scale offence reporting, matching the "no attacker required, protocol enters this state itself" characterization used to escalate the original issue to at least medium/high.

### Recommendation
Port the paginated approach already implemented in `pallet-staking-async` (`OffenceQueue`, `ProcessingOffence`, page-at-a-time `apply_unapplied_slashes`) into legacy `pallet-staking`, or reinstate/re-land the reverted bounded-slashing design from `pr_7424`, ensuring: (1) a cap on `UnappliedSlashes` entries admitted per era, and (2) that `apply_unapplied_slashes` processes at most a bounded number of slashes/nominators per invocation using a `WeightMeter`, deferring the remainder to subsequent blocks rather than requiring a single unbounded pass at era start.

### Proof of Concept
Conceptual reproduction (cannot be executed without a live chain/test harness in this session):
1. Configure `SlashDeferDuration > 0`.
2. Report offences for a large number of active validators (up to the validator set size) within the same era/slash window, each with near-maximum nominator exposure (`MaxNominatorRewardedPerValidator`).
3. Each report pushes an entry into `UnappliedSlashes` via `Pallet::<T>::on_offence` at `substrate/frame/staking/src/pallet/impls.rs:1371-1374` — no limit rejects any of these pushes.
4. When the era advances to the deferred era, `start_era` (`substrate/frame/staking/src/pallet/impls.rs:567`) calls `apply_unapplied_slashes`, which `take()`s and iterates the *entire* accumulated `Vec` in one call (`substrate/frame/staking/src/pallet/impls.rs:824-835`), performing `O(offenders × nominators_per_offender)` storage writes synchronously and without any weight-metered bailout, unlike `pallet-message-queue`'s `service_queue` (`substrate/frame/message-queue/src/lib.rs:1241-1252`) which explicitly `break`s on insufficient weight.
5. With enough queued slashes, this single call's execution time/weight can exceed practical block-production bounds, stalling era progression until manually remediated.

### Citations

**File:** substrate/frame/staking/src/pallet/mod.rs (L680-689)
```rust
	/// All unapplied slashes that are queued for later.
	#[pallet::storage]
	#[pallet::unbounded]
	pub type UnappliedSlashes<T: Config> = StorageMap<
		_,
		Twox64Concat,
		EraIndex,
		Vec<UnappliedSlash<T::AccountId, BalanceOf<T>>>,
		ValueQuery,
	>;
```

**File:** substrate/frame/staking/src/pallet/impls.rs (L529-568)
```rust
	/// Start a new era. It does:
	/// * Increment `active_era.index`,
	/// * reset `active_era.start`,
	/// * update `BondedEras` and apply slashes.
	fn start_era(start_session: SessionIndex) {
		let active_era = ActiveEra::<T>::mutate(|active_era| {
			let new_index = active_era.as_ref().map(|info| info.index + 1).unwrap_or(0);
			*active_era = Some(ActiveEraInfo {
				index: new_index,
				// Set new active era start in next `on_finalize`. To guarantee usage of `Time`
				start: None,
			});
			new_index
		});

		let bonding_duration = T::BondingDuration::get();

		BondedEras::<T>::mutate(|bonded| {
			bonded.push((active_era, start_session));

			if active_era > bonding_duration {
				let first_kept = active_era.defensive_saturating_sub(bonding_duration);

				// Prune out everything that's from before the first-kept index.
				let n_to_prune =
					bonded.iter().take_while(|&&(era_idx, _)| era_idx < first_kept).count();

				// Kill slashing metadata.
				for (pruned_era, _) in bonded.drain(..n_to_prune) {
					slashing::clear_era_metadata::<T>(pruned_era);
				}

				if let Some(&(_, first_session)) = bonded.first() {
					T::SessionInterface::prune_historical_up_to(first_session);
				}
			}
		});

		Self::apply_unapplied_slashes(active_era);
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

**File:** substrate/frame/staking/src/pallet/impls.rs (L1361-1376)
```rust
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
```

**File:** prdoc/stable2503/pr_7424.prdoc (L1-20)
```text
# Schema: Polkadot SDK PRDoc Schema (prdoc) v1.0.0
# See doc at https://raw.githubusercontent.com/paritytech/polkadot-sdk/master/prdoc/schema_user.json

title: 'Bounded Slashing: Paginated Offence Processing & Slash Application'

doc:
  - audience: Runtime Dev
    description: |
      NOTE: This is reverted in #7939.
      This PR refactors the slashing mechanism in `pallet-staking` to be bounded by introducing paged offence processing and paged slash application.

            ### Key Changes
            - Offences are queued instead of being processed immediately.
            - Slashes are computed in pages, stored as a `StorageDoubleMap` with `(Validator, SlashFraction, PageIndex)` to uniquely identify them.
            - Slashes are applied incrementally across multiple blocks instead of a single unbounded operation.
            - New storage items: `OffenceQueue`, `ProcessingOffence`, `OffenceQueueEras`.
            - Updated API for cancelling and applying slashes.
            - Preliminary benchmarks added; further optimizations planned.

            This enables staking slashing to scale efficiently and removes a major blocker for staking migration to a parachain (AH).
```
