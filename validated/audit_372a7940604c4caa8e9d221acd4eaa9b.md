### Title
`pallet-referenda` marks a passed referendum as `Approved` even when scheduling its enactment call fails, silently dropping the approved action — ([File: substrate/frame/referenda/src/lib.rs])

### Summary
The external report's broken invariant is: *a public, attacker-triggerable action can permanently corrupt bookkeeping state when a nested call fails, because the outer function ignores/hides that failure instead of aborting the state transition.* The `endProposal()` DAO bug fails to roll back `activeProposalNow` when the enacted call reverts. The direct local analog is in `pallet-referenda`'s `schedule_enactment`, which relies on a `debug_assert!` to detect scheduling failure — a check that compiles to a no-op in production (`--release`) builds — so a referendum can be finalized as `Approved` while its enactment call is silently never scheduled.

### Finding Description
When a referendum finishes confirming, `service_referendum` calls `schedule_enactment` and then unconditionally returns `ReferendumInfo::Approved(...)`: [1](#0-0) 

`schedule_enactment` itself does: [2](#0-1) 

The result of `T::Scheduler::schedule_named(...)` is captured only as a boolean `ok` and checked with `debug_assert!(ok, "LOGIC ERROR: ...")`. `debug_assert!` is stripped out entirely in non-debug (release) builds, which is how production runtimes are compiled. This means:

- If `schedule_named` returns `Err` (e.g. the target block's `Agenda` in `pallet-scheduler` is already at `T::MaxScheduledPerBlock` capacity, or the derived named address collides), the error is discarded.
- The caller (`service_referendum`) has no way to know the scheduling failed — it proceeds to mark the referendum `Approved`, refund/settle deposits, and remove it from `ReferendumInfoFor`'s `Ongoing` state.
- The intended privileged call (treasury spend, runtime upgrade, parameter change, etc.) is never actually scheduled and there is no retry mechanism, unlike `pallet-scheduler`'s own `PermanentlyOverweight`/`IncompleteSince` recovery path that keeps failed-but-known tasks visible and re-triggerable.

The `desired` enactment block is attacker-influenceable: `submit()` lets any `SubmitOrigin` account choose `enactment_moment: DispatchTime<BlockNumberFor<T,I>>` for its own referendum: [3](#0-2) 

By submitting many referenda (paying only the `SubmissionDeposit`) targeting the identical `DispatchTime::At(block)` and getting enough of them through the vote/confirm process to occupy `pallet-scheduler`'s bounded `Agenda` for that exact block, an attacker can cause `schedule_named` to fail for a subsequent (potentially high-value) referendum whose enactment is due at the same block. Because of the `debug_assert!`-only guard, that referendum's approved call is dropped without any error, event, or retry — the referendum record simply becomes `Approved` in storage with no further action ever taken.

### Impact Explanation
This breaks the "state must only advance after dispatch/execution succeeds" invariant. A legitimate governance decision — including runtime upgrades, treasury spends, or other `Root`/privileged calls approved via referenda — can be silently and permanently lost with no on-chain signal that it failed (no `PermanentlyOverweight`/`CallUnavailable`-style event exists for this failure path, only a debug-only assertion). This is a correctness/DoS-style bug against governance execution: intended privileged actions never execute, and there is no automated recovery, unlike the scheduler's own overweight-task handling. Depending on what proposal was silently dropped (e.g. a treasury payout or a bug-fix runtime upgrade), this can translate into permanent loss of intended state changes / fund transfers that governance believed had been enacted.

### Likelihood Explanation
Triggering requires the attacker to get multiple low-cost referenda (bounded only by `SubmissionDeposit`) through voting/confirmation to converge on the same specific enactment block against a bounded `MaxScheduledPerBlock` scheduler agenda, similar in spirit to the original report's requirement of "enough voting power." It does not require any node/validator/relayer compromise, admin privilege, or leaked keys — it uses only the standard, public `submit`/vote flow. The complexity is non-trivial (requires coordination on timing/votes) but is a genuine unprivileged code path, and the guard that exists (`debug_assert!`) provides zero protection in production builds, which is the exact class of "guard doesn't stop the path" required.

### Recommendation
Replace the `debug_assert!(ok, ...)` in `schedule_enactment` with proper error propagation: `schedule_enactment` should return a `Result`, and `service_referendum`/its callers must not transition the referendum to `Approved` (nor release/slash deposits) unless the scheduling actually succeeded. On failure, either retry with a bumped block, queue the referendum similar to `TrackQueue`, or emit a durable, indexable event (e.g. `EnactmentFailed`) and keep the referendum in a recoverable state, mirroring `pallet-scheduler`'s handling of `PermanentlyOverweight`/postponed tasks.

### Proof of Concept
1. Deploy/observe a chain with `pallet-referenda` configured with `pallet-scheduler` and a modest `MaxScheduledPerBlock`.
2. Submit `N` referenda (`N` ≈ `MaxScheduledPerBlock`) each specifying `enactment_moment = DispatchTime::At(B)` for the same future block `B`, each proposing a low-impact call.
3. Ensure all `N` referenda place their decision deposit and pass their vote/confirmation so that they all reach the `Approved` branch and call `schedule_enactment` targeting block `B`, filling the `Agenda` at `B` in `pallet-scheduler` to `MaxScheduledPerBlock`.
4. Submit and pass one more, higher-value referendum (e.g. one that would execute a treasury spend or runtime call) with the same target block `B`.
5. When this referendum's `schedule_enactment` runs, `T::Scheduler::schedule_named` returns `Err` because the agenda for block `B` is full; in a release build the `debug_assert!` is a no-op.
6. Observe: `ReferendumInfoFor` for this referendum is set to `Approved`, deposits are refunded/handled as if enactment succeeded, but the actual call is never executed at block `B` (or ever) — confirmable by checking `pallet-scheduler::Agenda` for block `B` (does not contain the new task) and by the absence of the intended call's side effects (e.g. no balance transfer, no runtime upgrade).

Note: verifying the exact `pallet-scheduler` error type returned for a full/blocked named schedule at a specific block (e.g. `AgendaFull`/`Named`) requires reading `place_task`/`do_schedule_named` in `substrate/frame/scheduler/src/lib.rs`, which I was not able to fully inspect before running out of tool iterations; the core defect (production-stripped `debug_assert!` guarding an unhandled `Result` from `T::Scheduler::schedule_named`) is confirmed directly from the cited source.

### Citations

**File:** substrate/frame/referenda/src/lib.rs (L474-521)
```rust
		pub fn submit(
			origin: OriginFor<T>,
			proposal_origin: Box<PalletsOriginOf<T>>,
			proposal: BoundedCallOf<T, I>,
			enactment_moment: DispatchTime<BlockNumberFor<T, I>>,
		) -> DispatchResult {
			let proposal_origin = *proposal_origin;
			let who = T::SubmitOrigin::ensure_origin(origin, &proposal_origin)?;

			// If the pre-image is already stored, ensure that it has the same length as given in
			// `proposal`.
			if let (Some(preimage_len), Some(proposal_len)) =
				(proposal.lookup_hash().and_then(|h| T::Preimages::len(&h)), proposal.lookup_len())
			{
				if preimage_len != proposal_len {
					return Err(Error::<T, I>::PreimageStoredWithDifferentLength.into());
				}
			}

			let track =
				T::Tracks::track_for(&proposal_origin).map_err(|_| Error::<T, I>::NoTrack)?;
			let submission_deposit = Self::take_deposit(who, T::SubmissionDeposit::get())?;
			let index = ReferendumCount::<T, I>::mutate(|x| {
				let r = *x;
				*x += 1;
				r
			});
			let now = T::BlockNumberProvider::current_block_number();
			let nudge_call =
				T::Preimages::bound(CallOf::<T, I>::from(Call::nudge_referendum { index }))?;
			let status = ReferendumStatus {
				track,
				origin: proposal_origin,
				proposal: proposal.clone(),
				enactment: enactment_moment,
				submitted: now,
				submission_deposit,
				decision_deposit: None,
				deciding: None,
				tally: TallyOf::<T, I>::new(track),
				in_queue: false,
				alarm: Self::set_alarm(nudge_call, now.saturating_add(T::UndecidingTimeout::get())),
			};
			ReferendumInfoFor::<T, I>::insert(index, ReferendumInfo::Ongoing(status));

			Self::deposit_event(Event::<T, I>::Submitted { index, track, proposal });
			Ok(())
		}
```

**File:** substrate/frame/referenda/src/lib.rs (L905-927)
```rust
	// Enqueue a proposal from a referendum which has presumably passed.
	fn schedule_enactment(
		index: ReferendumIndex,
		track: &TrackInfoOf<T, I>,
		desired: DispatchTime<BlockNumberFor<T, I>>,
		origin: PalletsOriginOf<T>,
		call: BoundedCallOf<T, I>,
	) {
		let now = T::BlockNumberProvider::current_block_number();
		// Earliest allowed block is always at minimum the next block.
		let earliest_allowed = now.saturating_add(track.min_enactment_period.max(One::one()));
		let desired = desired.evaluate(now);
		let ok = T::Scheduler::schedule_named(
			(ASSEMBLY_ID, "enactment", index).using_encoded(sp_io::hashing::blake2_256),
			DispatchTime::At(desired.max(earliest_allowed)),
			None,
			63,
			origin,
			call,
		)
		.is_ok();
		debug_assert!(ok, "LOGIC ERROR: bake_referendum/schedule_named failed");
	}
```

**File:** substrate/frame/referenda/src/lib.rs (L1192-1212)
```rust
				branch = if is_passing {
					match deciding.confirming {
						Some(t) if now >= t => {
							// Passed!
							Self::ensure_no_alarm(&mut status);
							Self::note_one_fewer_deciding(status.track);
							let (desired, call) = (status.enactment, status.proposal);
							Self::schedule_enactment(index, &track, desired, status.origin, call);
							Self::deposit_event(Event::<T, I>::Confirmed {
								index,
								tally: status.tally,
							});
							return (
								ReferendumInfo::Approved(
									now,
									Some(status.submission_deposit),
									status.decision_deposit,
								),
								true,
								ServiceBranch::Approved,
							);
```
