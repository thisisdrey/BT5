### Title
Equivocation self-reporting allows an offending validator to reclaim a share of its own slash - (File: `substrate/frame/grandpa/src/lib.rs`, `substrate/frame/babe/src/equivocation.rs`, `substrate/frame/staking/src/slashing.rs`)

### Summary
The UMA report shows that a punitive transfer (bond penalty) intended to move value from a wrongdoer to a vindicated third party becomes a no-op — or worse, a net gain — when the wrongdoer and the beneficiary are the same entity. The same broken invariant exists in the Substrate equivocation/slashing pipeline: `report_equivocation`/`report_equivocation_unsigned` accept an arbitrary signed `reporter` account with no check that the reporter differs from (or is unaffiliated with) the offending validator. The reporter reward, paid out of the offender's own slashed stake by `pay_reporters`, can therefore be redirected back to the offender, partially refunding the very penalty meant to deter the misbehaviour.

### Finding Description
When GRANDPA/BABE/BEEFY equivocation evidence is submitted via the signed extrinsic `report_equivocation`, the pallet does `ensure_signed(origin)` and forwards that signer as `reporter` directly into `T::EquivocationReportSystem::process_evidence(Some(reporter), evidence)` with no relationship check against the offender extracted from the key-ownership proof: [1](#0-0) 

`process_evidence` (identical shape in babe/beefy) validates the equivocation proof and key-ownership proof, builds the `EquivocationOffence`, and calls `R::report_offence(reporter.into_iter().collect(), offence)` — again, no comparison between `reporter` and `offender`: [2](#0-1) 

The client-side automatic reporting service in `environment.rs` does refrain from reporting "our own equivocation," but this is a local heuristic in the GRANDPA voter, not an on-chain guard, and it only checks the *voting* key, not arbitrary signed accounts: [3](#0-2) 

Because the on-chain call is a plain signed extrinsic, anyone — including the offender or an account controlled by the same entity as the offender — can submit the same equivocation proof themselves and name themselves as `reporter`. When the offence is processed, `apply_slash` splits the slashed imbalance and pays `SlashRewardFraction` of it to the named reporters via `pay_reporters`: [4](#0-3) [5](#0-4) 

The same pattern exists in the newer staking-async pallet: [6](#0-5) [7](#0-6) 

This mirrors the UMA bug exactly: the deterrent (the full slash) is supposed to flow to an economically distinct party (via a genuine independent reporter or being fully burned), but because "reporter" is an unauthenticated, unrelated-to-offender field, the offending entity can redirect part of the punitive value back to itself, weakening the slash's deterrence value — the report waives fees for a "valid and beneficial" report (`Pays::No`), so the self-report costs nothing but gas/weight, while it claws back `SlashRewardFraction * slash` (e.g. up to 100% if `SlashRewardFraction` is set to `Perbill::one()`, as literally demonstrated in the benchmarking code).

### Impact Explanation
An offending validator (or any account it controls) can recover part of its own slash by self-reporting its equivocation before an honest third party does. `SlashRewardFraction` is a configurable runtime parameter (`substrate/frame/staking/src/pallet/mod.rs` `SlashRewardFraction<T>`), and the benchmarking harness sets it to `Perbill::one()` to "make sure reporters actually get rewarded," showing the code path allows up to 100% self-refund under permissible configuration: [8](#0-7) 

Depending on the configured `SlashRewardFraction`, this directly reduces the net cost of equivocating, undermining the staking security model that assumes a fixed disincentive against misbehaviour (double-signing, GRANDPA/BABE/BEEFY equivocation), analogous to the UMA optimistic-oracle bond penalty being nullified when attacker and beneficiary coincide.

### Likelihood Explanation
No privileged role, governance action, or malicious peer/validator collusion beyond the offender's own equivocation is required — any account (unprivileged, ordinary signed origin) can submit `report_equivocation` naming itself as reporter, and nothing in `process_evidence`, `ReportOffence`, or `pay_reporters` verifies reporter/offender independence. The only requirement is that the entity already possesses a valid equivocation proof of its own misbehaviour, which it trivially does since it created the equivocation.

### Recommendation
Add an explicit invariant in `process_evidence` (babe/grandpa/beefy equivocation systems) rejecting or zeroing the reward when the `reporter` account is the same as, or provably controlled by, the `offender` identified via key-ownership proof. Alternatively, remove the reward path for self-submitted signed reports and only pay the `SlashRewardFraction` bonus for unsigned/authorship-attributed reports or reports whose signer is cryptographically distinct from the offender's bonded stash/controller, burning (or sending to treasury) the reward share otherwise — mirroring the UMA fix of diverting the disputed share away from the vindicated-party-equals-attacker case.

### Proof of Concept
1. Validator `V` (controlling session/authority key `K`) double-votes in the same GRANDPA round/BABE slot, producing a valid `EquivocationProof` for `K`.
2. `V` (or an account it controls, `A`) submits `Grandpa::report_equivocation(origin=signed(A), equivocation_proof, key_owner_proof)`.
3. `ensure_signed(origin)` succeeds trivially since any signed account is accepted; `process_evidence` validates the proof and calls `R::report_offence(vec![A], offence)` with no check that `A` is unrelated to `K`/`V`.
4. `pallet-staking`'s offence handling computes the slash and, in `apply_slash`/`pay_reporters`, pays `SlashRewardFraction * slash` to `A`.
5. If `SlashRewardFraction` is non-trivial (up to `Perbill::one()` per the benchmark configuration), `V` recovers a significant portion of its own slashed stake through account `A`, reducing the effective penalty for equivocating — the intended deterrent is partially or fully nullified, exactly as in the UMA report where proposer-equals-disputer nullifies the bond penalty transfer.

### Citations

**File:** substrate/frame/grandpa/src/lib.rs (L200-213)
```rust
		pub fn report_equivocation(
			origin: OriginFor<T>,
			equivocation_proof: Box<EquivocationProof<T::Hash, BlockNumberFor<T>>>,
			key_owner_proof: T::KeyOwnerProof,
		) -> DispatchResultWithPostInfo {
			let reporter = ensure_signed(origin)?;

			T::EquivocationReportSystem::process_evidence(
				Some(reporter),
				(*equivocation_proof, key_owner_proof),
			)?;
			// Waive the fee since the report is valid and beneficial
			Ok(Pays::No.into())
		}
```

**File:** substrate/frame/babe/src/equivocation.rs (L162-198)
```rust
	fn process_evidence(
		reporter: Option<T::AccountId>,
		evidence: (EquivocationProof<HeaderFor<T>>, T::KeyOwnerProof),
	) -> Result<(), DispatchError> {
		let (equivocation_proof, key_owner_proof) = evidence;
		let reporter = reporter.or_else(|| <pallet_authorship::Pallet<T>>::author());
		let offender = equivocation_proof.offender.clone();
		let slot = equivocation_proof.slot;

		// Validate the equivocation proof (check votes are different and signatures are valid)
		if !sp_consensus_babe::check_equivocation_proof(equivocation_proof) {
			return Err(Error::<T>::InvalidEquivocationProof.into());
		}

		let validator_set_count = key_owner_proof.validator_count();
		let session_index = key_owner_proof.session();

		let epoch_index =
			*slot.saturating_sub(crate::GenesisSlot::<T>::get()) / T::EpochDuration::get();

		// Check that the slot number is consistent with the session index
		// in the key ownership proof (i.e. slot is for that epoch)
		if Pallet::<T>::session_index_for_epoch(epoch_index) != session_index {
			return Err(Error::<T>::InvalidKeyOwnershipProof.into());
		}

		// Check the membership proof and extract the offender's id
		let offender = P::check_proof((KEY_TYPE, offender), key_owner_proof)
			.ok_or(Error::<T>::InvalidKeyOwnershipProof)?;

		let offence = EquivocationOffence { slot, validator_set_count, offender, session_index };

		R::report_offence(reporter.into_iter().collect(), offence)
			.map_err(|_| Error::<T>::DuplicateOffenceReport)?;

		Ok(())
	}
```

**File:** substrate/client/consensus/grandpa/src/environment.rs (L501-507)
```rust
		if let Some(local_id) = self.voter_set_state.voting_on(equivocation.round_number()) {
			if *equivocation.offender() == local_id {
				return Err(Error::Safety(
					"Refraining from sending equivocation report for our own equivocation.".into(),
				));
			}
		}
```

**File:** substrate/frame/staking/src/slashing.rs (L592-619)
```rust
/// Apply a previously-unapplied slash.
pub(crate) fn apply_slash<T: Config>(
	unapplied_slash: UnappliedSlash<T::AccountId, BalanceOf<T>>,
	slash_era: EraIndex,
) {
	let mut slashed_imbalance = NegativeImbalanceOf::<T>::zero();
	let mut reward_payout = unapplied_slash.payout;

	do_slash::<T>(
		&unapplied_slash.validator,
		unapplied_slash.own,
		&mut reward_payout,
		&mut slashed_imbalance,
		slash_era,
	);

	for &(ref nominator, nominator_slash) in &unapplied_slash.others {
		do_slash::<T>(
			nominator,
			nominator_slash,
			&mut reward_payout,
			&mut slashed_imbalance,
			slash_era,
		);
	}

	pay_reporters::<T>(reward_payout, slashed_imbalance, &unapplied_slash.reporters);
}
```

**File:** substrate/frame/staking/src/slashing.rs (L621-651)
```rust
/// Apply a reward payout to some reporters, paying the rewards out of the slashed imbalance.
fn pay_reporters<T: Config>(
	reward_payout: BalanceOf<T>,
	slashed_imbalance: NegativeImbalanceOf<T>,
	reporters: &[T::AccountId],
) {
	if reward_payout.is_zero() || reporters.is_empty() {
		// nobody to pay out to or nothing to pay;
		// just treat the whole value as slashed.
		T::Slash::on_unbalanced(slashed_imbalance);
		return;
	}

	// take rewards out of the slashed imbalance.
	let reward_payout = reward_payout.min(slashed_imbalance.peek());
	let (mut reward_payout, mut value_slashed) = slashed_imbalance.split(reward_payout);

	let per_reporter = reward_payout.peek() / (reporters.len() as u32).into();
	for reporter in reporters {
		let (reporter_reward, rest) = reward_payout.split(per_reporter);
		reward_payout = rest;

		// this cancels out the reporter reward imbalance internally, leading
		// to no change in total issuance.
		asset::deposit_slashed::<T>(reporter, reporter_reward);
	}

	// the rest goes to the on-slash imbalance handler (e.g. treasury)
	value_slashed.subsume(reward_payout); // remainder of reward division remains.
	T::Slash::on_unbalanced(value_slashed);
}
```

**File:** substrate/frame/staking-async/src/slashing.rs (L622-656)
```rust
/// Apply a previously-unapplied slash.
pub(crate) fn apply_slash<T: Config>(unapplied_slash: UnappliedSlash<T>, offence_era: EraIndex) {
	let mut slashed_imbalance = NegativeImbalanceOf::<T>::zero();
	let mut reward_payout = unapplied_slash.payout;

	if unapplied_slash.own > Zero::zero() {
		do_slash::<T>(
			&unapplied_slash.validator,
			unapplied_slash.own,
			&mut reward_payout,
			&mut slashed_imbalance,
			offence_era,
		);
	}

	for &(ref nominator, nominator_slash) in &unapplied_slash.others {
		if nominator_slash.is_zero() {
			continue;
		}

		do_slash::<T>(
			nominator,
			nominator_slash,
			&mut reward_payout,
			&mut slashed_imbalance,
			offence_era,
		);
	}

	pay_reporters::<T>(
		reward_payout,
		slashed_imbalance,
		&unapplied_slash.reporter.map(|v| crate::vec![v]).unwrap_or_default(),
	);
}
```

**File:** substrate/frame/staking-async/src/slashing.rs (L658-687)
```rust
/// Apply a reward payout to some reporters, paying the rewards out of the slashed imbalance.
fn pay_reporters<T: Config>(
	reward_payout: BalanceOf<T>,
	slashed_imbalance: NegativeImbalanceOf<T>,
	reporters: &[T::AccountId],
) {
	if reward_payout.is_zero() || reporters.is_empty() {
		// nobody to pay out to or nothing to pay;
		// just treat the whole value as slashed.
		T::Slash::on_unbalanced(slashed_imbalance);
		return;
	}

	// take rewards out of the slashed imbalance.
	let reward_payout = reward_payout.min(slashed_imbalance.peek());
	let (mut reward_payout, mut value_slashed) = slashed_imbalance.split(reward_payout);

	let per_reporter = reward_payout.peek() / (reporters.len() as u32).into();
	for reporter in reporters {
		let (reporter_reward, rest) = reward_payout.split(per_reporter);
		reward_payout = rest;

		// this cancels out the reporter reward imbalance internally, leading
		// to no change in total issuance.
		asset::deposit_slashed::<T>(reporter, reporter_reward);
	}

	// the rest goes to the on-slash imbalance handler (e.g. treasury)
	value_slashed.subsume(reward_payout); // remainder of reward division remains.
	T::Slash::on_unbalanced(value_slashed);
```

**File:** substrate/frame/offences/benchmarking/src/inner.rs (L222-235)
```rust
		let reporters = vec![account("reporter", 1, SEED)];

		// make sure reporters actually get rewarded
		Staking::<T>::set_slash_reward_fraction(Perbill::one());

		let mut offenders = make_offenders::<T>(1, n)?;
		let validator_set_count = Session::<T>::validators().len() as u32;

		let offence = GrandpaEquivocationOffence {
			time_slot: GrandpaTimeSlot { set_id: 0, round: 0 },
			session_index: 0,
			validator_set_count,
			offender: T::convert(offenders.pop().unwrap()),
		};
```
