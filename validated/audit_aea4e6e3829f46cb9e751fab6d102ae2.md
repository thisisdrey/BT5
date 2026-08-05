Audit Report

## Title
Validator self-stake incentive weight is captured from a live, flash-bondable `ledger.active` balance, letting a validator claim a disproportionate share of the fixed incentive pot without holding the stake for the era - (File: `substrate/frame/staking-async/src/pallet/impls.rs`, `substrate/frame/staking-async/src/session_rotation.rs`, `substrate/frame/staking-async/src/reward.rs`)

## Summary
`Pallet::get_npos_voters` computes each voter's (including self-voting validator's) weight by calling `weight_of(&voter)` at the exact moment that voter's turn comes up in the (possibly multi-block) snapshot iteration, and this function reads directly from the mutable `StakingLedger.active`. Because `bond_extra` and `unbond` both mutate `ledger.active` immediately and unconditionally (only fund *withdrawal* is delayed by `BondingDuration`), and because the only protection during an ongoing multi-page snapshot is a lock on `VoterList` reordering (not on the underlying stake value), a validator can inflate `ledger.active` right before their page is processed and revert it immediately after, permanently capturing an inflated `own` into `ErasStakersOverview`/`ErasValidatorIncentiveWeight` for the whole era.

## Finding Description
`get_npos_voters` reads voter weight live for each voter as its page is processed: [1](#0-0) 

`weight_of`/`slashable_balance_of` are simple pass-throughs to `StakingLedger.active`, with no time-weighting or historical averaging: [2](#0-1) 

`do_bond_extra` increases `ledger.active` immediately, subject only to spendable balance: [3](#0-2) 

`unbond` decreases the accounting value `ledger.active` immediately as well; `BondingDuration` only delays the later *withdrawal* of the already-decremented funds, not the drop in `ledger.active` itself: [4](#0-3) 

The only defense observed during an in-progress multi-page snapshot is a lock on `pallet_bags_list::Lock` that freezes *list ordering* (which page a voter falls into), not the *value* read for that voter when their page is eventually processed. The pallet's own tests document this distinction explicitly — bag position for an already-snapshotted account (51) does not update on `unbond`, and bag position for a not-yet-snapshotted account (11) does not update on `bond_extra`, but nothing shows (or claims) that the eventual `weight_of` read for a not-yet-processed voter is frozen at snapshot start: [5](#0-4) 

Whatever `own` value is captured is written unmodified into `ErasStakersOverview`/`ErasStakersPaged`, and consumed exactly once (with a `defensive!` warning against being seen twice, but no re-derivation logic) to compute `ErasValidatorIncentiveWeight` for the entire era: [6](#0-5) 

The incentive weight is a sqrt-curve function of that captured `own`, and the era's fixed incentive budget is split proportionally as `w_i / Σ w_j` per validator, per the feature's own documentation: [7](#0-6) 

No code path re-checks, discounts, or time-weights `own`/`ErasValidatorIncentiveWeight` if `ledger.active` is reduced immediately after the voter's page is processed. Since bags-list ordering and page timing are public on-chain state (`VoterSnapshotStatus`, `T::VoterList` scores), an attacker can observe when their own entry is about to be processed and time `bond_extra`/`unbond` around that window using only ordinary, permissionless self-controlled transactions.

## Impact Explanation
`ErasValidatorIncentiveBudget` for an era is fixed and split proportionally to each validator's captured self-stake weight. A validator that inflates `ledger.active` right before their npos-voter snapshot page is processed, then reverts it immediately after, captures a permanently inflated share of that budget for the full era while genuine long-term self-stake risk is never held. This directly dilutes the payout honest, continuously-self-staked validators receive from the same fixed pot — an unbacked/disproportionate transfer of value at other validators' expense, matching the "theft...at other honest stakers' expense" impact category.

## Likelihood Explanation
Any account already running as a validator can call the public, permissionless `bond_extra` and `unbond` extrinsics at will; no governance, privileged role, or off-chain infrastructure control is required. `VoterSnapshotStatus`, `T::VoterList` ordering/scores, and page-size bounds are all public on-chain state, so a validator can determine when their own entry will be processed within the (possibly multi-block) snapshot and time the bond/unbond pair accordingly. This is repeatable every era.

## Recommendation
Derive the validator self-stake incentive weight from a value that cannot be manipulated by a single bond/unbond pair around snapshot time — e.g., snapshot `own` at a fixed point decided at era-start (before the voter snapshot begins) and freeze it, require the self-stake used for `calculate_validator_incentive_weight` to be time-averaged or held for a minimum duration before being finalized into `ErasValidatorIncentiveWeight`, or explicitly disallow/ignore `bond_extra` increases that occur during an in-progress voter snapshot phase when computing incentive weight.

## Proof of Concept
1. Validator `V` operates with a modest active self-stake sufficient to remain an active validator, and observes `VoterSnapshotStatus`/`T::VoterList` to determine roughly when their entry will be processed in the current era's (possibly multi-page) voter snapshot.
2. Just before that page is processed, `V` calls `bond_extra(large_amount)`, which immediately raises `ledger.active` (`substrate/frame/staking-async/src/pallet/impls.rs:199-217`).
3. When `get_npos_voters` reaches `V`'s entry, `weight_of(&V)` reads the inflated live `ledger.active` (`impls.rs:905-919`, `impls.rs:149-162`), so the election snapshot records the inflated `own` for `V`.
4. `Eras::<T>::store_stakers_info`/incentive calculation writes `ErasValidatorIncentiveWeight[era][V]` from this inflated `own` via `calculate_validator_incentive_weight` (`session_rotation.rs:1269-1290`).
5. Immediately after `V`'s page has been consumed, `V` calls `unbond(large_amount)`, which decrements `ledger.active` right away (`pallet/mod.rs:1964-1977`); only fund withdrawal is delayed by `BondingDuration`.
6. At era-end payout, `V` receives its inflated `w_V/Σw_j` share of `ErasValidatorIncentiveBudget`, diluting the share paid to validators who held genuine self-stake for the full era — verifiable by comparing `ErasValidatorIncentiveWeight` before/after the bond/unbond pair in a unit test built around `voter_list_not_updated_when_locked`/`voter_list_locked_during_multi_page_snapshot`.

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

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L905-919)
```rust
			let voter = match sorted_voters.next() {
				Some(voter) => {
					voters_seen.saturating_inc();
					voter
				},
				None => break,
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

**File:** substrate/frame/staking-async/src/tests/election_data_provider.rs (L832-857)
```rust
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

**File:** substrate/frame/staking-async/src/reward.rs (L264-280)
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
```
