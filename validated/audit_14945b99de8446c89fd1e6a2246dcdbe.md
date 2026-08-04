### Title
`min_support` denominator in `pallet-referenda` is recomputed live from `TotalIssuanceOf<Balances>`, letting normal token-burning activity (transfers, reaping, fees) shrink the support threshold mid-referendum - (File: `substrate/frame/conviction-voting/src/types.rs`, `substrate/frame/conviction-voting/src/lib.rs`, `substrate/bin/node/runtime/src/lib.rs`)

### Summary
The external report's core broken invariant is: *a quorum/threshold is calculated as a percentage of a total-voting-power value that can be shrunk during an active proposal's voting window, and the threshold check is re-evaluated live against that shrinking denominator instead of a value frozen at proposal creation.* The Substrate governance analog is `pallet-referenda`'s `min_support` curve, whose "support" ratio is `ayes_pre_conviction / Total::get()`, where `Total` is bound to `MaxTurnout`, which in the reference runtime is `TotalIssuanceOf<Balances, AccountId>` [1](#0-0) . This denominator is fetched fresh on every evaluation (`Tally::support`), not frozen when the referendum enters the deciding phase.

### Finding Description
`Tally::support()` computes the support fraction as `Perbill::from_rational(self.support, Total::get())`: [2](#0-1) 

`TallyOf<T, I>` binds `Total` directly to `T::MaxTurnout`: [3](#0-2) 

In the reference node runtime, `MaxTurnout` is set to `TotalIssuanceOf<Balances, AccountId>`, i.e. the live total issuance of the native currency: [1](#0-0) 

`pallet-referenda` calls `tally.support(id)` every time it evaluates whether a referendum is passing — during `is_passing`, `decision_time`, `begin_deciding`, and every `service_referendum` invocation that runs on scheduled alarms throughout the entire deciding/confirming window: [4](#0-3) [5](#0-4) 

Nothing freezes `Total::get()` (total issuance) at proposal submission or at the start of the deciding period. Any activity that legitimately reduces total issuance during the live window of an ongoing referendum — dust/account reaping under existential deposit rules, burn-on-transfer-fee configurations, `pallet-balances` slashing that burns rather than redistributes, or any other configured burn sink — immediately lowers the `min_support` threshold's denominator for *every currently-ongoing referendum on every track*, exactly mirroring the reported bug class ("dynamic quorum recalculated from a value attackers can deflate mid-vote, with no freeze at proposal creation").

This differs from the external report's exact mechanism (there, the denominator was "active locked voting power," reduced by *withdrawing* locked tokens after expiry) but the structural flaw is the same: a percentage-of-total-supply threshold with a **live, unfrozen denominator** checked repeatedly throughout the voting window instead of being fixed once. An unprivileged party does not need to touch conviction-voting locks at all — ordinary token movement that shrinks total issuance is sufficient, and existing guards (decision/confirm periods, `min_support` curves, deposit requirements) only bound *when* the check runs, not *what value* it is checked against.

### Impact Explanation
If the runtime's burn sinks are active during the life of a referendum (this is common: transaction fee burning, slashing burns, ED-reaping burns are all standard `pallet-balances`/`pallet-transaction-payment` configurations), a shrinking total-issuance denominator lets a minority of aye-votes cross the `min_support` curve threshold that would otherwise have required proportionally more supply-backed support. Because `pallet-referenda` proposals dispatch arbitrary calls (including root-track/whitelisted-caller tracks configured with `Root` origin), passing a referendum this way can lead to unauthorized privileged execution on a Substrate-based chain — matching the "runtime bug that compromises intended behavior" / "underpriced or manipulable public state feeding privileged dispatch" impact class.

### Likelihood Explanation
Total issuance decreasing during an active referendum's decision/confirm period is a background, always-on effect of chain operation (fees, ED reaping) rather than a rare or attacker-orchestrated event, so the denominator *will* drift over any multi-day decision period by design. However, turning this into a *decisive* exploit requires the drift to be large enough, and fast enough, relative to a track's `decision_period`/`confirm_period` to flip a marginal vote from failing to passing — total issuance on real chains is large and burns are comparatively small, so the magnitude of achievable manipulation is far smaller than the report's veToken analog (where withdrawal removed a large fraction of the denominator outright). This makes the issue theoretically present but of markedly lower and harder-to-quantify likelihood than the original report; I could not verify from the repository alone whether any deployed runtime's fee-burn/ED-reaping magnitude is large enough over a track's decision window to flip a real vote outcome — that would require economic modeling of a specific runtime's issuance and burn rate, which is outside what static code review can confirm.

### Recommendation
Snapshot the `MaxTurnout`/total-issuance value used for `Tally::support` at the point a referendum begins deciding (`begin_deciding`) and store it in `ReferendumStatus`, rather than calling `Total::get()` live on every `is_passing`/`decision_time` evaluation. Alternatively, bound how much the denominator is permitted to move within a single referendum's lifetime, or require `min_support` curves for privileged tracks (Root, WhitelistedCaller) to also depend on an absolute (non-relative) vote-count floor unaffected by issuance drift.

### Proof of Concept
A concrete PoC would need to be executed via a Devin session (out of scope for static/read-only analysis): construct a test runtime using `pallet-referenda` + `pallet-conviction-voting` with `MaxTurnout = TotalIssuanceOf<Balances>`, enable a burn sink (e.g. transaction-fee burn or ED reaping), submit a referendum on a low-`min_support` track, drive it into the deciding/confirming period with votes just below the current `min_support` threshold, then trigger issuance-reducing transactions (transfers that reap dust accounts, or fee-burning extrinsics) each block until `Tally::support()`'s recomputed denominator drops enough for `is_passing` to flip to `true` in `service_referendum`, and assert the referendum reaches `ReferendumInfo::Approved` despite the raw aye vote count never increasing. I was not able to run this test in the current read-only environment; the trace above is based on static code reading of `substrate/frame/referenda/src/lib.rs` and `substrate/frame/conviction-voting/src/{lib.rs,types.rs}` only.

### Citations

**File:** substrate/bin/node/runtime/src/lib.rs (L1080-1090)
```rust
impl pallet_conviction_voting::Config for Runtime {
	type WeightInfo = pallet_conviction_voting::weights::SubstrateWeight<Self>;
	type RuntimeEvent = RuntimeEvent;
	type Currency = Balances;
	type VoteLockingPeriod = VoteLockingPeriod;
	type MaxVotes = ConstU32<512>;
	type MaxTurnout = frame_support::traits::TotalIssuanceOf<Balances, Self::AccountId>;
	type Polls = Referenda;
	type BlockNumberProvider = System;
	type VotingHooks = ();
}
```

**File:** substrate/frame/conviction-voting/src/types.rs (L64-70)
```rust
	fn ayes(&self, _: Class) -> Votes {
		self.ayes
	}

	fn support(&self, _: Class) -> Perbill {
		Perbill::from_rational(self.support, Total::get())
	}
```

**File:** substrate/frame/conviction-voting/src/lib.rs (L85-85)
```rust
pub type TallyOf<T, I = ()> = Tally<BalanceOf<T, I>, <T as Config<I>>::MaxTurnout>;
```

**File:** substrate/frame/referenda/src/lib.rs (L1183-1250)
```rust
			Some(deciding) => {
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
