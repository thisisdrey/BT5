### Title
`pallet-staking-async::chill_inactive` can permissionlessly de-register an actively-producing validator using not-yet-synced era reward points — ([File: substrate/frame/staking-async/src/pallet/mod.rs])

### Summary
`chill_inactive` is a permissionless, fee-free extrinsic that removes a validator from the active set if its supplied inactivity proof shows zero reward points for `ChillInactiveThreshold` eras. The reward points it reads (`ErasRewardPoints`) are populated **only** when the asynchronous `SessionReport` message from the Relay Chain (RC) has been received and processed by Asset Hub (AH) via `Eras::reward_active_era`. Because the RC→AH session-report channel is itself asynchronous, retried, and can be delayed across several sessions/eras, `chill_inactive` can read a *stale, not-yet-synced* reward-points snapshot and conclude a validator was "inactive" for an era in which it was in fact producing blocks/points — mirroring the external report's pattern of making an irreversible protocol decision (`triggerWindDown`) from data that had not been synced (`syncStablecoinRevenue`) first.

### Finding Description
`chill_inactive` (call_index 35) is defined in `substrate/frame/staking-async/src/pallet/mod.rs` (~lines 3216-3271): [1](#0-0) 

It is callable by `ensure_signed(origin)` — any unprivileged account — and on success charges `Pays::No`, i.e. it is completely free to attempt repeatedly.

The decision hinges on:
1. `active_era = Rotator::<T>::active_era()` — the AH-local notion of the currently active era.
2. `Eras::<T>::was_validator_exposed(era, &stash)` — whether the validator was elected/exposed for that era.
3. `Eras::<T>::get_reward_points_for_validator(era, &stash)` fed into `T::IsValidatorInactive::is_inactive`, whose default implementation is simply `era_points == 0`: [2](#0-1) 

Critically, `ErasRewardPoints` for the active era is **not** populated synchronously with block production. It is only updated when AH processes an RC `SessionReport` via `reward_active_era`: [3](#0-2) 

which itself is invoked from `on_relay_session_report`, triggered only when the cross-chain `SessionReport` message actually arrives at AH: [4](#0-3) 

The RC→AH channel that carries these validator points is asynchronous and can be delayed for multiple sessions. The pallet's own tests demonstrate that session reports (and the reward points they carry) can fail delivery, be retried, and only "restored and merged" into the buffer after several failed attempts — i.e., genuinely lagging behind the validator's actual on-chain activity for a non-trivial number of sessions: [5](#0-4) 

The doc comments for `SessionReport` further confirm this is a normal, expected part of the design (not an attacker-controlled fault): [6](#0-5) 

Nothing in `chill_inactive` requires or waits for confirmation that all relevant `SessionReport`s for the proof-window eras have actually landed on AH before evaluating the "inactivity" proof — exactly the missing "sync-before-decision" step described in the external report (`syncStablecoinRevenue` before `recognizedRevenueUsd`). The existing guards (`InvalidLen`, `NotSorted`, `ValidatorNotExposed`, `InvalidEra`) only validate the *shape* of the proof and that the validator was *exposed*; they do not validate that reward-point accounting for those eras is final/synced.

### Impact Explanation
An actively-producing, honest validator can be permissionlessly and irrevocably removed (`do_remove_validator`) from the validator set — with the caller paying no fee — purely because the RC→AH session-report pipeline had not yet delivered/processed the points for the most recent eras in the retained window at the moment the proof was submitted. This:
- Disrupts NPoS validator-set integrity and can degrade block/finality production if repeated against multiple validators near era boundaries or during periods of session-report delivery delay/retry (a normal operational condition, not requiring a malicious relayer, validator, or governance actor).
- Causes the wrongly-chilled validator to lose out on rewards/participation for an era in which it was actually active, i.e., an unjust "settlement" of validator status based on unsynced state — matching the "runtime bugs that compromise intended behavior" and "degrade block production" impact categories in scope.

### Likelihood Explanation
Because delivery lag/retries of `SessionReport` are an intrinsic, non-malicious part of the design (demonstrated by the pallet's own `session_report_send_fails_after_retries` test), the window in which `ErasRewardPoints` for the oldest eras in the fixed `ChillInactiveThreshold`-length proof is stale is not a rare edge case — it recurs any time XCM/queue congestion or retries push a session report's arrival later than usual. Any observer can monitor on-chain `ErasRewardPoints`/`Eras::was_validator_exposed` state and submit `chill_inactive` the moment a target validator's most recent era shows zero points due to reporting lag, at zero cost (`Pays::No`).

### Recommendation
Before honoring an inactivity proof in `chill_inactive`, ensure the reward-point state for every era in the proof window is final/synced — e.g., only accept proofs for eras strictly older than `active_era.saturating_sub(1)` (never the era whose session reports may still be in flight), or track and require a "reports fully received up to era X" watermark (analogous to calling `syncStablecoinRevenue` first) that `chill_inactive` must consult before trusting `ErasRewardPoints` as authoritative for the most recent eras in the window.

### Proof of Concept
1. Validator `V` is elected and actively produces blocks/parachain-consensus points during era `E` on the Relay Chain.
2. Due to normal message-queue congestion (as modeled by `NextAhDeliveryFails` in the pallet's own tests), the `SessionReport` carrying `V`'s points for era `E` is delayed/retried and has not yet been processed by `on_relay_session_report` on AH.
3. AH's `ErasRewardPoints::<T>::get(E)` therefore still shows `0` points for `V`, even though `Eras::was_validator_exposed(E, &V)` is `true` (V was elected/exposed for that era).
4. Any unprivileged account submits `chill_inactive(origin, V, proof)` where `proof` includes era `E` (and other older eras where `V` also happens to show 0 recorded points because of the same lag), satisfying all of `InvalidLen`, `NotSorted`, `ValidatorNotExposed`, `InvalidEra`, and `ValidatorActive` checks.
5. `do_remove_validator(&V)` succeeds, chilling an actually-active validator, for free (`Pays::No`), purely because the reward-point sync from RC had not yet caught up.

**Uncertainty note:** I was not able to fully trace the exact maximum possible delay window for `SessionReport` delivery (i.e., whether it can realistically span the full `ChillInactiveThreshold` number of eras under adversarial-free conditions, or only a session or two) within the available index; this bounds how easily exploitable the timing window is in practice, and a full investigation of the message-queue retry/backoff logic in `pallet-staking-async-rc-client` would be needed to confirm real-world exploitability precisely.

### Citations

**File:** substrate/frame/staking-async/src/pallet/mod.rs (L3218-3262)
```rust
		pub fn chill_inactive(
			origin: OriginFor<T>,
			stash: T::AccountId,
			proof: BoundedVec<EraIndex, T::HistoryDepth>,
		) -> DispatchResultWithPostInfo {
			ensure_signed(origin)?;

			let threshold = ChillInactiveThreshold::<T>::get();
			ensure!(
				proof.len() as EraIndex == threshold,
				Error::<T>::InvalidInactivityProof(InvalidInactivityProofError::InvalidLen)
			);
			ensure!(
				proof.is_sorted_by(|a, b| a < b),
				Error::<T>::InvalidInactivityProof(InvalidInactivityProofError::NotSorted)
			);

			// All proof eras must fall in the retained window `[active_era - HistoryDepth,
			// active_era)`.
			let active_era = Rotator::<T>::active_era();
			let oldest_allowed_era = active_era.saturating_sub(T::HistoryDepth::get());
			let oldest_proof_era = proof.first().copied().unwrap_or(EraIndex::MAX);
			let most_recent_proof_era = proof.last().copied().unwrap_or(EraIndex::MAX);
			ensure!(
				oldest_proof_era >= oldest_allowed_era && most_recent_proof_era < active_era,
				Error::<T>::InvalidInactivityProof(InvalidInactivityProofError::InvalidEra)
			);

			for era in proof {
				ensure!(
					Eras::<T>::was_validator_exposed(era, &stash),
					Error::<T>::InvalidInactivityProof(
						InvalidInactivityProofError::ValidatorNotExposed
					)
				);

				let points = Eras::<T>::get_reward_points_for_validator(era, &stash);

				ensure!(
					T::IsValidatorInactive::is_inactive(era, &stash, points),
					Error::<T>::InvalidInactivityProof(
						InvalidInactivityProofError::ValidatorActive
					)
				);
			}
```

**File:** substrate/frame/staking-async/src/lib.rs (L505-517)
```rust
/// Check if validator was inactive at some era.
///
/// The check is based on the [`RewardPoint`] amount received during the era by the given validator.
pub trait IsValidatorInactive<AccountId> {
	/// Tell if the validator considered inactive.
	fn is_inactive(era: EraIndex, stash: &AccountId, era_points: RewardPoint) -> bool;
}

impl<AccountId> IsValidatorInactive<AccountId> for () {
	fn is_inactive(_era: EraIndex, _stash: &AccountId, era_points: RewardPoint) -> bool {
		era_points == 0
	}
}
```

**File:** substrate/frame/staking-async/src/session_rotation.rs (L415-462)
```rust
	/// Add reward points to validators using their stash account ID.
	///
	/// As a side effect, accumulates `weight × points` into [`ErasSumWeightedPoints`] for the
	/// active era, where `weight` is the validator's [`ErasValidatorIncentiveWeight`]. This
	/// keeps the denominator of the weighted-points share up to date without iterating every
	/// validator at payout time.
	pub(crate) fn reward_active_era(
		validators_points: impl IntoIterator<Item = (T::AccountId, u32)>,
	) {
		if let Some(active_era) = ActiveEra::<T>::get() {
			let mut sum_weighted_points_delta: BalanceOf<T> = Zero::zero();
			<ErasRewardPoints<T>>::mutate(active_era.index, |era_rewards| {
				for (validator, points) in validators_points.into_iter() {
					let weight =
						ErasValidatorIncentiveWeight::<T>::get(active_era.index, &validator)
							.unwrap_or_else(Zero::zero);

					let recorded = match era_rewards.individual.get_mut(&validator) {
						Some(individual) => {
							individual.saturating_accrue(points);
							true
						},
						None => {
							// not much we can do -- validators should always be less than
							// `MaxValidatorSet`.
							era_rewards.individual.try_insert(validator, points).defensive().is_ok()
						},
					};

					// Keep the denominator aligned with `individual`, which is the source used
					// by payouts and try-state recomputation. A defensive overflow may leave
					// points unrecorded; those points must not be counted in
					// `ErasSumWeightedPoints`.
					if recorded && !weight.is_zero() {
						sum_weighted_points_delta = sum_weighted_points_delta.saturating_add(
							weight.saturating_mul(IncentiveWeight::<T>::from(points)),
						);
					}

					era_rewards.total.saturating_accrue(points);
				}
			});
			if !sum_weighted_points_delta.is_zero() {
				ErasSumWeightedPoints::<T>::mutate(active_era.index, |sum| {
					*sum = sum.saturating_add(sum_weighted_points_delta);
				});
			}
		}
```

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L1413-1432)
```rust
	fn on_relay_session_report(report: rc_client::SessionReport<Self::AccountId>) -> Weight {
		log!(debug, "Received session report: {}", report,);

		let rc_client::SessionReport {
			end_index,
			activation_timestamp,
			validator_points,
			leftover,
		} = report;
		debug_assert!(!leftover);

		let validator_count = validator_points.len() as u32;
		// note: weight for `reward_active_era` is taken care of inside `end_session`
		Eras::<T>::reward_active_era(validator_points.into_iter());
		session_rotation::Rotator::<T>::end_session(
			end_index,
			activation_timestamp,
			validator_count,
		)
	}
```

**File:** substrate/frame/staking-async/integration-tests/src/rc/test.rs (L380-450)
```rust
#[test]
fn session_report_send_fails_after_retries() {
	// if a session report cannot be sent, first we retry. If we still fail and retries are out, we
	// restore the points.
	ExtBuilder::default().local_queue().build().execute_with(|| {
		// insert a custom validator point for easier tracking
		ah_client::ValidatorPoints::<Runtime>::insert(1, 100);

		assert_eq!(pallet_session::CurrentIndex::<Runtime>::get(), 0);
		assert!(ah_client::OutgoingSessionReport::<Runtime>::get().is_none());

		// when roll forward, but next message will fail to be sent
		NextAhDeliveryFails::set(true);
		roll_until_matches(|| pallet_session::CurrentIndex::<Runtime>::get() == 1, false);

		// these are the points that are saved in the outgoing report
		assert_eq!(
			OutgoingSessionReport::<Runtime>::get().unwrap().0.validator_points,
			vec![(1, 100), (11, 580)]
		);

		// now we have 2 retries left
		assert!(matches!(ah_client::OutgoingSessionReport::<Runtime>::get(), Some((_, 2))));
		// validator points are drained, since we have the session report.
		assert_eq!(validator_points(), vec![]);
		// event emitted
		assert_eq!(
			ah_client_events_since_last_call(),
			vec![ah_client::Event::Unexpected(UnexpectedKind::SessionReportSendFailed)]
		);

		// again
		NextAhDeliveryFails::set(true);
		roll_next();
		assert!(matches!(ah_client::OutgoingSessionReport::<Runtime>::get(), Some((_, 1))));
		// this is registered by our mock setup
		assert_eq!(validator_points(), vec![(11, 20)]);
		assert_eq!(
			ah_client_events_since_last_call(),
			vec![ah_client::Event::Unexpected(UnexpectedKind::SessionReportSendFailed)]
		);

		// in the meantime, we receive some new validator points.
		ah_client::ValidatorPoints::<Runtime>::insert(1, 50);

		// again
		NextAhDeliveryFails::set(true);
		roll_next();
		assert!(matches!(ah_client::OutgoingSessionReport::<Runtime>::get(), Some((_, 0))));
		assert_eq!(validator_points(), vec![(1, 50), (11, 40)]);
		assert_eq!(
			ah_client_events_since_last_call(),
			vec![ah_client::Event::Unexpected(UnexpectedKind::SessionReportSendFailed)]
		);

		// last time, we will drop it now.
		NextAhDeliveryFails::set(true);
		roll_next();
		assert!(matches!(ah_client::OutgoingSessionReport::<Runtime>::get(), None));
		assert_eq!(
			ah_client_events_since_last_call(),
			vec![
				ah_client::Event::Unexpected(UnexpectedKind::SessionReportSendFailed),
				ah_client::Event::Unexpected(UnexpectedKind::SessionReportDropped)
			]
		);

		// validator points are restored and merged with what we have noted in the meantime.
		assert_eq!(validator_points(), vec![(1, 150), (11, 640)]);
	})
}
```

**File:** substrate/frame/staking-async/rc-client/src/lib.rs (L480-509)
```rust
#[derive(Encode, Decode, DecodeWithMemTracking, Clone, PartialEq, TypeInfo, MaxEncodedLen)]
/// The information that is sent from RC -> AH on session end.
pub struct SessionReport<AccountId> {
	/// The session that is ending.
	///
	/// This always implies start of `end_index + 1`, and planning of `end_index + 2`.
	pub end_index: SessionIndex,
	/// All of the points that validators have accumulated.
	///
	/// This can be either from block authoring, or from parachain consensus, or anything else.
	pub validator_points: Vec<(AccountId, u32)>,
	/// If none, it means no new validator set was activated as a part of this session.
	///
	/// If `Some((timestamp, id))`, it means that the new validator set was activated at the given
	/// timestamp, and the id of the validator set is `id`.
	///
	/// This `id` is what was previously communicated to the RC as a part of
	/// [`ValidatorSetReport::id`].
	pub activation_timestamp: Option<(u64, u32)>,
	/// If this session report is self-contained, then it is false.
	///
	/// If this session report has some leftover, it should not be acted upon until a subsequent
	/// message with `leftover = true` comes in. The client pallets should handle this queuing.
	///
	/// This is in place to future proof us against possibly needing to send multiple rounds of
	/// messages to convey all of the `validator_points`.
	///
	/// Upon processing, this should always be true, and it should be ignored.
	pub leftover: bool,
}
```
