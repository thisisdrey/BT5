Based on my investigation, I have enough evidence to validate this claim as legitimate.

Audit Report

## Title
`reap_stash`/`force_unstake` kill a nomination pool's bonded stash via `kill_stash` without invoking `on_withdraw`, permanently desyncing `TotalValueLocked` - (File: `substrate/frame/staking/src/pallet/mod.rs`, `substrate/frame/staking/src/pallet/impls.rs`)

## Summary
`pallet-nomination-pools` relies exclusively on `OnStakingUpdate::on_withdraw` and `on_slash` to keep the aggregate `TotalValueLocked` in sync with the actual amount locked in bonded pool stashes. `kill_stash`, used by both `force_unstake` and the permissionless `reap_stash`, removes the entire remaining `StakingLedger` (releasing whatever `ledger.total` is left, including unmatured unlocking chunks) but never calls `T::EventListeners::on_withdraw`, so any residual staked/unbonding balance freed this way is never subtracted from `TotalValueLocked`.

## Finding Description
`kill_stash` fully removes a stash's `StakingLedger`, `Bonded` entry, and `Payee`, releasing the entire remaining `ledger.total` lock [1](#0-0) . This is called directly by `force_unstake` (root-gated) [2](#0-1)  and by the permissionless `reap_stash`, which only requires `ensure_signed` and a reapability check (`origin_balance < ed` or `ledger_total < ed`, either of which can be true while `ledger.total` is still nonzero, including cases where unlocking chunks have not yet matured past the bonding duration) [3](#0-2) .

By contrast, `do_withdraw_unbonded` — the path PR#3052 fixed — notifies listeners only for the amount consolidated via `consolidate_unlocked` (`old_total - new_total`), and separately calls `kill_stash` when the *remaining* active/unlocking balance is dust; that remaining dust amount removed by `kill_stash` is not part of the reported `value` in that function either, but in practice it is typically zero by the time `kill_stash` fires there because `consolidate_unlocked` runs against the current era [4](#0-3) . `reap_stash`, however, has no such consolidation step at all before calling `kill_stash` — it can be triggered as soon as `ledger.total` drops below the existential deposit, even while unlocking chunks are still pending (not yet past `bonding_duration`), releasing that locked amount immediately without ever invoking `on_withdraw`.

