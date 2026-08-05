Audit Report

## Title
Permissionless `reap_stash` destroys the staking ledger before pending era rewards are claimed, permanently locking staker (and nominators') funds - (File: `substrate/frame/staking/src/pallet/mod.rs`)

## Summary
`Pallet::reap_stash` is callable by any signed account against any stash whose `total_balance` or `ledger.total` has fallen below the existential deposit [1](#0-0) . It performs no check for unclaimed reward pages tracked in `ErasStakersPaged`/`ClaimedRewards` before calling `kill_stash`, which removes `Bonded`, `Ledger`, and `Payee` for the stash [2](#0-1) . Once removed, any subsequent `payout_stakers`/`payout_stakers_by_page` call for that validator fails permanently because it must resolve `Self::bonded(&validator_stash)` to find the controller/ledger.

## Finding Description
`reap_stash` computes `reapable` purely from `origin_balance`/`ledger.total` versus the existential deposit, with no inspection of `pending_rewards`-style state [3](#0-2) . `Pallet::pending_rewards` demonstrates that unclaimed reward existence is tracked completely independently via `ErasStakersOverview`/`ErasStakers` page counts compared against `ClaimedRewards`/`legacy_claimed_rewards`, decoupled from `ledger.total` [4](#0-3) . This means a validator can have `ledger.total < ed` (e.g., after a heavy slash or full unbond) while still holding several eras of unclaimed reward pages within `HistoryDepth`.

`kill_stash` calls `StakingLedger::kill`, which removes `Ledger`, `Bonded`, and `Payee` unconditionally once `reapable` is true [5](#0-4) . After this, `do_payout_stakers` requires `Self::bonded(&validator_stash)` to succeed to locate the controller/ledger; once `Bonded` is removed, this lookup returns `None` and the call fails with `Error::NotStash` [6](#0-5) . `do_payout_stakers_by_page` similarly depends on `Self::ledger(StakingAccount::Stash(...))`, which fails once the ledger is removed [7](#0-6) . Since `payout_stakers` is the only entrypoint that resolves and pays out `ErasStakersPaged` reward pages (for both the validator and its backing nominators), destroying the ledger before those pages are claimed makes the associated era reward permanently unclaimable for everyone backing that validator in the affected eras.

## Impact Explanation
This is a permanent, unprivileged loss of legitimately-accrued staking rewards for an arbitrary validator and all of its backing nominators, matching the "permanent user-fund lock" impact category for the Polkadot SDK program. The affected value is the unclaimed `ErasValidatorReward`/`ErasStakersPaged` payout tied to `(era, validator_stash)`, which becomes permanently inaccessible once `Bonded`/`Ledger` are removed.

## Likelihood Explanation
`reap_stash` is explicitly documented and implemented as permissionless (`ensure_signed(origin)?` with no additional origin restriction) [8](#0-7) . The precondition (`ledger.total < ed` or `origin_balance < ed`) is naturally reachable through ordinary slashing or full unbonding, situations that commonly co-occur with delayed/optional reward claims, since users are not required to claim rewards promptly. No privileged, malicious-peer, or off-chain-infra assumptions are needed — only a normal signed transaction from any account observing public chain state, and the caller is even fee-refunded via `Pays::No`.

## Recommendation
Before permitting `reap_stash`/`kill_stash` to proceed, check for any unclaimed reward pages for the stash within `HistoryDepth` (reusing logic similar to `Pallet::pending_rewards` across `ErasStakersOverview`/`ErasStakers`/`ClaimedRewards`) and either block reaping while pending rewards exist, or automatically settle/flush all pending pages (preserving whatever payout metadata is required to pay backing nominators) prior to removing `Ledger`/`Bonded`/`Payee`.

## Proof of Concept
1. Validator `V` is bonded with several nominators backing it; in era `E`, `V` and nominators earn rewards but none call `payout_stakers`/`payout_stakers_by_page` for era `E`.
2. `V` is slashed heavily (or fully unbonds), so `ledger.total` for `V` falls below the existential deposit while era `E` remains within `HistoryDepth` of `CurrentEra`.
3. Any unrelated account calls `Staking::reap_stash(origin, V, _)`; the `reapable` check passes on the `ledger.total < ed` condition alone, and `kill_stash` removes `Bonded::<T>`, `Ledger::<T>`, `Payee::<T>` for `V`.
4. Anyone then calls `Staking::payout_stakers(origin, V, E)`; `do_payout_stakers` fails at `Self::bonded(&validator_stash)` returning `None`, yielding `Error::NotStash`.
5. The era-`E` reward for `V` and all of its nominators is now permanently unclaimable, verifiable as a unit test asserting `do_payout_stakers` returns `Err(Error::NotStash)` after `kill_stash` when `ClaimedRewards::<T>::get(E, &V)` was empty prior to reaping.

### Citations

**File:** substrate/frame/staking/src/pallet/mod.rs (L1805-1831)
```rust
		#[pallet::call_index(20)]
		#[pallet::weight(T::WeightInfo::reap_stash(*num_slashing_spans))]
		pub fn reap_stash(
			origin: OriginFor<T>,
			stash: T::AccountId,
			num_slashing_spans: u32,
		) -> DispatchResultWithPostInfo {
			ensure_signed(origin)?;

			// virtual stakers should not be allowed to be reaped.
			ensure!(!Self::is_virtual_staker(&stash), Error::<T>::VirtualStakerNotAllowed);

			let ed = asset::existential_deposit::<T>();
			let origin_balance = asset::total_balance::<T>(&stash);
			let ledger_total =
				Self::ledger(Stash(stash.clone())).map(|l| l.total).unwrap_or_default();
			let reapable = origin_balance < ed ||
				origin_balance.is_zero() ||
				ledger_total < ed ||
				ledger_total.is_zero();
			ensure!(reapable, Error::<T>::FundedTarget);

			// Remove all staking-related information and lock.
			Self::kill_stash(&stash, num_slashing_spans)?;

			Ok(Pays::No.into())
		}
```

**File:** substrate/frame/staking/src/ledger.rs (L256-274)
```rust
	/// Clears all data related to a staking ledger and its bond in both [`Ledger`] and [`Bonded`]
	/// storage items and updates the stash staking lock.
	pub(crate) fn kill(stash: &T::AccountId) -> DispatchResult {
		let controller = <Bonded<T>>::get(stash).ok_or(Error::<T>::NotStash)?;

		<Ledger<T>>::get(&controller).ok_or(Error::<T>::NotController).map(|ledger| {
			Ledger::<T>::remove(controller);
			<Bonded<T>>::remove(&stash);
			<Payee<T>>::remove(&stash);

			// kill virtual staker if it exists.
			if <VirtualStakers<T>>::take(&ledger.stash).is_none() {
				// if not virtual staker, clear locks.
				asset::kill_stake::<T>(&ledger.stash)?;
			}

			Ok(())
		})?
	}
```

**File:** substrate/frame/staking/src/lib.rs (L1124-1148)
```rust
	/// Returns true if validator has one or more page of era rewards not claimed yet.
	// Also looks at legacy storage that can be cleaned up after #433.
	pub fn pending_rewards(era: EraIndex, validator: &T::AccountId) -> bool {
		let page_count = if let Some(overview) = <ErasStakersOverview<T>>::get(&era, validator) {
			overview.page_count
		} else {
			if <ErasStakers<T>>::contains_key(era, validator) {
				// this means non paged exposure, and we treat them as single paged.
				1
			} else {
				// if no exposure, then no rewards to claim.
				return false;
			}
		};

		// check if era is marked claimed in legacy storage.
		if <Ledger<T>>::get(validator)
			.map(|l| l.legacy_claimed_rewards.contains(&era))
			.unwrap_or_default()
		{
			return false;
		}

		ClaimedRewards::<T>::get(era, validator).len() < page_count as usize
	}
```

**File:** substrate/frame/staking/src/pallet/impls.rs (L235-251)
```rust
	pub(super) fn do_payout_stakers(
		validator_stash: T::AccountId,
		era: EraIndex,
	) -> DispatchResultWithPostInfo {
		let controller = Self::bonded(&validator_stash).ok_or_else(|| {
			Error::<T>::NotStash.with_weight(T::WeightInfo::payout_stakers_alive_staked(0))
		})?;

		let ledger = Self::ledger(StakingAccount::Controller(controller))?;
		let page = EraInfo::<T>::get_next_claimable_page(era, &validator_stash, &ledger)
			.ok_or_else(|| {
				Error::<T>::AlreadyClaimed
					.with_weight(T::WeightInfo::payout_stakers_alive_staked(0))
			})?;

		Self::do_payout_stakers_by_page(validator_stash, era, page)
	}
```

**File:** substrate/frame/staking/src/pallet/impls.rs (L283-290)
```rust
		let account = StakingAccount::Stash(validator_stash.clone());
		let mut ledger = Self::ledger(account.clone()).or_else(|_| {
			if StakingLedger::<T>::is_bonded(account) {
				Err(Error::<T>::NotController.into())
			} else {
				Err(Error::<T>::NotStash.with_weight(T::WeightInfo::payout_stakers_alive_staked(0)))
			}
		})?;
```

**File:** substrate/frame/staking/src/pallet/impls.rs (L780-798)
```rust
	/// Remove all associated data of a stash account from the staking system.
	///
	/// Assumes storage is upgraded before calling.
	///
	/// This is called:
	/// - after a `withdraw_unbonded()` call that frees all of a stash's bonded balance.
	/// - through `reap_stash()` if the balance has fallen to zero (through slashing).
	pub(crate) fn kill_stash(stash: &T::AccountId, num_slashing_spans: u32) -> DispatchResult {
		slashing::clear_stash_metadata::<T>(&stash, num_slashing_spans)?;

		// removes controller from `Bonded` and staking ledger from `Ledger`, as well as reward
		// setting of the stash in `Payee`.
		StakingLedger::<T>::kill(&stash)?;

		Self::do_remove_validator(&stash);
		Self::do_remove_nominator(&stash);

		Ok(())
	}
```
