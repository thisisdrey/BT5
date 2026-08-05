Based on my research, I found a structural analog to the Pyth "max voter weight manipulation" bug in `pallet-conviction-voting` / `pallet-referenda`.

### Title
Support-threshold manipulation via dynamic total-issuance read allows unauthorized referendum passage - (File: `substrate/frame/conviction-voting/src/types.rs`)

### Summary
The Pyth bug lets an attacker shrink the denominator (mint supply) used to compute voter weight *after* votes are cast but *before* finalization, so a minority of locked tokens can retroactively satisfy a majority threshold. The polkadot-sdk equivalent lives in `pallet-conviction-voting`'s `Tally::support`, which divides the fixed, already-cast `support` capital by a **live** read of total issuance rather than a value snapshotted at vote time.

### Finding Description
`Tally::support` is defined as: [1](#0-0) 

`Total::get()` is bound in the node runtime to `frame_support::traits::TotalIssuanceOf<Balances, Self::AccountId>` via the `MaxTurnout` associated type: [2](#0-1) 

This means every time `support()` is evaluated — during `pallet-referenda`'s `nudge_referendum`/`service_referendum` machinery that decides whether a track's `min_support` curve is satisfied (`is_passing`, `decision_time`) — it re-reads the *current* chain-wide total issuance, not a value fixed when the tally's votes were cast: [3](#0-2) [4](#0-3) 

`self.support` (the numerator, the raw pre-conviction capital locked by "aye" voters) is fixed once votes are cast and locked: [5](#0-4) 

Because the denominator (`Total::get()`) is live rather than snapshotted, any permissionless action that reduces total issuance between vote-casting and the referendum's confirm/decision check inflates the computed support ratio without adding any real votes — exactly the "shrink the denominator after votes are locked in" primitive from the Pyth report, where burning tokens after voting increased the effective vote weight ratio.

### Impact Explanation
If total issuance can be reduced (e.g., via a public/permissionless burn path such as `pallet_balances`'s self-service burn extrinsic, transaction-fee burning, or other burn-capable pallets configured in a runtime) while a referendum is in its `Deciding`/`Confirming` window, an attacker holding a minority of `support` capital can artificially cross a track's `min_support` curve requirement that they otherwise could not meet with genuine turnout. This can push a referendum that should be `Rejected` into `Approved` state, which — combined with root/whitelisted-caller tracks — grants unauthorized execution of privileged calls. This matches the "unauthorized origin escalation via forged threshold acceptance" impact class.

### Likelihood Explanation
Exploitability is runtime-configuration dependent: it requires (a) a track whose `min_support` curve is near the current support ratio for a given proposal, and (b) some caller-controlled mechanism in the concrete runtime that reduces `TotalIssuanceOf` (self-burn, or another permissionless burn-triggering flow) during the decision window. Because `support()` is re-evaluated on every scheduler-driven `nudge_referendum` tick rather than cached, no elevated privilege is needed to trigger the recompute — only the ability to lower total issuance. This is analogous to, but weaker in blast radius than, the Pyth case, since Substrate's curves generally require a much larger swing than the Pyth minimum-threshold example; still, the underlying denominator-timing flaw is structurally identical.

### Recommendation
Snapshot total issuance (or an equivalent "electorate" baseline) at the point the referendum enters its `Deciding` state (or track submission), and use that frozen value for all subsequent `support()` evaluations of that referendum, rather than re-reading `TotalIssuanceOf` live on every tick. This mirrors the Pyth remediation of deriving voter weight from a fixed constant rather than a live, manipulable supply value.

### Proof of Concept
1. Configure/observe a runtime where `pallet_balances` or another pallet exposes a public self-service burn (reducing `TotalIssuanceOf`).
2. Submit a referendum on a track whose `min_support` curve is close to, but above, the current `support / total_issuance` ratio.
3. Cast votes to lock in a `support` capital value just short of the curve requirement at the current total issuance.
4. Before the `Deciding`/`Confirming` window closes, trigger burns that reduce total issuance sufficiently that `Perbill::from_rational(self.support, Total::get())` now exceeds the curve's required value at the current elapsed-time point.
5. On the next scheduler tick, `is_passing` in `substrate/frame/referenda/src/lib.rs` recomputes `support` live and the referendum transitions into `Confirming`/`Approved`, despite no additional genuine votes being cast — reproducing the Pyth-style "shrink the denominator after voting" bypass.

Note: full confirmation that a *permissionless* burn path exists and is wired into a concrete production runtime (vs. only privileged/root-gated burn calls) requires reading `substrate/frame/balances/src/lib.rs`'s dispatchable definitions in full, which I was not able to completely verify within the available tool calls — this should be checked before treating the PoC as fully weaponized in any specific runtime.

### Citations

**File:** substrate/frame/conviction-voting/src/types.rs (L68-70)
```rust
	fn support(&self, _: Class) -> Perbill {
		Perbill::from_rational(self.support, Total::get())
	}
```

**File:** substrate/frame/conviction-voting/src/types.rs (L126-157)
```rust
	/// Add an account's vote into the tally.
	pub fn add(&mut self, vote: AccountVote<Votes>) -> Option<()> {
		match vote {
			AccountVote::Standard { vote, balance } => {
				let Delegations { votes, capital } = vote.conviction.votes(balance);
				match vote.aye {
					true => {
						self.support = self.support.checked_add(&capital)?;
						self.ayes = self.ayes.checked_add(&votes)?
					},
					false => self.nays = self.nays.checked_add(&votes)?,
				}
			},
			AccountVote::Split { aye, nay } => {
				let aye = Conviction::None.votes(aye);
				let nay = Conviction::None.votes(nay);
				self.support = self.support.checked_add(&aye.capital)?;
				self.ayes = self.ayes.checked_add(&aye.votes)?;
				self.nays = self.nays.checked_add(&nay.votes)?;
			},
			AccountVote::SplitAbstain { aye, nay, abstain } => {
				let aye = Conviction::None.votes(aye);
				let nay = Conviction::None.votes(nay);
				let abstain = Conviction::None.votes(abstain);
				self.support =
					self.support.checked_add(&aye.capital)?.checked_add(&abstain.capital)?;
				self.ayes = self.ayes.checked_add(&aye.votes)?;
				self.nays = self.nays.checked_add(&nay.votes)?;
			},
		}
		Some(())
	}
```

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

**File:** substrate/frame/referenda/src/lib.rs (L1263-1278)
```rust
	fn decision_time(
		deciding: &DecidingStatusOf<T, I>,
		tally: &T::Tally,
		track_id: TrackIdOf<T, I>,
		track: &TrackInfoOf<T, I>,
	) -> BlockNumberFor<T, I> {
		deciding.confirming.unwrap_or_else(|| {
			// Set alarm to the point where the current voting would make it pass.
			let approval = tally.approval(track_id);
			let support = tally.support(track_id);
			let until_approval = track.min_approval.delay(approval);
			let until_support = track.min_support.delay(support);
			let offset = until_support.max(until_approval);
			deciding.since.saturating_add(offset.mul_ceil(track.decision_period))
		})
	}
```

**File:** substrate/frame/referenda/src/lib.rs (L1318-1329)
```rust
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