`pallet-nomination-pools::on_withdraw` is the only mechanism that decrements `TotalValueLocked` for withdrawals from a bonded pool account [5](#0-4) ; `on_slash` independently decrements it for slashes [6](#0-5) . Neither hook is invoked from `kill_stash`, so any `ledger.total` released through `force_unstake`/`reap_stash` that was not already zeroed out by a prior slash or a prior `do_withdraw_unbonded` consolidation remains permanently counted in `TotalValueLocked` after the stash is destroyed.

## Impact Explanation
`TotalValueLocked` is a chain-wide accounting aggregate consumed by nomination-pools logic and external monitoring/governance as ground truth for "how much is actually staked across all pools." A silent, permanent inflation of this value via an unprivileged, repeatable extrinsic is an accounting/state-integrity bug consistent with "runtime bugs that compromise intended behavior" — it does not, however, produce theft, double-payout, unauthorized origin escalation, or fund lock; its effect is limited to a stale/inflated global counter used for reporting/gating rather than direct custody of funds.

## Likelihood Explanation
`reap_stash` is fully permissionless (`ensure_signed`, no privileged filter) and can target any bonded pool stash whose `ledger.total` or free balance has fallen below the existential deposit — a state reachable via ordinary heavy unbonding/slashing of small or dissolving pools [3](#0-2) . No governance, validator, or relayer privilege is required, and the scenario is repeatable across pools that reach dust state.

## Recommendation
In `kill_stash`, before removing the `StakingLedger`, emit `Event::Withdrawn` and call `T::EventListeners::on_withdraw(stash, ledger.total)` for the full remaining `ledger.total`, mirroring what `do_withdraw_unbonded` does for its consolidated portion, so `force_unstake` and `reap_stash` notify listeners identically and `TotalValueLocked` stays in sync.

## Proof of Concept
1. Create a nomination pool bonding stash `P` with a small amount near the existential deposit.
2. Fully unbond `P`'s active stake so `ledger.active` becomes 0, leaving only unlocking chunks whose maturity era has not yet passed, such that `ledger.total < ExistentialDeposit`.
3. Any unprivileged account calls `Staking::reap_stash(origin, P, num_slashing_spans)`; `ledger_total < ed` makes `reapable == true`, and `kill_stash(&P, ..)` executes, releasing the full remaining lock without notifying `on_withdraw`.
4. Observe `pallet_nomination_pools::TotalValueLocked::<T>::get()` remains unchanged despite `P`'s ledger (and its staked funds) being fully removed, diverging permanently from the true amount now staked — replicable via a unit test comparing `TotalValueLocked` before/after the `reap_stash` call in a scenario with pending, unmatured unlocking chunks.

### Citations

**File:** substrate/frame/staking/src/pallet/impls.rs (L193-230)
```rust
	pub(super) fn do_withdraw_unbonded(
		controller: &T::AccountId,
		num_slashing_spans: u32,
	) -> Result<Weight, DispatchError> {
		let mut ledger = Self::ledger(Controller(controller.clone()))?;
		let (stash, old_total) = (ledger.stash.clone(), ledger.total);
		if let Some(current_era) = CurrentEra::<T>::get() {
			ledger = ledger.consolidate_unlocked(current_era)
		}
		let new_total = ledger.total;

		let ed = asset::existential_deposit::<T>();
		let used_weight =
			if ledger.unlocking.is_empty() && (ledger.active < ed || ledger.active.is_zero()) {
				// This account must have called `unbond()` with some value that caused the active
				// portion to fall below existential deposit + will have no more unlocking chunks
				// left. We can now safely remove all staking-related information.
				Self::kill_stash(&ledger.stash, num_slashing_spans)?;

				T::WeightInfo::withdraw_unbonded_kill(num_slashing_spans)
			} else {
				// This was the consequence of a partial unbond. just update the ledger and move on.
				ledger.update()?;

				// This is only an update, so we use less overall weight.
				T::WeightInfo::withdraw_unbonded_update(num_slashing_spans)
			};

		// `old_total` should never be less than the new total because
		// `consolidate_unlocked` strictly subtracts balance.
		if new_total < old_total {
			// Already checked that this won't overflow by entry condition.
			let value = old_total.defensive_saturating_sub(new_total);
			Self::deposit_event(Event::<T>::Withdrawn { stash, amount: value });

			// notify listeners.
			T::EventListeners::on_withdraw(controller, value);
		}
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

**File:** substrate/frame/staking/src/pallet/mod.rs (L1653-1666)
```rust
		#[pallet::call_index(15)]
		#[pallet::weight(T::WeightInfo::force_unstake(*num_slashing_spans))]
		pub fn force_unstake(
			origin: OriginFor<T>,
			stash: T::AccountId,
			num_slashing_spans: u32,
		) -> DispatchResult {
			ensure_root(origin)?;

			// Remove all staking-related information and lock.
			Self::kill_stash(&stash, num_slashing_spans)?;

			Ok(())
		}
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

**File:** substrate/frame/nomination-pools/src/lib.rs (L4341-4373)
```rust
	fn on_slash(
		pool_account: &T::AccountId,
		// Bonded balance is always read directly from staking, therefore we don't need to update
		// anything here.
		slashed_bonded: BalanceOf<T>,
		slashed_unlocking: &BTreeMap<EraIndex, BalanceOf<T>>,
		total_slashed: BalanceOf<T>,
	) {
		let Some(pool_id) = ReversePoolIdLookup::<T>::get(pool_account) else { return };
		// As the slashed account belongs to a `BondedPool` the `TotalValueLocked` decreases and
		// an event is emitted.
		TotalValueLocked::<T>::mutate(|tvl| {
			tvl.defensive_saturating_reduce(total_slashed);
		});

		if let Some(mut sub_pools) = SubPoolsStorage::<T>::get(pool_id) {
			// set the reduced balance for each of the `SubPools`
			slashed_unlocking.iter().for_each(|(era, slashed_balance)| {
				if let Some(pool) = sub_pools.with_era.get_mut(era).defensive() {
					pool.balance = *slashed_balance;
					Self::deposit_event(Event::<T>::UnbondingPoolSlashed {
						era: *era,
						pool_id,
						balance: *slashed_balance,
					});
				}
			});
			SubPoolsStorage::<T>::insert(pool_id, sub_pools);
		} else if !slashed_unlocking.is_empty() {
			defensive!("Expected SubPools were not found");
		}
		Self::deposit_event(Event::<T>::PoolSlashed { pool_id, balance: slashed_bonded });
	}
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L4375-4383)
```rust
	/// Reduces the overall `TotalValueLocked` if a withdrawal happened for a pool involved in the
	/// staking withdraw.
	fn on_withdraw(pool_account: &T::AccountId, amount: BalanceOf<T>) {
		if ReversePoolIdLookup::<T>::get(pool_account).is_some() {
			TotalValueLocked::<T>::mutate(|tvl| {
				tvl.saturating_reduce(amount);
			});
		}
	}
```
