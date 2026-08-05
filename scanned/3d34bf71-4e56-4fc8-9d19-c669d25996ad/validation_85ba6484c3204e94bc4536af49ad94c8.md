### Title
`chill_other` capacity gate can be bypassed with a transient Sybil-nominator flood, forcing premature eviction of legitimate under-min-bond nominators - (File: `substrate/frame/staking/src/pallet/mod.rs`, also mirrored in `substrate/frame/staking-async/src/pallet/mod.rs`)

### Summary
`pallet-staking::chill_other` is a permissionless extrinsic that lets *anyone* force-chill another stash, but only once the pool of nominators/validators is judged to be "near capacity" — a gate computed from the mutable, real-time counters `Nominators::<T>::count()` / `Validators::<T>::count()` compared against `ChillThreshold * MaxNominatorsCount/MaxValidatorsCount`. Exactly like the Balancer BPT-share denominator that Bob temporarily inflated with a flash-loaned deposit/withdrawal to make an otherwise-blocked emergency action callable, an attacker can temporarily inflate `Nominators::<T>::count()` with disposable low-stake accounts, trigger `chill_other` against a legitimate low-stake (but tolerated) nominator, and then immediately deflate the counter back down by chilling the Sybil accounts — restoring the pre-attack state while leaving the victim forcibly evicted from nomination.

