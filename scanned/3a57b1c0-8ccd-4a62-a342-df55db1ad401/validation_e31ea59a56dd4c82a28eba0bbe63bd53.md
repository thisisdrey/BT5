## Analysis

The Code4rena/Spearbit report's core broken invariant is: **a threshold-defining value read live from mutable global state (`totalSupply`) can be moved atomically and permissionlessly by the same actor who benefits from the threshold check, inside the exact window where the check fires.**

The direct analog in `polkadot-sdk` is in **OpenGov's conviction-voting/referenda pipeline**, where the "support" ratio used to decide whether a referendum passes is computed by dividing by a **live** `TotalIssuance` read, and `pallet-balances` exposes a **permissionless `burn` extrinsic** that any signed account can call to shrink that exact denominator in the same block the decision is evaluated.

### Title
Referendum support/approval thresholds can be flipped by permissionlessly burning tokens via `pallet_balances::burn` right before the decision check - (File: `substrate/frame/conviction-voting/src/types.rs`, `substrate/frame/referenda/src/lib.rs`, `substrate/frame/balances/src/lib.rs`)

### Summary
`pallet_conviction_voting::Tally::support()` computes the referendum's support ratio as `self.support / Total::get()`, where `Total::get()` (`MaxTurnout`) is bound to `TotalIssuanceOf<Balances, ...>` — a **live** read of `pallet_balances::TotalIssuance` at the moment the check executes, not a value snapshotted when the referendum began. `pallet-referenda`'s `is_passing`/`decision_time` logic (invoked every block via the scheduler alarm in `nudge_referendum`) re-evaluates this ratio on the fly. Since `pallet-balances` added a permissionless `burn` extrinsic that lets any signed account reduce `TotalIssuance` directly by burning their own liquid balance, an attacker can shrink the denominator of the support calculation in the very block the alarm fires, flipping a borderline referendum from "not passing" to "passing" (or vice versa for an opponent), with no external flash loan and no privileged role required — exactly the risk class described in the report (a live, cheaply-movable "total supply" figure being used as a threshold denominator).

