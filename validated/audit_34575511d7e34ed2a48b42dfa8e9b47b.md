### Title
Referendum pass/fail decisions use a live `total_issuance` denominator that any accountholder can shrink mid-decision to flip `support` — ([File: substrate/frame/conviction-voting/src/types.rs])

### Summary
The external report's core broken invariant is: a governance outcome threshold is computed against a "max weight" denominator (total token supply) that is *not fixed* at vote-close time, so shrinking that denominator after votes are locked in but before finalization changes the computed outcome without any new genuine votes. The same broken invariant exists in `pallet-conviction-voting`'s `Tally::support`, which is consumed live by `pallet-referenda`'s deciding/confirming state machine on every scheduled re-evaluation, not just once when the poll closes.

### Finding Description
`Tally::support` computes the support ratio directly against the live total issuance: [1](#0-0) 

`Total` is bound via `type MaxTurnout = frame_support::traits::TotalIssuanceOf<Balances, AccountId>` in the pallet config, i.e. it reads `pallet_balances::TotalIssuance` at call time: [2](#0-1) 

`pallet-referenda` does not snapshot this ratio once — it recomputes `support`/`approval` from the live tally *every time* `service_referendum` runs (triggered by scheduler alarms set via `nudge_referendum`, throughout the whole Deciding/Confirming window, not only at submission or at a single close event): [3](#0-2) 

The decision of whether a referendum is "Passing" (and eventually gets `Confirmed`/`Approved`) is `is_passing`, which folds elapsed time into `Perbill` and directly compares against `tally.support(id)`: [4](#0-3) 

Because `support(id) = Perbill::from_rational(self.support, Total::get())`, shrinking `Total::get()` (total issuance) at any point while the referendum is still `Ongoing`/`Deciding` increases the reported support ratio for the exact same absolute aye-capital already tallied — exactly mirroring the external report's "burn to lower max_voter_weight, then finalize" primitive, except here the recomputation happens continuously via the alarm-driven `nudge_referendum`/`service_referendum` path rather than a single manual "FinalizeVote" call.

No lock exists on `TotalIssuance` for the duration of a referendum's decision, and unlike the SPL-governance fix referenced in the report (disallow relinquish/withdraw during Finalizing), there is no analogous freeze of the issuance denominator during the Deciding/Confirming window in `pallet-referenda`/`pallet-conviction-voting`.

### Impact Explanation
This lets governance outcomes be manipulated by any unprivileged tokenholder (not necessarily even a voter in the referendum) burning some of their own free balance while a referendum is near a borderline "Passing" state. Reducing the denominator can push a referendum from "not yet passing" into "Passing", trigger `ConfirmStarted`, and ultimately `Approved`/enacted with root-origin-scheduled dispatch — i.e. unauthorized approval of arbitrary runtime calls (including root-track proposals) that would not have passed on genuine voter turnout. This directly compromises the intended behavior of on-chain governance and can lead to origin escalation for whatever call the referendum enacts.

### Likelihood Explanation
Any accountholder can trigger a reduction in `TotalIssuance` through ordinary, unprivileged mechanisms (e.g. transferring balance below the existential deposit so dust is reaped/burned, or other public burn paths available in the runtime's `pallet_balances` configuration). No governance, admin, validator, or relayer role is required, and the referenda pallet's periodic `service_referendum`/`nudge_referendum` re-evaluation means the attacker doesn't need precise timing of a single finalize call — they just need the issuance reduction to land before the next scheduled alarm re-checks `is_passing`. This makes exploitation feasible for any borderline referendum, though the magnitude of achievable issuance reduction (and thus the size of the support-ratio shift) depends on how much of their own balance an attacker is willing/able to burn, which limits severity for very lopsided votes but is directly exploitable for close votes.

### Recommendation
Freeze the issuance/turnout denominator used for `support` at the point a referendum enters the Deciding phase (or use a moving snapshot updated only via privileged/consensus-safe means), rather than reading live `TotalIssuance` on every `service_referendum` evaluation. Alternatively, require that `Total::get()` be captured into `ReferendumStatus` at `begin_deciding` time and reused for the entire decision/confirmation lifecycle, so post-lock-in supply changes cannot retroactively alter whether a referendum is deemed "Passing."

### Proof of Concept
1. Submit/observe a referendum on a track approaching the end of its `decision_period`, where `tally.support(track)` is just below `track.min_support`'s required curve value (i.e., not yet "Passing").
2. As any account (does not need to have voted on this referendum), burn a portion of your own transferable/unlocked balance via a public path that reduces `pallet_balances::TotalIssuance` (e.g. dust removal on transfer below existential deposit).
3. Wait for the next scheduled `nudge_referendum` alarm (already scheduled per `ensure_alarm_at`/`decision_time`) to fire, which calls `service_referendum` → `is_passing` → `tally.support(id)` using the now-reduced `Total::get()`.
4. Because the denominator shrank while `self.support` (aye capital) stayed the same, `Perbill::from_rational(self.support, Total::get())` increases, causing `support_needed.passing(x, tally.support(id))` to evaluate true where it previously would not have, moving the referendum into `ConfirmStarted`/`Confirmed`/`Approved` without any additional genuine aye votes.

### Citations

**File:** substrate/frame/conviction-voting/src/types.rs (L68-70)
```rust
	fn support(&self, _: Class) -> Perbill {
		Perbill::from_rational(self.support, Total::get())
	}
```

**File:** substrate/frame/conviction-voting/src/tests.rs (L151-161)
```rust
impl Config for Test {
	type RuntimeEvent = RuntimeEvent;
	type Currency = pallet_balances::Pallet<Self>;
	type VoteLockingPeriod = ConstU64<3>;
	type MaxVotes = ConstU32<3>;
	type WeightInfo = ();
	type MaxTurnout = frame_support::traits::TotalIssuanceOf<Balances, Self::AccountId>;
	type Polls = TestPolls;
	type BlockNumberProvider = System;
	type VotingHooks = HooksHandler;
}
```

**File:** substrate/frame/referenda/src/lib.rs (L1184-1250)
```rust
				let is_passing = Self::is_passing(
					&status.tally,
					now.saturating_sub(deciding.since),
					track.decision_period,
					&track.min_support,
					&track.min_approval,
					status.track,
				);
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
						},
						Some(_) => ServiceBranch::ContinueConfirming,
						None => {
							// Start confirming
							dirty = true;
							deciding.confirming = Some(now.saturating_add(track.confirm_period));
							Self::deposit_event(Event::<T, I>::ConfirmStarted { index });
							ServiceBranch::BeginConfirming
						},
					}
				} else {
					if now >= deciding.since.saturating_add(track.decision_period) {
						// Failed!
						Self::ensure_no_alarm(&mut status);
						Self::note_one_fewer_deciding(status.track);
						Self::deposit_event(Event::<T, I>::Rejected { index, tally: status.tally });
						return (
							ReferendumInfo::Rejected(
								now,
								Some(status.submission_deposit),
								status.decision_deposit,
							),
							true,
							ServiceBranch::Rejected,
						);
					}
					if deciding.confirming.is_some() {
						// Stop confirming
						dirty = true;
						deciding.confirming = None;
						Self::deposit_event(Event::<T, I>::ConfirmAborted { index });
						ServiceBranch::EndConfirming
					} else {
						ServiceBranch::ContinueNotConfirming
					}
				};
				alarm = Self::decision_time(deciding, &status.tally, status.track, &track);
			},
```

**File:** substrate/frame/referenda/src/lib.rs (L1315-1329)
```rust
	/// Determine whether the given `tally` would result in a referendum passing at `elapsed` blocks
	/// into a total decision `period`, given the two curves for `support_needed` and
	/// `approval_needed`.
	fn is_passing(
		tally: &T::Tally,
		elapsed: BlockNumberFor<T, I>,
		period: BlockNumberFor<T, I>,
		support_needed: &Curve,
		approval_needed: &Curve,
		id: TrackIdOf<T, I>,
	) -> bool {
		let x = Perbill::from_rational(elapsed.min(period), period);
		support_needed.passing(x, tally.support(id)) &&
			approval_needed.passing(x, tally.approval(id))
	}
```
