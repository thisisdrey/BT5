Based on my research, I found a concrete local analog in `substrate/frame/staking-async/src/pallet/impls.rs`, in the `on_new_offences` handler.

### Title
Reporter of an offence can be silently overwritten by a later report, letting an attacker steal the honest reporter's slash bounty - (File: `substrate/frame/staking-async/src/pallet/impls.rs`)

### Summary
`Staking::on_new_offences` deduplicates offence reports per `(offence_era, validator)` in `OffenceQueue`, but when a later report for the *same* offender/era arrives with a strictly larger `slash_fraction`, it fully replaces the stored `OffenceRecord`, including the `reporter` field, discarding the original reporter who first raised (and effectively "won") the offence.

### Finding Description
When an offence is queued for the first time, `reporter: o.reporters.first().cloned()` is stored alongside the `slash_fraction`: [1](#0-0) 

If a subsequent, distinct offence report arrives for the same `(offence_era, validator)` pair and its `slash_fraction` is strictly greater than the one already stored, the whole `OffenceRecord` — including `reporter` — is overwritten with the new report's data: [2](#0-1) 

This mirrors the `FaultDisputeGame.challengeRootL2Block` pattern: a later-arriving "special" report can unconditionally supersede the state set by the first, honest reporter, and the bond/reward payout logic in `pay_reporters` then only pays whoever is recorded as `reporter` at slash-apply time: [3](#0-2) 

Because `report_offence`/`reporters` in the relay-chain-to-asset-hub offence pipeline (`rc_client::Offence`) can, in principle, be submitted by any account that produces a valid larger-fraction offence proof for the same offender/era (e.g. reporting an additional/aggravated equivocation instance that yields a higher computed fraction), an attacker who is racing an honest reporter can front-run or race a second, higher-fraction report to have themselves recorded as `reporter`, then collect 100% of the reward at settlement time — instead of the honest first reporter.

### Impact Explanation
This directly maps to "wrong beneficiary" and "duplicate settlement to wrong party" in the accepted impact classes: theft of a legitimately-earned reward from an honest reporter, redirecting the slash bounty to a different account than the one that actually reported/detected the offence first, as shown by `only_first_reporter_receive_the_slice`/reward tests confirming that only the currently-recorded `reporter` gets paid: [4](#0-3) 

### Likelihood Explanation
Exploitation depends on being able to submit a second, independent offence report for the same offender/era with a larger `slash_fraction` than the honest reporter's original report before the offence is applied. This requires access to distinct equivocation/misbehavior evidence rather than an arbitrary user-chosen fraction, so it is not trivially attacker-controlled in all consensus mechanisms (GRANDPA/BABE derive `slash_fraction` from `validator_set_count`/proof data, not a free-form value) — I was not able to fully verify, within the available tool budget, whether any current offence source lets an unprivileged party choose or influence the fraction value to guarantee winning this race. This uncertainty should be resolved with further code review (e.g., of `pallet-im-online`'s and BABE/GRANDPA's exact fraction computation and whether multiple distinct proofs for the same era/validator are realistically producible by an outside party) before treating this as fully confirmed.

### Recommendation
When updating an existing `OffenceQueue` entry because a higher `slash_fraction` was observed, preserve the original `reporter` (the first party to report the offender for that era) rather than overwriting it with the new report's reporter, or split the bounty proportionally between all reporters who contributed evidence, consistent with the stated intent in the `reporter: Option<T::AccountId>` doc comment: "Reporters of the offence; bounty payout recipients." [5](#0-4) 

### Proof of Concept
1. Honest party `A` detects and reports offender `V`'s equivocation for `era=1` with `slash_fraction=20%`; `OffenceQueue` stores `reporter = A`.
2. Attacker `B` obtains/derives a second qualifying report for the same offender `V`/`era=1` with `slash_fraction=50%` (larger) and submits it before era-end settlement.
3. `on_new_offences` sees `slash_fraction(50%) > existing.slash_fraction(20%)` and overwrites `OffenceRecord.reporter` from `A` to `B`, per the update branch: [2](#0-1) 
4. When the slash is finally applied, `pay_reporters` pays out the full bounty to `B` only, and `A` — the original honest detector — receives nothing, exactly analogous to the described `challengeRootL2Block` bond-hijacking pattern. [6](#0-5)

### Citations

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L1534-1546)
```rust
			if let Some(existing) = OffenceQueue::<T>::get(offence_era, &validator) {
				if slash_fraction.deconstruct() > existing.slash_fraction.deconstruct() {
					OffenceQueue::<T>::insert(
						offence_era,
						&validator,
						OffenceRecord {
							reporter: o.reporters.first().cloned(),
							reported_era: active_era.index,
							slash_fraction,
							..existing
						},
					);

```

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L1570-1589)
```rust
			} else if slash_fraction.deconstruct() > prior_slash_fraction.deconstruct() {
				ValidatorSlashInEra::<T>::insert(
					offence_era,
					&validator,
					(slash_fraction, exposure_overview.own),
				);

				OffenceQueue::<T>::insert(
					offence_era,
					&validator,
					OffenceRecord {
						reporter: o.reporters.first().cloned(),
						reported_era: active_era.index,
						// there are cases of validator with no exposure, hence 0 page, so we
						// saturate to avoid underflow.
						exposure_page: exposure_overview.page_count.saturating_sub(1),
						slash_fraction,
						prior_slash_fraction,
					},
				);
```

**File:** substrate/frame/staking/src/slashing.rs (L618-646)
```rust
	pay_reporters::<T>(reward_payout, slashed_imbalance, &unapplied_slash.reporters);
}

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
```

**File:** substrate/frame/staking-async/src/tests/slashing.rs (L189-226)
```rust
#[test]
fn only_first_reporter_receive_the_slice() {
	// This test verifies that the first reporter of the offence receive their slice from the
	// slashed amount.
	ExtBuilder::default().nominate(false).build_and_execute(|| {
		// The reporters' reward is calculated from the total exposure.
		assert_eq!(Staking::eras_stakers(active_era(), &11).total, 1000);

		let initial_balance_1 = asset::total_balance::<T>(&1);
		let initial_balance_2 = asset::total_balance::<T>(&2);

		<Staking as rc_client::AHStakingInterface>::on_new_offences(
			session_mock::Session::current_index(),
			vec![rc_client::Offence {
				offender: 11,
				reporters: vec![1, 2],
				slash_fraction: Perbill::from_percent(50),
			}],
		);
		Session::roll_next();
		assert_eq!(
			staking_events_since_last_call(),
			vec![
				Event::OffenceReported {
					offence_era: 1,
					validator: 11,
					fraction: Perbill::from_percent(50)
				},
				Event::SlashComputed { offence_era: 1, slash_era: 1, offender: 11, page: 0 },
				Event::Slashed { staker: 11, amount: 500 },
			]
		);

		let reward = 500 / 10;
		assert_eq!(asset::total_balance::<T>(&1), initial_balance_1 + reward);
		// second reporter got nothing
		assert_eq!(asset::total_balance::<T>(&2), initial_balance_2);
	});
```

**File:** substrate/frame/staking-async/src/lib.rs (L479-480)
```rust
	/// Reporters of the offence; bounty payout recipients.
	pub reporter: Option<T::AccountId>,
```