### Finding Description
- `Tally::support` divides by `Total::get()`: [1](#0-0) 
- `Total` is configured as the live total issuance of the native currency: [2](#0-1) 
- The decision logic re-derives `support`/`approval` from the tally against the curve every time the referendum is serviced (each alarm/block), with no stored snapshot of issuance taken at referendum start: [3](#0-2)  and the pass/fail branch acting on it inside `nudge_referendum`: [4](#0-3) 
- `pallet-balances` exposes a fully permissionless `burn` extrinsic (`call_index(10)`) that any signed account can use to reduce `TotalIssuance` by burning their own free balance: [5](#0-4) 
- The underlying mutation directly decrements the global `TotalIssuance` storage item used by the `Total::get()` read above: [6](#0-5) 
- The legacy `pallet-democracy` has the same pattern: it reads `T::Currency::total_issuance()` live at `bake_referendum` time and feeds it straight into `VoteThreshold::approved`, which also divides by the (square-rooted) electorate: [7](#0-6)  and [8](#0-7) 

No component snapshots issuance at referendum submission/decision-start; the threshold denominator is whatever `TotalIssuance` happens to be in the exact block the pass/fail branch executes. This is structurally identical to the report's `currentTokenSupply()` gaming: a threshold ratio is computed against a live, cheaply-movable supply figure, and the actor who benefits from the outcome controls the timing and magnitude of the supply change via a public, unprivileged call.

### Impact Explanation
This directly touches "runtime bugs that compromise intended behavior" and governance-critical accounting: a well-funded but otherwise unprivileged actor can manipulate whether a chain-changing referendum (including Root-track proposals) passes or fails, without needing a malicious validator, collator, or admin — only ownership of enough liquid native tokens to burn (which are destroyed, not "at risk" beyond the burn itself) and correct timing of a single public extrinsic in the block where the decision alarm executes. Because `min_support`/`min_approval` curves are percentage-based, shrinking `TotalIssuance` disproportionately benefits low-turnout "aye" tallies late in the decision period, letting an attacker push through (or block) proposals that would not have organically met support.

### Likelihood Explanation
Requires only a signed extrinsic (`Balances::burn`) callable by any account, combined with knowledge of when the referendum's decision alarm block will execute — publicly visible on-chain via `Referenda::ReferendumInfoOf`/scheduler state. No relayer, validator, prover, or governance privilege is needed. The main constraint is having enough liquid balance to burn to move the ratio meaningfully, which scales with how close the referendum already is to the threshold curve (most impactful in close votes, which are also the most contentious/valuable to manipulate).

### Recommendation
- Snapshot `TotalIssuance` (or `active_issuance`) once when a referendum enters the deciding phase (or use an average/checkpointed figure) instead of re-reading it live on every `nudge_referendum`/`bake_referendum` call.
- Alternatively, bound how much `TotalIssuance` may move within a single decision-period window relative to conviction-voting/democracy threshold computations, or require the "worst-case" (min over the confirm/decision window) issuance figure, mirroring the report's "compute using both min and max supply and take the conservative figure" recommendation.
- Consider rate-limiting or disabling the permissionless `burn` extrinsic's ability to alter the exact denominator used mid-decision, or explicitly document that vote thresholds are subject to real-time issuance if the risk is deemed acceptable.

### Proof of Concept
1. An OpenGov referendum on a contested track is close to the `min_support` curve threshold but not yet passing, with the decision alarm scheduled to fire in block `N` (visible via `ReferendumInfoOf`/`DecidingStatus.confirming`/`decision_time`).
2. In block `N` (or a preceding block within the same evaluation window), an attacker with a large liquid balance calls `Balances::burn(origin, value, keep_alive)` — a standard signed extrinsic, callable by anyone — burning enough tokens to meaningfully shrink `pallet_balances::TotalIssuance`.
3. When `Referenda::nudge_referendum` (or the scheduler alarm) executes `is_passing`, `Tally::support()` computes `Perbill::from_rational(self.support, Total::get())` against the now-reduced `TotalIssuance`, pushing the ratio above `track.min_support`'s curve value at the current elapsed time.
4. The referendum transitions into `ConfirmStarted`/`Approved` (or a previously-passing one is flipped to `Rejected` by an opposing burn), altering the outcome purely via a public, single-block, unprivileged burn call — with no external capital risk beyond the burned amount itself.

### Citations

**File:** substrate/frame/conviction-voting/src/types.rs (L68-70)
```rust
	fn support(&self, _: Class) -> Perbill {
		Perbill::from_rational(self.support, Total::get())
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

**File:** substrate/frame/referenda/src/lib.rs (L1183-1213)
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

**File:** substrate/frame/balances/src/lib.rs (L850-874)
```rust
		/// Burn the specified liquid free balance from the origin account.
		///
		/// If the origin's account ends up below the existential deposit as a result
		/// of the burn and `keep_alive` is false, the account will be reaped.
		///
		/// Unlike sending funds to a _burn_ address, which merely makes the funds inaccessible,
		/// this `burn` operation will reduce total issuance by the amount _burned_.
		#[pallet::call_index(10)]
		#[pallet::weight(if *keep_alive {T::WeightInfo::burn_keep_alive()} else {T::WeightInfo::burn_allow_death()})]
		pub fn burn(
			origin: OriginFor<T>,
			#[pallet::compact] value: T::Balance,
			keep_alive: bool,
		) -> DispatchResult {
			let source = ensure_signed(origin)?;
			let preservation = if keep_alive { Preserve } else { Expendable };
			<Self as fungible::Mutate<_>>::burn_from(
				&source,
				value,
				preservation,
				Precision::Exact,
				Polite,
			)?;
			Ok(())
		}
```

**File:** substrate/frame/balances/src/impl_currency.rs (L336-351)
```rust
	// Burn funds from the total issuance, returning a positive imbalance for the amount burned.
	// Is a no-op if amount to be burned is zero.
	fn burn(mut amount: Self::Balance) -> Self::PositiveImbalance {
		if amount.is_zero() {
			return PositiveImbalance::zero();
		}
		<TotalIssuance<T, I>>::mutate(|issued| {
			*issued = issued.checked_sub(&amount).unwrap_or_else(|| {
				amount = *issued;
				Zero::zero()
			});
		});

		Pallet::<T, I>::deposit_event(Event::<T, I>::Rescinded { amount });
		PositiveImbalance::new(amount)
	}
```

**File:** substrate/frame/democracy/src/lib.rs (L1597-1604)
```rust
	fn bake_referendum(
		now: BlockNumberFor<T>,
		index: ReferendumIndex,
		status: ReferendumStatus<BlockNumberFor<T>, BoundedCallOf<T>, BalanceOf<T>>,
	) -> bool {
		let total_issuance = T::Currency::total_issuance();
		let approved = status.threshold.approved(status.tally, total_issuance);

```

**File:** substrate/frame/democracy/src/vote_threshold.rs (L103-118)
```rust
	fn approved(&self, tally: Tally<Balance>, electorate: Balance) -> bool {
		let sqrt_voters = tally.turnout.integer_sqrt();
		let sqrt_electorate = electorate.integer_sqrt();
		if sqrt_voters.is_zero() {
			return false;
		}
		match *self {
			VoteThreshold::SuperMajorityApprove => {
				compare_rationals(tally.nays, sqrt_voters, tally.ayes, sqrt_electorate)
			},
			VoteThreshold::SuperMajorityAgainst => {
				compare_rationals(tally.nays, sqrt_electorate, tally.ayes, sqrt_voters)
			},
			VoteThreshold::SimpleMajority => tally.ayes > tally.nays,
		}
	}
```
