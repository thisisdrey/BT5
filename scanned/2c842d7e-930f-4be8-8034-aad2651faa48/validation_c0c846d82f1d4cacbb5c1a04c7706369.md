### Title
Validator self-stake incentive weight is snapshotted from a live, flash-inflatable `ledger.active` balance, letting a validator capture a disproportionate share of the fixed incentive pot without holding the stake for the era - (File: `substrate/frame/staking-async/src/pallet/impls.rs`, `substrate/frame/staking-async/src/session_rotation.rs`, `substrate/frame/staking-async/src/reward.rs`)

### Summary
The Union Finance bug rewarded a staker with the full "locked" multiplier from a value (`lockedStake`) that was read at claim time with no accounting for how long the lock had actually existed, letting an attacker flash-lock/unlock within one transaction and collect rewards meant for genuine long-term risk, at other honest stakers' expense. The same broken invariant — a reward-share input sampled once from a mutable balance instead of integrated over a committed duration — exists in `pallet-staking-async`'s new validator self-stake incentive: the "own stake" fed into the sqrt-based `incentive_weight` curve is read live from `StakingLedger.active` at NPoS-voter-snapshot time via `Self::weight_of`, and `bond_extra`/`unbond` mutate `ledger.active` instantly. A validator can bond extra funds right before/at the moment their entry is iterated in the election voter snapshot, then unbond immediately after, permanently baking the inflated self-stake into that era's `ErasValidatorIncentiveWeight`/`ErasStakersOverview` without ever holding the larger stake for any meaningful duration of the era.

