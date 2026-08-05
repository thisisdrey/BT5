### Title
Low-cost `max_deciding` slot exhaustion permits indefinite governance-track hostage-taking - (File: `substrate/frame/referenda/src/lib.rs`)

### Summary
`pallet-referenda` caps the number of referenda that may be concurrently "deciding" on a track via `TrackInfo::max_deciding`, tracked in `DecidingCount`, with overflow parked in `TrackQueue`. Any unprivileged account that can afford a track's `decision_deposit` may submit and fund enough dummy referenda to fill every deciding slot on a track, exactly analogous to the Olympus governance report where a low-power actor occupies the sole activation slot and blocks all other proposals for a grace period.

### Finding Description
`ready_for_deciding` only allows a referendum to begin deciding if `DecidingCount::<T,I>::get(status.track) < track.max_deciding`; otherwise it is placed in `TrackQueue` and waits: [1](#0-0) 

The per-track limits are configured in runtime track tables, e.g. `small_tipper` allows `max_deciding: 200` for a `decision_deposit` of only `3 CENTS`, and `big_tipper` allows `max_deciding: 100` for `10*3 CENTS`: [2](#0-1) 

`TrackQueue`/`DecidingCount` storage and their documented invariant ("This should be empty if `DecidingCount` is less than `TrackInfo::max_deciding`") show that once all `max_deciding` slots are occupied, every further submission — including legitimate ones — is forced to wait in the queue until a slot is vacated: [3](#0-2) 

Once deciding begins, `begin_deciding` locks the slot for the track's `decision_period`/`confirm_period` regardless of whether the referendum is ultimately trivial or doomed to fail — the slot is only freed via `note_one_fewer_deciding`/`one_fewer_deciding` after the full decision timing elapses (or the referendum is administratively killed): [4](#0-3) [5](#0-4) 

Submission and decision-deposit placement are both public, unprivileged extrinsics gated only by paying the (often small) `decision_deposit`, not by any special role. An attacker with modest capital can therefore submit `max_deciding` dummy referenda on a cheap track (e.g. `small_tipper`), place the decision deposit on each, and occupy every slot for the whole `decision_period`. Any genuine referendum submitted afterward on the same track is shoved into `TrackQueue` and cannot begin deciding — and thus cannot be approved/enacted — until an attacker-held slot times out. Because decision deposits are refunded once a referendum concludes, the attacker can immediately resubmit fresh dummy referenda and re-occupy the freed slots, repeating the hold indefinitely with no growing cost, mirroring the "resubmit after `GRACE_PERIOD`" pattern in the Olympus report.

### Impact Explanation
This lets an unprivileged, non-governance actor durably stall an entire referenda track (e.g. treasury tipper/spender tracks, or any track with `max_deciding` reachable at its `decision_deposit`), preventing legitimate proposals — including time-sensitive treasury or administrative actions scoped to that track — from ever entering the decision phase. This is a public-entrypoint denial-of-service on runtime governance throughput, not a governance/admin-abuse scenario, since the attacker need not hold any privileged origin.

### Likelihood Explanation
Likelihood is moderate-to-high on tracks with low `decision_deposit` relative to `max_deciding` (e.g. `small_tipper`/`big_tipper`), since the total capital required to fill all slots is small and fully recoverable via deposit refund after each round, making the attack self-funding and repeatable. High-deposit/low-`max_deciding` tracks (e.g. `root`) are naturally more expensive but the mechanism is structurally identical across all tracks.

### Recommendation
Consider per-account or per-submission-origin rate limiting on concurrent deciding referenda per track, requiring escalating deposits for repeated submissions from the same origin within a track, or allowing legitimate high-support referenda to preempt low-support ones occupying deciding slots (the existing `TrackQueue` ordering by `tally.ayes` only helps once a slot frees, not while it is held) so a determined low-support attacker cannot indefinitely deny slots to higher-support proposals.

### Proof of Concept
1. Pick a track with a low `decision_deposit`/`max_deciding` ratio, e.g. `small_tipper` (`decision_deposit: 3 CENTS`, `max_deciding: 200`) as configured in `polkadot/runtime/rococo/src/governance/tracks.rs`.
2. From an unprivileged account, call `Referenda::submit` 200 times with a trivial/no-op proposal targeted at that track, then `Referenda::place_decision_deposit` on each, funding `200 * 3 CENTS` total.
3. `ready_for_deciding` admits all 200 into deciding state since `DecidingCount < max_deciding` for each, filling `DecidingCount[small_tipper] == max_deciding` per `substrate/frame/referenda/src/lib.rs:1003-1022`.
4. Any legitimate `small_tipper` referendum submitted afterward is routed to `TrackQueue` and cannot begin deciding until a slot frees at the end of `decision_period`.
5. When the attacker's referenda resolve, decision deposits are refunded; the attacker immediately resubmits fresh dummy referenda before genuine queued proposals can claim the freed slots, repeating the hold indefinitely.

### Citations

**File:** substrate/frame/referenda/src/lib.rs (L963-997)
```rust
	fn begin_deciding(
		status: &mut ReferendumStatusOf<T, I>,
		index: ReferendumIndex,
		now: BlockNumberFor<T, I>,
		track: &TrackInfoOf<T, I>,
	) -> (Option<BlockNumberFor<T, I>>, BeginDecidingBranch) {
		let is_passing = Self::is_passing(
			&status.tally,
			Zero::zero(),
			track.decision_period,
			&track.min_support,
			&track.min_approval,
			status.track,
		);
		status.in_queue = false;
		Self::deposit_event(Event::<T, I>::DecisionStarted {
			index,
			tally: status.tally.clone(),
			proposal: status.proposal.clone(),
			track: status.track,
		});
		let confirming = if is_passing {
			Self::deposit_event(Event::<T, I>::ConfirmStarted { index });
			Some(now.saturating_add(track.confirm_period))
		} else {
			None
		};
		let deciding_status = DecidingStatus { since: now, confirming };
		let alarm = Self::decision_time(&deciding_status, &status.tally, status.track, track)
			.max(now.saturating_add(One::one()));
		status.deciding = Some(deciding_status);
		let branch =
			if is_passing { BeginDecidingBranch::Passing } else { BeginDecidingBranch::Failing };
		(Some(alarm), branch)
	}
```

**File:** substrate/frame/referenda/src/lib.rs (L1003-1022)
```rust
	fn ready_for_deciding(
		now: BlockNumberFor<T, I>,
		track: &TrackInfoOf<T, I>,
		index: ReferendumIndex,
		status: &mut ReferendumStatusOf<T, I>,
	) -> (Option<BlockNumberFor<T, I>>, ServiceBranch) {
		let deciding_count = DecidingCount::<T, I>::get(status.track);
		if deciding_count < track.max_deciding {
			// Begin deciding.
			DecidingCount::<T, I>::insert(status.track, deciding_count.saturating_add(1));
			let r = Self::begin_deciding(status, index, now, track);
			(r.0, r.1.into())
		} else {
			// Add to queue.
			let item = (index, status.tally.ayes(status.track));
			status.in_queue = true;
			TrackQueue::<T, I>::mutate(status.track, |q| q.insert_sorted_by_key(item, |x| x.1));
			(None, ServiceBranch::Queued)
		}
	}
```

**File:** substrate/frame/referenda/src/lib.rs (L1038-1055)
```rust
	/// Schedule a call to `one_fewer_deciding` function via the dispatchable
	/// `defer_one_fewer_deciding`. We could theoretically call it immediately (and it would be
	/// overall more efficient), however the weights become rather less easy to measure.
	fn note_one_fewer_deciding(track: TrackIdOf<T, I>) {
		// Set an alarm call for the next block to nudge the track along.
		let now = T::BlockNumberProvider::current_block_number();
		let next_block = now + One::one();
		let call = match T::Preimages::bound(CallOf::<T, I>::from(Call::one_fewer_deciding {
			track,
		})) {
			Ok(c) => c,
			Err(_) => {
				debug_assert!(false, "Unable to create a bounded call from `one_fewer_deciding`??",);
				return;
			},
		};
		Self::set_alarm(call, next_block);
	}
```

**File:** polkadot/runtime/rococo/src/governance/tracks.rs (L213-239)
```rust
		id: 30,
		info: pallet_referenda::TrackInfo {
			name: s("small_tipper"),
			max_deciding: 200,
			decision_deposit: 1 * 3 * CENTS,
			prepare_period: 1 * MINUTES,
			decision_period: 14 * MINUTES,
			confirm_period: 4 * MINUTES,
			min_enactment_period: 1 * MINUTES,
			min_approval: APP_SMALL_TIPPER,
			min_support: SUP_SMALL_TIPPER,
		},
	},
	pallet_referenda::Track {
		id: 31,
		info: pallet_referenda::TrackInfo {
			name: s("big_tipper"),
			max_deciding: 100,
			decision_deposit: 10 * 3 * CENTS,
			prepare_period: 4 * MINUTES,
			decision_period: 14 * MINUTES,
			confirm_period: 12 * MINUTES,
			min_enactment_period: 3 * MINUTES,
			min_approval: APP_BIG_TIPPER,
			min_support: SUP_BIG_TIPPER,
		},
	},
```
