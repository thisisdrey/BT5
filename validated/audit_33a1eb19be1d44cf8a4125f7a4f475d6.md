## Analog Found: Unbounded Referenda Spam via Flat, Non-Scaling `SubmissionDeposit`

### Title
Referenda pallet permits unbounded low-cost proposal spam because `submit()` has no cap on concurrent "Preparing" referenda and a flat `SubmissionDeposit` that does not scale with proposal count - (File: `substrate/frame/referenda/src/lib.rs`)

### Summary
The external report's core broken invariant is: *a public, permissionless entrypoint that creates governance objects has no cost or rate mitigation proportional to the number of outstanding objects, letting one funded/malicious actor flood the system so cheaply that legitimate voters cannot review or contest them, with only one item needing to slip through to cause damage.* The local analog is `pallet_referenda::Pallet::submit` [1](#0-0) , which is reachable by any `SubmitOrigin`-approved signed account and only charges a flat, track-independent `SubmissionDeposit` [2](#0-1) , with **no limit at all on the number of referenda sitting in the "Preparing"/"Ongoing-not-yet-deciding" state** for a track.

### Finding Description
`submit()` takes a fixed deposit via `Self::take_deposit(who, T::SubmissionDeposit::get())` and unconditionally inserts a new `ReferendumInfo::Ongoing` entry into the unbounded `ReferendumInfoFor` `StorageMap` [3](#0-2) [4](#0-3) . The only capacity limits that exist in this pallet are on the **deciding** stage:
- `TrackQueue` is a `BoundedVec<_, T::MaxQueued>` [5](#0-4) 
- `DecidingCount` is checked against `track.max_deciding` in `ready_for_deciding` [6](#0-5) 

Neither of these is consulted at submission time. A referendum only enters `TrackQueue`/`DecidingCount` once a *Decision Deposit* has been placed and the *Preparation Period* has elapsed (`place_decision_deposit` / `service_referendum`) [7](#0-6) . Before that, a referendum simply sits as `Ongoing` with `decision_deposit: None`, alive until `UndecidingTimeout` expires [8](#0-7) .

In the Rococo runtime, `SubmitOrigin = frame_system::EnsureSigned<AccountId>` (any signed account, no membership gate) and `SubmissionDeposit = 1 * 3 * CENTS`, while `UndecidingTimeout` is 14 days [9](#0-8) . `MaxQueued` (100) only bounds the deciding queue, not total `Ongoing` count. This means:
- Cost per proposal is a flat, small deposit unrelated to how many referenda already exist (contrast this with `pallet-collective`'s `Consideration` mechanism, added specifically to fix this class of issue by scaling deposit cost with `active_proposals` count, see `do_propose_proposed` [10](#0-9)  and the corresponding PRDoc [11](#0-10) ). `pallet-referenda` never received an equivalent scaling mechanism for `SubmissionDeposit`.
- An attacker can call `submit` an unbounded number of times, creating an unbounded number of `Ongoing` referenda that never place a decision deposit, each persisting in unbounded on-chain storage for up to `UndecidingTimeout` blocks (and beyond that, until someone bothers to call `nudge_referendum`/`kill` to clear them out).

### Impact Explanation
This directly mirrors the report's "no mitigation for spam attacks" primitive: flooding the voting surface with cheap, low-quality proposals to (a) bloat chain state via unbounded `ReferendumInfoFor` insertions, (b) obscure a single malicious/urgent proposal among noise so voters cannot review all of them within the preparation window, and (c) impose scheduler/alarm overhead (`Self::set_alarm`) for every spammed referendum, degrading block production capacity for legitimate governance processing. This is a public underpriced-work vector against the runtime's governance/proof-of-legitimacy surface without requiring any privileged, malicious-validator, or malicious-relayer assumption — only a funded signed account.

### Likelihood Explanation
High, given `SubmitOrigin` is `EnsureSigned` in the observed runtime configuration (no council/membership gate) and `SubmissionDeposit` is a small flat constant with no dynamic scaling. Any account with modest funds can repeatedly call `submit` in a loop within a single block or across blocks, since nothing in `submit()` checks the caller's existing outstanding referenda count or a global/per-track cap on `Ongoing` (non-deciding) referenda.

### Recommendation
Introduce a `Consideration`-style dynamic deposit for `pallet_referenda::submit`, mirroring the fix already applied to `pallet-collective` (`pr_3151`): scale the submission cost with the number of currently `Ongoing` (non-decided) referenda per submitter and/or per track, or introduce an explicit cap on outstanding `Ongoing` referenda per track independent of the deciding-stage `MaxQueued`/`max_deciding` limits. Additionally, consider reaping/refunding `TimedOut` entries automatically rather than requiring a separate call, to bound storage growth.

### Proof of Concept
1. Fund an account `A` with enough balance to pay `SubmissionDeposit` (3 CENTS on Rococo) many thousands of times.
2. Loop calling `Referenda::submit(RuntimeOrigin::signed(A), Box::new(RawOrigin::Root.into()), <any bounded call>, DispatchTime::After(0))` — each call succeeds per the logic in `submit()` [1](#0-0) , since there is no check against total `Ongoing` count for the track.
3. Never call `place_decision_deposit` for any of them — they remain `Ongoing` with `decision_deposit: None`, consuming a `ReferendumInfoFor` storage slot each, for up to `UndecidingTimeout` (14 days) as seen in `service_referendum`'s `NoDeposit`/timeout branch [8](#0-7) .
4. Observe unbounded storage growth in `ReferendumInfoFor` and `ReferendumCount`, and that genuine voters/track-watchers cannot practically review/deposit against all spammed referenda before the preparation period elapses on any single malicious one, exactly matching the "only one vote needs to pass" abuse pattern from the external report.

### Citations

**File:** substrate/frame/referenda/src/lib.rs (L191-193)
```rust
		/// The minimum amount to be used as a deposit for a public referendum proposal.
		#[pallet::constant]
		type SubmissionDeposit: Get<BalanceOf<Self, I>>;
```

**File:** substrate/frame/referenda/src/lib.rs (L261-264)
```rust
	/// Information concerning any given referendum.
	#[pallet::storage]
	pub type ReferendumInfoFor<T: Config<I>, I: 'static = ()> =
		StorageMap<_, Blake2_128Concat, ReferendumIndex, ReferendumInfoOf<T, I>>;
```

**File:** substrate/frame/referenda/src/lib.rs (L266-277)
```rust
	/// The sorted list of referenda ready to be decided but not yet being decided, ordered by
	/// conviction-weighted approvals.
	///
	/// This should be empty if `DecidingCount` is less than `TrackInfo::max_deciding`.
	#[pallet::storage]
	pub type TrackQueue<T: Config<I>, I: 'static = ()> = StorageMap<
		_,
		Twox64Concat,
		TrackIdOf<T, I>,
		BoundedVec<(ReferendumIndex, T::Votes), T::MaxQueued>,
		ValueQuery,
	>;
```

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

**File:** substrate/frame/referenda/src/lib.rs (L1146-1181)
```rust
				} else {
					// Are we ready for deciding?
					branch = if status.decision_deposit.is_some() {
						let prepare_end = status.submitted.saturating_add(track.prepare_period);
						if now >= prepare_end {
							let (maybe_alarm, branch) =
								Self::ready_for_deciding(now, &track, index, &mut status);
							if let Some(set_alarm) = maybe_alarm {
								alarm = alarm.min(set_alarm);
							}
							dirty = true;
							branch
						} else {
							alarm = alarm.min(prepare_end);
							ServiceBranch::Preparing
						}
					} else {
						alarm = timeout;
						ServiceBranch::NoDeposit
					}
				}
				// If we didn't move into being decided, then check the timeout.
				if status.deciding.is_none() && now >= timeout && !status.in_queue {
					// Too long without being decided - end it.
					Self::ensure_no_alarm(&mut status);
					Self::deposit_event(Event::<T, I>::TimedOut { index, tally: status.tally });
					return (
						ReferendumInfo::TimedOut(
							now,
							Some(status.submission_deposit),
							status.decision_deposit,
						),
						true,
						ServiceBranch::TimedOut,
					);
				}
```

**File:** polkadot/runtime/rococo/src/governance/mod.rs (L54-97)
```rust
parameter_types! {
	pub const AlarmInterval: BlockNumber = 1;
	pub const SubmissionDeposit: Balance = 1 * 3 * CENTS;
	pub const UndecidingTimeout: BlockNumber = 14 * DAYS;
}

parameter_types! {
	pub const MaxBalance: Balance = Balance::max_value();
}
pub type TreasurySpender = EitherOf<EnsureRootWithSuccess<AccountId, MaxBalance>, Spender>;

impl origins::pallet_custom_origins::Config for Runtime {}

impl pallet_whitelist::Config for Runtime {
	type WeightInfo = weights::pallet_whitelist::WeightInfo<Self>;
	type RuntimeCall = RuntimeCall;
	type RuntimeEvent = RuntimeEvent;
	type WhitelistOrigin =
		EitherOf<EnsureRootWithSuccess<Self::AccountId, ConstU16<65535>>, Fellows>;
	type DispatchWhitelistedOrigin = EitherOf<EnsureRoot<Self::AccountId>, WhitelistedCaller>;
	type DeferredDispatchExpiration = ConstU32<{ 28 * DAYS }>;
	type BlockNumberProvider = System;
	type Preimages = Preimage;
}

impl pallet_referenda::Config for Runtime {
	type WeightInfo = weights::pallet_referenda_referenda::WeightInfo<Self>;
	type RuntimeCall = RuntimeCall;
	type RuntimeEvent = RuntimeEvent;
	type Scheduler = Scheduler;
	type Currency = Balances;
	type SubmitOrigin = frame_system::EnsureSigned<AccountId>;
	type CancelOrigin = EitherOf<EnsureRoot<AccountId>, ReferendumCanceller>;
	type KillOrigin = EitherOf<EnsureRoot<AccountId>, ReferendumKiller>;
	type Slash = Treasury;
	type Votes = pallet_conviction_voting::VotesOf<Runtime>;
	type Tally = pallet_conviction_voting::TallyOf<Runtime>;
	type SubmissionDeposit = SubmissionDeposit;
	type MaxQueued = ConstU32<100>;
	type UndecidingTimeout = UndecidingTimeout;
	type AlarmInterval = AlarmInterval;
	type Tracks = TracksInfo;
	type Preimages = Preimage;
	type BlockNumberProvider = System;
```

**File:** substrate/frame/collective/src/lib.rs (L955-964)
```rust
		let active_proposals =
			<Proposals<T, I>>::try_mutate(|proposals| -> Result<usize, DispatchError> {
				proposals.try_push(proposal_hash).map_err(|_| Error::<T, I>::TooManyProposals)?;
				Ok(proposals.len())
			})?;

		let cost = T::Consideration::new(&who, active_proposals as u32 - 1)?;
		if !cost.is_none() {
			<CostOf<T, I>>::insert(proposal_hash, (who.clone(), cost));
		}
```

**File:** prdoc/stable2412/pr_3151.prdoc (L4-18)
```text
title: Dynamic deposit based on number of proposals 

doc:
  - audience: 
      - Runtime User
      - Runtime Dev
    description: |
      Introduce a dynamic proposal deposit mechanism influenced by the total number of active 
      proposals, with the option to set the deposit to none.

      The potential cost (e.g., balance hold) for proposal submission and storage is determined 
      by the implementation of the `Consideration` trait. The footprint is defined as `proposal_count`, 
      representing the total number of active proposals in the system, excluding the one currently 
      being proposed. This cost may vary based on the proposal count. The pallet also offers various 
      types to define a cost strategy based on the number of proposals.
```