### Finding Description
`Pallet::get_npos_voters` builds the NPoS voter set for the election snapshot and, for each voter (including self-voting validators), computes their weight with a **live** call: [1](#0-0) 

`weight_of` / `slashable_balance_of` read directly from the current `StakingLedger.active`, not from any cached, time-weighted, or previously-snapshotted score: [2](#0-1) 

`bond_extra` increases `ledger.active` immediately and unconditionally (subject only to the caller's spendable balance): [3](#0-2) 

`unbond` reduces `ledger.active` immediately as well (only the *withdrawal* of funds, not the accounting decrease, is delayed by `BondingDuration`): [4](#0-3) 

Whatever `own` value is captured for the validator's self-vote at snapshot time is carried, unmodified for the rest of the era, into `ErasStakersOverview`/`ErasStakersPaged` via `upsert_exposure`, and used exactly once to compute the validator's incentive weight for the entire era: [5](#0-4) 

That weight is the sqrt-curve function of self-stake, and the total budget for the era is split proportionally as `share_i = w_i / Σ w_j` (per the PRDoc for this feature): [6](#0-5) [7](#0-6) 

Nothing re-derives or discounts `own`/`ErasValidatorIncentiveWeight` if the validator's active stake is reduced immediately after the snapshot; existing tests explicitly document that bag position (not the raw weight fed to the election) is frozen while the snapshot is locked, but a live `bond_extra`/`unbond` right around a validator's own turn in the snapshot iteration is not defended against: [8](#0-7) 

### Impact Explanation
The validator incentive pot (`ErasValidatorIncentiveBudget`) for an era is fixed; a validator that inflates its recorded self-stake at snapshot time and then unbonds takes a larger `w_i/Σw_j` slice of that fixed budget for the entire era while never actually bearing the corresponding slashing/opportunity-cost risk for that duration. This directly reduces the payout that honest, genuinely self-staked validators receive from the same pot — the same "at the expense of other honest stakers" outcome described in the Union Finance report, now expressed through a runtime reward-accounting invariant rather than direct fund theft.

### Likelihood Explanation
Any account already acting as a validator (unprivileged with respect to this mechanism — no governance, no malicious peer/relayer/collator assumptions) can call the public extrinsics `bond_extra` and `unbond` at will. The only timing requirement is issuing `bond_extra` shortly before their own entry is processed within the (possibly multi-page) voter snapshot phase and `unbond` shortly after — both are ordinary, permissionless, self-controlled transactions, not adversary-controlled infrastructure.

### Recommendation
Compute the validator self-stake incentive weight from a stake value that cannot be manipulated by a single-block bond/unbond pair — e.g., use a time-averaged or era-start-committed active stake, require the self-stake to remain bonded (not just snapshotted) for the full era before the incentive weight is finalized, or apply a minimum holding-period check analogous to bonding duration before `own` is used in `calculate_validator_incentive_weight`.

### Proof of Concept
1. Validator `V` runs with a modest self-stake, sufficient to be an active validator.
2. Just before (or exactly when) the era-planning voter snapshot reaches `V`'s entry in `get_npos_voters`, `V` calls `bond_extra(large_amount)` — `ledger.active` jumps immediately (`substrate/frame/staking-async/src/pallet/impls.rs:199-217`).
3. `weight_of(&V)` in the same snapshot pass reads this inflated live `ledger.active` (`impls.rs:149-162`, `impls.rs:911-919`), so the election result records the inflated `own` for `V`.
4. `store_stakers_info` computes and stores `ErasValidatorIncentiveWeight[era][V]` from this inflated `own` (`session_rotation.rs:1269-1290`).
5. Immediately after the snapshot phase concludes, `V` calls `unbond(large_amount)` — `ledger.active` drops back down right away (`pallet/mod.rs:1964-1977`); only fund *withdrawal* is delayed by `BondingDuration`, the incentive-relevant accounting value is already unaffected by this.
6. At era-end payout, `V` receives its inflated `w_V/Σw_j` share of `ErasValidatorIncentiveBudget` despite having held the larger stake for only the brief snapshot window, diluting the share paid to other validators who maintained genuine stake all era.

### Citations

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L149-162)
```rust
	/// The total balance that can be slashed from a stash account as of right now.
	pub fn slashable_balance_of(stash: &T::AccountId) -> BalanceOf<T> {
		// Weight note: consider making the stake accessible through stash.
		Self::ledger(Stash(stash.clone())).map(|l| l.active).unwrap_or_default()
	}

	/// Internal impl of [`Self::slashable_balance_of`] that returns [`VoteWeight`].
	pub fn slashable_balance_of_vote_weight(
		stash: &T::AccountId,
		issuance: BalanceOf<T>,
	) -> VoteWeight {
		T::CurrencyToVote::to_vote(Self::slashable_balance_of(stash), issuance)
	}

```

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L199-217)
```rust
	pub(super) fn do_bond_extra(stash: &T::AccountId, additional: BalanceOf<T>) -> DispatchResult {
		let mut ledger = Self::ledger(StakingAccount::Stash(stash.clone()))?;

		// for virtual stakers, we don't need to check the balance. Since they are only accessed
		// via low level apis, we can assume that the caller has done the due diligence.
		let extra = if Self::is_virtual_staker(stash) {
			additional
		} else {
			// additional amount or actual balance of stash whichever is lower.
			additional.min(asset::free_to_stake::<T>(stash))
		};

		ledger.total = ledger.total.checked_add(&extra).ok_or(ArithmeticError::Overflow)?;
		ledger.active = ledger.active.checked_add(&extra).ok_or(ArithmeticError::Overflow)?;
		// last check: the new active amount of ledger must be more than min bond.
		ensure!(ledger.active >= Self::min_chilled_bond(), Error::<T>::InsufficientBond);

		// NOTE: ledger must be updated prior to calling `Self::weight_of`.
		ledger.update()?;
```

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L911-919)
```rust
			};

			let voter_weight = weight_of(&voter);
			// if voter weight is zero, do not consider this voter for the snapshot.
			if voter_weight.is_zero() {
				log!(debug, "voter's active balance is 0. skip this voter.");
				continue;
			}

```

**File:** substrate/frame/staking-async/src/pallet/mod.rs (L1964-1977)
```rust
			ensure!(
				ledger.unlocking.len() < T::MaxUnlockingChunks::get() as usize,
				Error::<T>::NoMoreChunks,
			);

			if !value.is_zero() {
				ledger.active -= value;

				// Avoid there being a dust balance left in the staking system.
				if ledger.active < asset::existential_deposit::<T>() {
					value += ledger.active;
					ledger.active = Zero::zero();
				}

```

**File:** substrate/frame/staking-async/src/session_rotation.rs (L1269-1290)
```rust
			// Calculate incentive weight from own-stake. Own-stake appears only on the
			// first page of a multi-page exposure, so if the key already exists with a
			// non-zero own, something is wrong upstream.
			if !own.is_zero() {
				if ErasValidatorIncentiveWeight::<T>::contains_key(new_planned_era, &stash) {
					defensive!(
						"validator own-stake seen twice in the same era across election pages"
					);
				} else {
					let incentive_weight =
						T::StakerRewardCalculator::calculate_validator_incentive_weight(own);
					if !incentive_weight.is_zero() {
						total_incentive_weight_page =
							total_incentive_weight_page.saturating_add(incentive_weight);
						ErasValidatorIncentiveWeight::<T>::insert(
							new_planned_era,
							&stash,
							incentive_weight,
						);
					}
				}
			}
```

**File:** substrate/frame/staking-async/src/reward.rs (L264-307)
```rust
/// Piecewise sqrt-based incentive weight function.
///
/// - Below optimum: `w(s) = √s`
/// - Between optimum and cap: `w(s) = √(T + k² × (s - T))`
/// - Above cap: plateau at `w(cap)`
fn incentive_weight<Balance>(
	self_stake: Balance,
	optimum: Balance,
	cap: Balance,
	slope_factor: Perbill,
) -> Balance
where
	Balance: AtLeast32BitUnsigned + Copy + Into<u128> + From<u128>,
{
	debug_assert!(optimum <= cap, "config invariant: optimum must be <= cap");

	if self_stake.is_zero() {
		return Balance::zero();
	}

	if optimum.is_zero() && cap.is_zero() {
		return Balance::zero();
	}

	let self_stake_u128: u128 = self_stake.into();
	let optimum_u128: u128 = optimum.into();
	let cap_u128: u128 = cap.into();

	let weight_u128 = if self_stake <= optimum {
		sp_arithmetic::helpers_128bit::sqrt(self_stake_u128)
	} else if self_stake <= cap {
		let k_squared = slope_factor.square();
		let excess = self_stake_u128.saturating_sub(optimum_u128);
		let arg = optimum_u128.saturating_add(k_squared.mul_floor(excess));
		sp_arithmetic::helpers_128bit::sqrt(arg)
	} else {
		let k_squared = slope_factor.square();
		let excess = cap_u128.saturating_sub(optimum_u128);
		let arg = optimum_u128.saturating_add(k_squared.mul_floor(excess));
		sp_arithmetic::helpers_128bit::sqrt(arg)
	};

	Balance::from(weight_u128)
}
```

**File:** prdoc/stable2606/pr_11651.prdoc (L1-8)
```text
title: "Validator self-stake incentive curve (non-vested)"
doc:
- audience: Runtime Dev
  description: |-
    Adds a separate validator incentive reward track funded from a second DAP budget pot.
    Each validator's share is determined by a sqrt-based piecewise weight function of their
    self-stake, with governance-configurable parameters (optimum, cap, slope factor).
    Payout is a direct liquid transfer from the era incentive pot.
```

**File:** substrate/frame/staking-async/src/tests/election_data_provider.rs (L820-857)
```rust
				assert_eq!(pallet_bags_list::Lock::<T, VoterBagsListInstance>::get(), None);

				let voters_page_3 = <Staking as ElectionDataProvider>::electing_voters(bounds, 3)
					.unwrap()
					.into_iter()
					.map(|(a, _, _)| a)
					.collect::<Vec<_>>();

				assert_eq!(voters_page_3, vec![51, 41]);
				assert_eq!(VoterSnapshotStatus::<Test>::get(), SnapshotStatus::Ongoing(41));
				assert_eq!(pallet_bags_list::Lock::<T, VoterBagsListInstance>::get(), Some(()));

				// 51 who is already part of the list might want to unbond. They are already in the
				// snapshot, and their position is not updated
				hypothetically!({
					assert_ok!(Staking::unbond(RuntimeOrigin::signed(51), 500));
					// they are still in the original bag
					assert_eq!(
						pallet_bags_list::ListNodes::<T, VoterBagsListInstance>::get(51)
							.unwrap()
							.bag_upper,
						10_000
					);
				});

				// 11 who is not part of the snapshot yet might want to bond a lot extra, this is
				// not reflected in this election.
				hypothetically!({
					crate::asset::set_stakeable_balance::<T>(&11, 10000);
					assert_ok!(Staking::bond_extra(RuntimeOrigin::signed(11), 5000));
					// they are still in the original bag
					assert_eq!(
						pallet_bags_list::ListNodes::<T, VoterBagsListInstance>::get(11)
							.unwrap()
							.bag_upper,
						1000
					);
				});
```