### Finding Description
`chill_other` in [1](#0-0)  allows a third-party caller to chill a `stash` when `caller != controller`, gated by:

```
let threshold = ChillThreshold::<T>::get()...
let current_nominator_count = Nominators::<T>::count();
ensure!(threshold * max_nominator_count < current_nominator_count, ...);
ensure!(ledger.active < min_active_bond, ...);
```

The gate deliberately only allows this "kick" when the network is *close to capacity* (`threshold * max_nominator_count < current_count`) — the intent being that under-bonded nominators are only evicted when their slot could be better used by a fully-bonded one. However, `Nominators::<T>::count()` is a live counter that any signed account can raise or lower nearly for free:
- `nominate()` adds an account to `Nominators` as soon as it is bonded (no `MinNominatorBond` enforcement at nomination time — that check only exists retroactively in `chill_other`), and
- `chill()` removes the caller from `Nominators` immediately, with no unbonding delay for the *nomination flag* itself (only unbonding actual funds has a delay, chilling a nomination does not).

Because both the inflation (bond small ED + `nominate`) and the deflation (`chill`) of the counter are instantaneous, permissionless, self-authorized actions with no lock-up, an attacker can:
1. Create `k` disposable stash accounts, each bonding only the existential deposit, and call `nominate` for all of them.
2. This pushes `Nominators::<T>::count()` from a genuinely comfortable level up past `threshold * max_nominator_count`, satisfying the capacity check that was never actually true in steady state.
3. Call `chill_other(victim)` against any real nominator whose `ledger.active < MinNominatorBond` (a legitimate, tolerated under-bond nominator that the network was *not* actually short of slots for).
4. Immediately `chill()` all `k` Sybil accounts, restoring `Nominators::<T>::count()` to its original value — erasing evidence that the capacity condition was ever synthetically manufactured.

All of steps 1–4 can be composed into a single atomic transaction (e.g. via `pallet-utility::batch_all`), so the exploit succeeds or reverts entirely, exactly mirroring the atomic flash-loan sequence in the Balancer report — the corrupted value here is `Nominators::<T>::count()` (the denominator-equivalent of Balancer's `totalSupply`), and the "no existing guard stops the path" fact is that the threshold check reads only the current storage count with no time-averaging, no minimum dwell-time, and no protection against an attacker inflating and deflating it within one block.

### Impact Explanation
The victim is forcibly chilled (`Self::chill_stash`), losing their nomination for the following era(s) without having actually violated the intended "pool near capacity" invariant — this is a runtime bug that compromises intended behavior of a public dispatchable (`chill_other` is meant to enforce a genuine congestion policy, not to be weaponizable on demand). Repeated application lets any well-funded attacker selectively evict any under-`MinNominatorBond` nominator at will, at negligible/reversible cost (only fees + refundable EDs), which is a denial-of-service against specific stakers' reward eligibility — the closest local analog to the Balancer report's "attacker bypasses a supply-based threshold to trigger an otherwise-blocked emergency action against victims at near-zero cost."

The impact is bounded (loss of a nomination/era rewards, not fund loss or chain-wide halt), which is materially milder than the original vault-lock scenario, so this should be scoped as a lower-severity DoS/griefing finding rather than a critical fund-loss bug.

### Likelihood Explanation
Requires no privileged role, no malicious validator/collator/relayer, and no off-chain infrastructure — only ordinary signed accounts and existential-deposit-level funds, executed via standard public extrinsics (`bond`, `nominate`, `chill`, `chill_other`), optionally batched atomically via `pallet-utility`. It does, however, require the chain's governance to have actually configured `MinNominatorBond`, `MaxNominatorsCount`, and `ChillThreshold` simultaneously (all three must be set for `chill_other` to be actionable at all), and it requires headroom between the real nominator count and `MaxNominatorsCount` so Sybil nominations don't themselves hit the cap. This makes exploitation config-dependent but not privileged — a reasonably likely condition on any chain that actually enables the `chill_other` congestion-relief feature.

### Recommendation
- Do not gate `chill_other` purely on the instantaneous value of `Nominators::<T>::count()` / `Validators::<T>::count()`; require the "near capacity" condition to have held for a minimum number of blocks/eras (a debounced or era-snapshotted count) so it cannot be manufactured and reverted within a single block/transaction.
- Alternatively/also, enforce `MinNominatorBond` at `nominate()` time (not only retroactively in `chill_other`), which would remove the ability to cheaply mint many nomination-count entries with sub-`MinNominatorBond` stakes purely to move the threshold.
- Consider rate-limiting or charging a non-refundable fee per `nominate`/`chill` cycle to make Sybil-count inflation costly rather than essentially free.

### Proof of Concept
Given a chain with `MinNominatorBond = B`, `MaxNominatorsCount = M`, `ChillThreshold = T` all configured (as required by `chill_other`, see the 8-case matrix in [2](#0-1) ), and a real nominator `V` bonded with `active < B` who is tolerated because `Nominators::count() <= T*M`:

1. Attacker batches (via `utility.batch_all`) `k` calls of `Staking::bond(stash_i, ExistentialDeposit, ...)` + `Staking::nominate(stash_i, [..])` for `i in 1..=k`, chosen so that `Nominators::<T>::count() + k > T * M`.
2. In the same batch, call `Staking::chill_other(caller, V)` — this now passes the `ensure!(threshold * max_nominator_count < current_nominator_count, ...)` check (per the logic at [1](#0-0) ) and the `ledger.active < min_active_bond` check, chilling `V`.
3. In the same batch, call `Staking::chill(stash_i)` for each `i in 1..=k`, restoring `Nominators::<T>::count()` to its pre-attack value.
4. The batch commits atomically: `V` is chilled, the attacker's Sybil stakes are refundable (only ED locked, no unbonding needed since the accounts were never actually elected), and the manufactured "near capacity" condition leaves no persistent trace.

This exact multi-configuration gating logic and its test coverage (which only tests genuine/organic count changes, not transient/self-reversing manipulation) is visible in [3](#0-2)  and the staking-async mirror in [4](#0-3) .

### Citations

**File:** substrate/frame/staking/src/pallet/mod.rs (L1988-2013)
```rust
			if caller != controller {
				let threshold = ChillThreshold::<T>::get().ok_or(Error::<T>::CannotChillOther)?;
				let min_active_bond = if Nominators::<T>::contains_key(&stash) {
					let max_nominator_count =
						MaxNominatorsCount::<T>::get().ok_or(Error::<T>::CannotChillOther)?;
					let current_nominator_count = Nominators::<T>::count();
					ensure!(
						threshold * max_nominator_count < current_nominator_count,
						Error::<T>::CannotChillOther
					);
					MinNominatorBond::<T>::get()
				} else if Validators::<T>::contains_key(&stash) {
					let max_validator_count =
						MaxValidatorsCount::<T>::get().ok_or(Error::<T>::CannotChillOther)?;
					let current_validator_count = Validators::<T>::count();
					ensure!(
						threshold * max_validator_count < current_validator_count,
						Error::<T>::CannotChillOther
					);
					MinValidatorBond::<T>::get()
				} else {
					Zero::zero()
				};

				ensure!(ledger.active < min_active_bond, Error::<T>::CannotChillOther);
			}
```

**File:** substrate/frame/staking/src/tests.rs (L5309-5378)
```rust
	ExtBuilder::default()
		.existential_deposit(100)
		.balance_factor(100)
		.min_nominator_bond(1_000)
		.min_validator_bond(1_500)
		.build_and_execute(|| {
			let initial_validators = Validators::<Test>::count();
			let initial_nominators = Nominators::<Test>::count();
			for i in 0..15 {
				let a = 4 * i;
				let b = 4 * i + 2;
				let c = 4 * i + 3;
				asset::set_stakeable_balance::<Test>(&a, 100_000);
				asset::set_stakeable_balance::<Test>(&b, 100_000);
				asset::set_stakeable_balance::<Test>(&c, 100_000);

				// Nominator
				assert_ok!(Staking::bond(RuntimeOrigin::signed(a), 1000, RewardDestination::Stash));
				assert_ok!(Staking::nominate(RuntimeOrigin::signed(a), vec![1]));

				// Validator
				assert_ok!(Staking::bond(RuntimeOrigin::signed(b), 1500, RewardDestination::Stash));
				assert_ok!(Staking::validate(RuntimeOrigin::signed(b), ValidatorPrefs::default()));
			}

			// To chill other users, we need to:
			// * Set a minimum bond amount
			// * Set a limit
			// * Set a threshold
			//
			// If any of these are missing, we do not have enough information to allow the
			// `chill_other` to succeed from one user to another.
			//
			// Out of 8 possible cases, only one will allow the use of `chill_other`, which is
			// when all 3 conditions are met.

			// 1. No limits whatsoever
			assert_ok!(Staking::set_staking_configs(
				RuntimeOrigin::root(),
				ConfigOp::Remove,
				ConfigOp::Remove,
				ConfigOp::Remove,
				ConfigOp::Remove,
				ConfigOp::Remove,
				ConfigOp::Remove,
				ConfigOp::Remove,
			));

			// Can't chill these users
			assert_noop!(
				Staking::chill_other(RuntimeOrigin::signed(1337), 0),
				Error::<Test>::CannotChillOther
			);
			assert_noop!(
				Staking::chill_other(RuntimeOrigin::signed(1337), 2),
				Error::<Test>::CannotChillOther
			);

			// 2. Change only the minimum bonds.
			assert_ok!(Staking::set_staking_configs(
				RuntimeOrigin::root(),
				ConfigOp::Set(1_500),
				ConfigOp::Set(2_000),
				ConfigOp::Noop,
				ConfigOp::Noop,
				ConfigOp::Noop,
				ConfigOp::Noop,
				ConfigOp::Noop,
			));

```

**File:** substrate/frame/staking/src/tests.rs (L5511-5537)
```rust
			// 16 people total because tests start with 2 active one
			assert_eq!(Nominators::<Test>::count(), 15 + initial_nominators);
			assert_eq!(Validators::<Test>::count(), 15 + initial_validators);

			// Users can now be chilled down to 7 people, so we try to remove 9 of them (starting
			// with 16)
			for i in 6..15 {
				let b = 4 * i;
				let d = 4 * i + 2;
				assert_ok!(Staking::chill_other(RuntimeOrigin::signed(1337), b));
				assert_eq!(*staking_events().last().unwrap(), Event::Chilled { stash: b });
				assert_ok!(Staking::chill_other(RuntimeOrigin::signed(1337), d));
				assert_eq!(*staking_events().last().unwrap(), Event::Chilled { stash: d });
			}

			// chill a nominator. Limit is not reached, not chill-able
			assert_eq!(Nominators::<Test>::count(), 7);
			assert_noop!(
				Staking::chill_other(RuntimeOrigin::signed(1337), 0),
				Error::<Test>::CannotChillOther
			);
			// chill a validator. Limit is reached, chill-able.
			assert_eq!(Validators::<Test>::count(), 9);
			assert_ok!(Staking::chill_other(RuntimeOrigin::signed(1337), 2));
		})
}

```

**File:** substrate/frame/staking-async/src/tests/bonding.rs (L1789-2054)
```rust
	#[test]
	fn chill_other_works() {
		ExtBuilder::default()
			.existential_deposit(100)
			.balance_factor(100)
			.min_nominator_bond(1_000)
			.min_validator_bond(1_500)
			.build_and_execute(|| {
				let initial_validators = Validators::<Test>::count();
				let initial_nominators = Nominators::<Test>::count();
				for i in 0..15 {
					let a = 4 * i;
					let b = 4 * i + 2;
					asset::set_stakeable_balance::<Test>(&a, 100_000);
					asset::set_stakeable_balance::<Test>(&b, 100_000);

					// Nominator
					assert_ok!(Staking::bond(
						RuntimeOrigin::signed(a),
						1000,
						RewardDestination::Stash
					));
					assert_ok!(Staking::nominate(RuntimeOrigin::signed(a), vec![11]));

					// Validator
					assert_ok!(Staking::bond(
						RuntimeOrigin::signed(b),
						1500,
						RewardDestination::Stash
					));
					assert_ok!(Staking::validate(
						RuntimeOrigin::signed(b),
						ValidatorPrefs::default()
					));
					assert_eq!(
						staking_events_since_last_call(),
						vec![
							Event::Bonded { stash: a, amount: 1000 },
							Event::Bonded { stash: b, amount: 1500 },
							Event::ValidatorPrefsSet {
								stash: b,
								prefs: ValidatorPrefs { commission: Zero::zero(), blocked: false }
							}
						]
					);
				}

				// To chill other users, we need to:
				// * Set a minimum bond amount
				// * Set a limit
				// * Set a threshold
				//
				// If any of these are missing, we do not have enough information to allow the
				// `chill_other` to succeed from one user to another.
				//
				// Out of 8 possible cases, only one will allow the use of `chill_other`, which is
				// when all 3 conditions are met.

				// 1. No limits whatsoever
				assert_ok!(Staking::set_staking_configs(
					RuntimeOrigin::root(),
					ConfigOp::Remove,
					ConfigOp::Remove,
					ConfigOp::Remove,
					ConfigOp::Remove,
					ConfigOp::Remove,
					ConfigOp::Remove,
					ConfigOp::Noop,
					ConfigOp::Noop,
					ConfigOp::Noop,
				));

				// Can't chill these users
				assert_noop!(
					Staking::chill_other(RuntimeOrigin::signed(1337), 0),
					Error::<Test>::CannotChillOther
				);
				assert_noop!(
					Staking::chill_other(RuntimeOrigin::signed(1337), 2),
					Error::<Test>::CannotChillOther
				);

				// 2. Change only the minimum bonds.
				assert_ok!(Staking::set_staking_configs(
					RuntimeOrigin::root(),
					ConfigOp::Set(1_500),
					ConfigOp::Set(2_000),
					ConfigOp::Noop,
					ConfigOp::Noop,
					ConfigOp::Noop,
					ConfigOp::Noop,
					ConfigOp::Noop,
					ConfigOp::Noop,
					ConfigOp::Noop,
				));

				// Still can't chill these users
				assert_noop!(
					Staking::chill_other(RuntimeOrigin::signed(1337), 0),
					Error::<Test>::CannotChillOther
				);
				assert_noop!(
					Staking::chill_other(RuntimeOrigin::signed(1337), 2),
					Error::<Test>::CannotChillOther
				);

				// 3. Add nominator/validator count limits, but no other threshold.
				assert_ok!(Staking::set_staking_configs(
					RuntimeOrigin::root(),
					ConfigOp::Remove,
					ConfigOp::Remove,
					ConfigOp::Set(10),
					ConfigOp::Set(10),
					ConfigOp::Noop,
					ConfigOp::Noop,
					ConfigOp::Noop,
					ConfigOp::Noop,
					ConfigOp::Noop,
				));

				// Still can't chill these users
				assert_noop!(
					Staking::chill_other(RuntimeOrigin::signed(1337), 0),
					Error::<Test>::CannotChillOther
				);
				assert_noop!(
					Staking::chill_other(RuntimeOrigin::signed(1337), 2),
					Error::<Test>::CannotChillOther
				);

				// 4. Add chill threshold, but no other limits
				assert_ok!(Staking::set_staking_configs(
					RuntimeOrigin::root(),
					ConfigOp::Noop,
					ConfigOp::Noop,
					ConfigOp::Remove,
					ConfigOp::Remove,
					ConfigOp::Set(Percent::from_percent(75)),
					ConfigOp::Noop,
					ConfigOp::Noop,
					ConfigOp::Noop,
					ConfigOp::Noop,
				));

				// Still can't chill these users
				assert_noop!(
					Staking::chill_other(RuntimeOrigin::signed(1337), 0),
					Error::<Test>::CannotChillOther
				);
				assert_noop!(
					Staking::chill_other(RuntimeOrigin::signed(1337), 2),
					Error::<Test>::CannotChillOther
				);

				// 5. Add bond and count limits, but no threshold
				assert_ok!(Staking::set_staking_configs(
					RuntimeOrigin::root(),
					ConfigOp::Set(1_500),
					ConfigOp::Set(2_000),
					ConfigOp::Set(10),
					ConfigOp::Set(10),
					ConfigOp::Remove,
					ConfigOp::Remove,
					ConfigOp::Noop,
					ConfigOp::Noop,
					ConfigOp::Noop,
				));

				// Still can't chill these users
				assert_noop!(
					Staking::chill_other(RuntimeOrigin::signed(1337), 0),
					Error::<Test>::CannotChillOther
				);
				assert_noop!(
					Staking::chill_other(RuntimeOrigin::signed(1337), 2),
					Error::<Test>::CannotChillOther
				);

				// 6. Add bond and threshold limits, but no count limits
				assert_ok!(Staking::set_staking_configs(
					RuntimeOrigin::root(),
					ConfigOp::Noop,
					ConfigOp::Noop,
					ConfigOp::Remove,
					ConfigOp::Remove,
					ConfigOp::Set(Percent::from_percent(75)),
					ConfigOp::Noop,
					ConfigOp::Noop,
					ConfigOp::Noop,
					ConfigOp::Noop,
				));

				// Still can't chill these users
				assert_noop!(
					Staking::chill_other(RuntimeOrigin::signed(1337), 0),
					Error::<Test>::CannotChillOther
				);
				assert_noop!(
					Staking::chill_other(RuntimeOrigin::signed(1337), 2),
					Error::<Test>::CannotChillOther
				);

				// 7. Add count limits and a chill threshold, but no bond limits
				assert_ok!(Staking::set_staking_configs(
					RuntimeOrigin::root(),
					ConfigOp::Remove,
					ConfigOp::Remove,
					ConfigOp::Set(10),
					ConfigOp::Set(10),
					ConfigOp::Set(Percent::from_percent(75)),
					ConfigOp::Noop,
					ConfigOp::Noop,
					ConfigOp::Noop,
					ConfigOp::Noop,
				));

				// Still can't chill these users
				assert_noop!(
					Staking::chill_other(RuntimeOrigin::signed(1337), 0),
					Error::<Test>::CannotChillOther
				);
				assert_noop!(
					Staking::chill_other(RuntimeOrigin::signed(1337), 2),
					Error::<Test>::CannotChillOther
				);

				// 8. Add all limits
				assert_ok!(Staking::set_staking_configs(
					RuntimeOrigin::root(),
					ConfigOp::Set(1_500),
					ConfigOp::Set(2_000),
					ConfigOp::Set(10),
					ConfigOp::Set(10),
					ConfigOp::Set(Percent::from_percent(75)),
					ConfigOp::Noop,
					ConfigOp::Noop,
					ConfigOp::Noop,
					ConfigOp::Noop,
				));

				// 16 people total because tests start with 2 active one
				assert_eq!(Nominators::<Test>::count(), 15 + initial_nominators);
				assert_eq!(Validators::<Test>::count(), 15 + initial_validators);

				// Users can now be chilled down to 7 people, so we try to remove 9 of them
				// (starting with 16)
				for i in 6..15 {
					let b = 4 * i;
					let d = 4 * i + 2;
					assert_ok!(Staking::chill_other(RuntimeOrigin::signed(1337), b));
					assert_eq!(*staking_events().last().unwrap(), Event::Chilled { stash: b });
					assert_ok!(Staking::chill_other(RuntimeOrigin::signed(1337), d));
					assert_eq!(*staking_events().last().unwrap(), Event::Chilled { stash: d });
				}

				// chill a nominator. Limit is not reached, not chill-able
				assert_eq!(Nominators::<Test>::count(), 7);
				assert_noop!(
					Staking::chill_other(RuntimeOrigin::signed(1337), 0),
					Error::<Test>::CannotChillOther
				);
				// chill a validator. Limit is reached, chill-able.
				assert_eq!(Validators::<Test>::count(), 9);
				assert_ok!(Staking::chill_other(RuntimeOrigin::signed(1337), 2));
			})
	}
```
