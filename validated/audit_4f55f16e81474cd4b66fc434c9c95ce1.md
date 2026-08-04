### Title
Permissionless `reap_stash` destroys the staking ledger before pending era rewards are claimed, permanently locking staker (and nominators') funds - (File: `substrate/frame/staking/src/pallet/mod.rs`)

### Summary
`Pallet::reap_stash` is a signed, permissionless extrinsic that anyone can call against any stash whose free/reserved balance or ledger total has fallen below the existential deposit [1](#0-0) . It never checks whether the target stash still has un-claimed `ErasStakersPaged`/`ClaimedRewards` entries for past eras (within `HistoryDepth`) before calling `kill_stash`, which wipes `Bonded`, `Ledger`, and `Payee` [2](#0-1) [3](#0-2) . Once that state is gone, `payout_stakers`/`payout_stakers_by_page` can no longer resolve the stash's controller/ledger and permanently fail, so any unclaimed reward for that validator (and, transitively, for its backing nominators) becomes unclaimable forever. This mirrors the reported Derby `blacklistProtocol()` bug: a state-changing/destructive call proceeds without first claiming pending rewards, freezing funds that were otherwise available.

### Finding Description
`reap_stash` is callable by any signed account (`ensure_signed(origin)?`) and is reapable whenever:
- `origin_balance < ed` (free+reserved of the stash), OR
- `ledger.total < ed` [4](#0-3) 

Neither condition inspects whether the stash still has **pending, unclaimed era rewards**. Pending rewards are tracked completely independently in `ErasStakersPaged`/`ClaimedRewards` for every era within `[current_era - HistoryDepth, current_era]` [5](#0-4) , and `EraInfo::pending_rewards` explicitly checks the ledger's `legacy_claimed_rewards`/`ClaimedRewards` to determine if a payout is still owed [6](#0-5) . A validator's `ledger.total` (currently bonded/active stake) can easily be small (e.g. it was slashed almost to zero, or it fully unbonded and is only waiting on unwithdrawn chunks) while it still has multiple past eras of unclaimed validator+nominator rewards sitting in `ErasStakersPaged`.

Once `reap_stash` succeeds, `StakingLedger::kill` removes `Ledger`, `Bonded`, and `Payee` for that stash [7](#0-6) . Any subsequent `payout_stakers`/`payout_stakers_by_page` call for that validator now fails at the very first step, because it needs to resolve `Self::bonded(&validator_stash)` (removed) to find the controller/ledger and returns `Error::NotStash`/`Error::NotController` [8](#0-7) . The reward money itself was never transferred out prior to reaping — it is simply left unreachable, since the only entrypoint to claim it (`payout_stakers`) requires exactly the ledger state that was just destroyed. Importantly, `payout_stakers` pays not just the validator but also all backing nominators from the shared exposure page, so a single unprivileged reap can freeze reward funds belonging to many third-party nominator accounts, not just the reaped stash.

This is directly analogous to the Derby report: a public/administrative action (`blacklistProtocol`/`reap_stash`) mutates protocol/ledger state destructively without first draining/claiming the rewards that are tied to that state, leaving them permanently stuck.

### Impact Explanation
Reward funds that were legitimately earned by a validator and its nominators for eras within `HistoryDepth` become permanently unclaimable once the validator's stash is reaped. This is a direct, permanent loss of already-accrued staking rewards for potentially many accounts (all nominators backing that validator in the affected eras), triggerable by any unprivileged account. This matches the "permanent user-fund lock" impact category in scope for the program.

### Likelihood Explanation
`reap_stash` is explicitly permissionless ("can be called by anyone") and the reapability condition is easy to satisfy naturally: a validator/nominator whose stake was heavily slashed, or who fully unbonded and is between unbonding periods, will have `ledger.total < ed` while still having outstanding unclaimed reward pages from recent eras (since claiming is a separate, optional action many stakers delay). No malicious peer/validator/governance action is required — only a normal signed call from any account observing on-chain state. This makes the path realistically and cheaply exploitable (it even refunds the caller's tx fee via `Pays::No`).

### Recommendation
Before allowing `reap_stash` (and `kill_stash`) to proceed, check whether the target stash still has any unclaimed reward pages within `HistoryDepth` (e.g. iterate `ErasStakersPaged`/`ClaimedRewards`/`ErasStakersOverview` for that stash, similar to `EraInfo::pending_rewards`) and either:
- refuse to reap while pending rewards exist, or
- auto-claim/flush all pending pages for the stash (and, if it's a validator, ensure the payout information needed to pay nominators is preserved/settled) before wiping `Ledger`/`Bonded`/`Payee`.

### Proof of Concept
1. Validator `V` is bonded and nominated; several nominators back it.
2. In era `E`, `V` earns validator+nominator rewards, but neither `V` nor its nominators call `payout_stakers` for era `E` (this is entirely normal/optional user behavior).
3. `V` gets slashed heavily (or fully unbonds), so `ledger.total` for `V` drops below the existential deposit while era `E` is still within `HistoryDepth` of `current_era`.
4. Any unrelated, unprivileged account calls `Staking::reap_stash(origin, V, _)`. The `reapable` check passes purely on `ledger.total < ed`; there is no check for pending rewards. `kill_stash` removes `Bonded::<T>`, `Ledger::<T>`, `Payee::<T>` for `V`.
5. Anyone now calls `Staking::payout_stakers(origin, V, E)` to claim the still-unclaimed era-`E` reward: it fails with `Error::NotStash`/`Error::NotController` because `Bonded`/`Ledger` no longer exist for `V`.
6. The era-`E` reward funds for `V` and all of its nominators are now permanently unclaimable.

### Citations

**File:** substrate/frame/staking/src/pallet/mod.rs (L605-621)
```rust
	/// History of claimed paged rewards by era and validator.
	///
	/// This is keyed by era and validator stash which maps to the set of page indexes which have
	/// been claimed.
	///
	/// It is removed after [`Config::HistoryDepth`] eras.
	#[pallet::storage]
	#[pallet::unbounded]
	pub type ClaimedRewards<T: Config> = StorageDoubleMap<
		_,
		Twox64Concat,
		EraIndex,
		Twox64Concat,
		T::AccountId,
		Vec<Page>,
		ValueQuery,
	>;
```

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

**File:** substrate/frame/staking/src/pallet/impls.rs (L235-290)
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

	pub(super) fn do_payout_stakers_by_page(
		validator_stash: T::AccountId,
		era: EraIndex,
		page: Page,
	) -> DispatchResultWithPostInfo {
		// Validate input data
		let current_era = CurrentEra::<T>::get().ok_or_else(|| {
			Error::<T>::InvalidEraToReward
				.with_weight(T::WeightInfo::payout_stakers_alive_staked(0))
		})?;

		let history_depth = T::HistoryDepth::get();
		ensure!(
			era <= current_era && era >= current_era.saturating_sub(history_depth),
			Error::<T>::InvalidEraToReward
				.with_weight(T::WeightInfo::payout_stakers_alive_staked(0))
		);

		ensure!(
			page < EraInfo::<T>::get_page_count(era, &validator_stash),
			Error::<T>::InvalidPage.with_weight(T::WeightInfo::payout_stakers_alive_staked(0))
		);

		// Note: if era has no reward to be claimed, era may be future. It's better to not update
		// `ledger.legacy_claimed_rewards` in this case.
		let era_payout = <ErasValidatorReward<T>>::get(&era).ok_or_else(|| {
			Error::<T>::InvalidEraToReward
				.with_weight(T::WeightInfo::payout_stakers_alive_staked(0))
		})?;

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
