Audit Report

## Title
Offence reporters can receive zero reward despite valid participation due to silent truncation to a single reporter - (File: `substrate/frame/staking-async/src/slashing.rs`)

## Summary
`apply_slash` in `substrate/frame/staking-async/src/slashing.rs` collapses `unapplied_slash.reporter` (an `Option<AccountId>`) into a single-element vector before calling `pay_reporters`, even though the upstream offence-reporting interface (`rc_client::Offence`) permits multiple reporters per offence via `reporters: Vec<AccountId>`. As a result, when more than one account legitimately reports the same offence, only one reporter is ever credited a reward share, and the rest silently receive nothing, with the unpaid share instead flowing to `T::Slash::on_unbalanced`.

## Finding Description
`apply_slash` builds the reporter list passed to `pay_reporters` from a single optional field: [1](#0-0) 

`pay_reporters` itself correctly implements even division of the reward pot across however many reporters are supplied: [2](#0-1) 

The bug is not in `pay_reporters`'s division logic but in the caller: `UnappliedSlash.reporter: Option<AccountId>` structurally cannot carry more than one reporter forward from the offence-reporting stage, whereas `rc_client::Offence.reporters: Vec<AccountId>` allows multiple. Any reporters beyond the first (or however the `Option` is populated) are dropped before `pay_reporters` ever sees them — there is no error, event, or revert indicating the shortfall. This is confirmed directly by the repository's own test, which reports an offence with `reporters: vec![1, 2]` and asserts account `2`'s balance is unchanged ("second reporter got nothing") while account `1` receives the full expected reward share: [3](#0-2) 

This contrasts with the legacy `pallet-staking` model, which retains the full `Vec<AccountId>` of reporters through to payout and fairly splits the reward among all of them, as shown by its analogous test `reporters_receive_their_slice` where both reporters receive `reward_each`.

## Impact Explanation
This is a payout/beneficiary-correctness bug: a legitimate, permissionless, unprivileged offence reporter does real work (submitting a valid equivocation/offence report) but is silently denied their promised bounty share due to a data-model truncation, while the unpaid reward is redirected to `T::Slash::on_unbalanced` rather than the rightful second reporter. This matches "duplicate settlement or payout ... wrong beneficiary or amount" — the exact corrupted value is the reporter reward payout, where a valid second reporter's beneficiary amount is silently zeroed instead of receiving its fair `per_reporter` split.

## Likelihood Explanation
This is deterministic, not probabilistic: it is directly exercised by the maintainers' own test `only_first_reporter_receive_the_slice`. Any offence in a `staking-async` deployment reported by more than one account will trigger this outcome every time, requiring no malicious validator, governance action, or privileged capability — merely two honest, independent reporters submitting reports for the same offence.

## Recommendation
Change `UnappliedSlash` in `staking-async` to store the full `Vec<AccountId>` (or a bounded equivalent) of reporters, matching `pallet-staking`'s model, and thread that list unchanged from the offence-reporting interface through `apply_slash` into `pay_reporters`, rather than collapsing it to `Option<AccountId>` via `.map(|v| vec![v])`. If a single-reporter model is intentional, the offence-reporting interface should explicitly reject or flag multiple reporters with a corresponding event, rather than silently dropping them at the storage-conversion boundary.

## Proof of Concept
The existing repository test demonstrates the vulnerable state transition end-to-end: an offence is reported via `<Staking as rc_client::AHStakingInterface>::on_new_offences` with `reporters: vec![1, 2]`; after the slash is applied, reporter `1`'s balance increases by `reward = 500 / 10`, while reporter `2`'s balance is asserted unchanged, confirming the second reporter's rightful share is lost rather than reduced proportionally. [4](#0-3)

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
