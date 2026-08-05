Given the constraints, I found a concrete, test-confirmed local analog: the slashing-reward payout path in `substrate/frame/staking-async` silently drops legitimate reporters instead of splitting the reward among all of them, exactly mirroring the "liquidator receives less than expected, silently, with no revert" pattern from the external report.

### Title
Offence reporters can receive zero reward despite valid participation due to silent truncation to a single reporter - (File: `substrate/frame/staking-async/src/slashing.rs`)

### Summary
`pay_reporters` in `staking-async` computes and distributes the slash-reward pot to the reporters recorded on an `UnappliedSlash`. However, `UnappliedSlash` in `staking-async` only stores a single `Option<AccountId>` reporter, while the upstream offence-reporting interface still allows multiple reporters to be attributed to the same offence. When an offence is reported with more than one reporter, only the first is ever paid; the rest silently receive nothing, with no error, event, or revert to signal the shortfall.

### Finding Description
`apply_slash` builds the payee list for the reward pot from a single optional field: [1](#0-0) 

This differs from the legacy `pallet-staking` model, which stores a full `Vec<AccountId>` of `reporters` and fairly divides the reward among all of them: [2](#0-1) 

`pay_reporters` itself is written generically to split `reward_payout` evenly across however many reporters are passed in: [3](#0-2) 

The unit test `only_first_reporter_receive_the_slice` demonstrates the resulting behavior directly: an offence is reported with `reporters: vec![1, 2]`, yet only account `1` is credited a reward, and account `2`'s balance is asserted to be **unchanged** ("second reporter got nothing"): [4](#0-3) 

Compare this to the legacy pallet's equivalent test, `reporters_receive_their_slice`, where both reporters `1` and `2` each receive `reward_each`, i.e. the reward is actually divided among all listed reporters as `pay_reporters`'s logic intends: [5](#0-4) 

The root cause is the data-model mismatch: the offence pipeline can legitimately attribute multiple reporters to one offence, but `staking-async`'s `UnappliedSlash.reporter: Option<AccountId>` can only carry one of them forward to the payout stage, so any additional reporters are dropped before `pay_reporters` ever sees them. This is structurally identical to the ShortCollateral bug: a payout function that correctly computes/caps an amount internally, but the caller-facing guarantee ("you did the work, you get paid your fair share") is silently violated by state truncation upstream, and there is no revert, no minimum-payout check, and no event indicating the second reporter got nothing.

### Impact Explanation
This falls under "duplicate settlement or payout ... wrong beneficiary or amount" in the impact gate: a legitimate, protocol-recognized offence reporter (an unprivileged, permissionless actor — anyone can submit equivocation/offence reports) does real, useful work (helping secure the chain by reporting misbehavior) but receives none of the promised bounty, while the "lost" reward is absorbed into `T::Slash::on_unbalanced` (i.e., effectively burned/redirected to the slash-fund destination) instead of the rightful second reporter. This breaks the "settle exactly once to the rightful beneficiary and amount" invariant for bridge/staking reward payouts.

### Likelihood Explanation
This is not a hypothetical edge case — it is exercised and explicitly documented by the repository's own test (`only_first_reporter_receive_the_slice`), meaning any offence with two or more independent reporters in `staking-async` deployments will trigger this outcome deterministically, not probabilistically. No malicious validator, collator, relayer, governance action, or leaked key is required — an ordinary honest reporter simply loses their reward because someone else also reported the same offence.

### Recommendation
Change `UnappliedSlash` in `staking-async` to carry the full `Vec<AccountId>` (or equivalent bounded collection) of reporters, matching the legacy `pallet-staking` model, and thread that list through `apply_slash` → `pay_reporters` unchanged, instead of collapsing to `Option<AccountId>` via `.map(|v| vec![v])`. If a single-reporter model is intentional going forward, the offence-reporting interface (`rc_client::Offence`) should reject/ignore additional reporters explicitly and emit an event/warning when multiple reporters are supplied but only one will be paid, rather than silently dropping them at the storage-conversion boundary.

### Proof of Concept
The existing repository test already demonstrates the vulnerable state transition: [6](#0-5) 
Two reporters (`1` and `2`) are attributed to the same offence; after slashing is applied, reporter `1`'s balance increases by the expected reward while reporter `2`'s balance is asserted unchanged — confirming that the second reporter's rightful share is lost, not merely delayed or reduced proportionally.

### Citations

**File:** substrate/frame/staking-async/src/slashing.rs (L651-656)
```rust
	pay_reporters::<T>(
		reward_payout,
		slashed_imbalance,
		&unapplied_slash.reporter.map(|v| crate::vec![v]).unwrap_or_default(),
	);
}
```

**File:** substrate/frame/staking-async/src/slashing.rs (L658-688)
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

**File:** substrate/frame/staking/src/lib.rs (L882-886)
```rust
	/// Reporters of the offence; bounty payout recipients.
	pub reporters: Vec<AccountId>,
	/// The amount of payout.
	pub payout: Balance,
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

**File:** substrate/frame/staking/src/tests.rs (L2557-2576)
```rust
#[test]
fn reporters_receive_their_slice() {
	// This test verifies that the reporters of the offence receive their slice from the slashed
	// amount.
	ExtBuilder::default().build_and_execute(|| {
		// The reporters' reward is calculated from the total exposure.
		let initial_balance = 1125;

		assert_eq!(Staking::eras_stakers(active_era(), &11).total, initial_balance);

		on_offence_now(&[offence_from(11, Some(vec![1, 2]))], &[Perbill::from_percent(50)]);

		// F1 * (reward_proportion * slash - 0)
		// 50% * (10% * initial_balance / 2)
		let reward = (initial_balance / 20) / 2;
		let reward_each = reward / 2; // split into two pieces.
		assert_eq!(asset::total_balance::<Test>(&1), 10 + reward_each);
		assert_eq!(asset::total_balance::<Test>(&2), 20 + reward_each);
	});
}
```
