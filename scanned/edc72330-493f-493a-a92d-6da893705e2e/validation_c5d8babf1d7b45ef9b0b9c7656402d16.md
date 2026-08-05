[1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6) [8](#0-7)

### Citations

**File:** substrate/frame/asset-rewards/src/lib.rs (L622-649)
```rust
		#[pallet::call_index(4)]
		pub fn set_pool_reward_rate_per_block(
			origin: OriginFor<T>,
			pool_id: PoolId,
			new_reward_rate_per_block: T::Balance,
		) -> DispatchResult {
			let caller = T::CreatePoolOrigin::ensure_origin(origin.clone())
				.or_else(|_| ensure_signed(origin))?;
			<Self as RewardsPool<_>>::set_pool_reward_rate_per_block(
				&caller,
				pool_id,
				new_reward_rate_per_block,
			)
		}

		/// Modify a pool admin.
		///
		/// Only the pool admin may perform this operation.
		#[pallet::call_index(5)]
		pub fn set_pool_admin(
			origin: OriginFor<T>,
			pool_id: PoolId,
			new_admin: T::AccountId,
		) -> DispatchResult {
			let caller = T::CreatePoolOrigin::ensure_origin(origin.clone())
				.or_else(|_| ensure_signed(origin))?;
			<Self as RewardsPool<_>>::set_pool_admin(&caller, pool_id, new_admin)
		}
```

**File:** substrate/frame/asset-rewards/src/lib.rs (L672-688)
```rust
		#[pallet::call_index(7)]
		pub fn deposit_reward_tokens(
			origin: OriginFor<T>,
			pool_id: PoolId,
			amount: T::Balance,
		) -> DispatchResult {
			let caller = ensure_signed(origin)?;
			let pool_info = Pools::<T>::get(pool_id).ok_or(Error::<T>::NonExistentPool)?;
			T::Assets::transfer(
				pool_info.reward_asset_id,
				&caller,
				&pool_info.account,
				amount,
				Preservation::Preserve,
			)?;
			Ok(())
		}
```

**File:** substrate/frame/asset-rewards/src/lib.rs (L697-702)
```rust
		pub fn cleanup_pool(origin: OriginFor<T>, pool_id: PoolId) -> DispatchResult {
			let who = ensure_signed(origin)?;

			let pool_info = Pools::<T>::get(pool_id).ok_or(Error::<T>::NonExistentPool)?;
			ensure!(pool_info.admin == who, BadOrigin);

```

**File:** substrate/frame/nomination-pools/src/lib.rs (L3772-3813)
```rust
	fn do_adjust_pool_deposit(who: T::AccountId, pool: PoolId) -> DispatchResult {
		let bonded_pool = BondedPool::<T>::get(pool).ok_or(Error::<T>::PoolNotFound)?;

		let reward_acc = &bonded_pool.reward_account();
		let pre_frozen_balance =
			T::Currency::balance_frozen(&FreezeReason::PoolMinBalance.into(), reward_acc);
		let min_balance = T::Currency::minimum_balance();

		if pre_frozen_balance == min_balance {
			return Err(Error::<T>::NothingToAdjust.into());
		}

		// Update frozen amount with current ED.
		Self::freeze_pool_deposit(reward_acc)?;

		if pre_frozen_balance > min_balance {
			// Ensure the caller is the depositor or the root.
			ensure!(
				who == bonded_pool.roles.depositor ||
					bonded_pool.roles.root.as_ref().map_or(false, |root| &who == root),
				Error::<T>::DoesNotHavePermission
			);

			// Transfer excess back to depositor.
			let excess = pre_frozen_balance.saturating_sub(min_balance);

			T::Currency::transfer(reward_acc, &who, excess, Preservation::Preserve)?;
			Self::deposit_event(Event::<T>::MinBalanceExcessAdjusted {
				pool_id: pool,
				amount: excess,
			});
		} else {
			// Transfer ED deficit from depositor to the pool
			let deficit = min_balance.saturating_sub(pre_frozen_balance);
			T::Currency::transfer(&who, reward_acc, deficit, Preservation::Expendable)?;
			Self::deposit_event(Event::<T>::MinBalanceDeficitAdjusted {
				pool_id: pool,
				amount: deficit,
			});
		}

		Ok(())
```

**File:** substrate/frame/staking-async/src/slashing.rs (L515-536)
```rust
/// Compute the slash for a validator. Returns the amount slashed and the reward payout.
fn slash_validator<T: Config>(params: SlashParams<T>) -> (BalanceOf<T>, BalanceOf<T>) {
	let own_stake = params.exposure.exposure_metadata.own;
	let prior_slashed = params.prior_slash * own_stake;
	let new_total_slash = params.slash * own_stake;

	let slash_due = new_total_slash.saturating_sub(prior_slashed);
	// Audit Note: Previously, each repeated slash reduced the reward by 50% (e.g., 50% × 50% for
	// two offences). Since repeat offences in the same era are discarded unless the new slash is
	// higher, this reduction logic was unnecessary and removed.
	let reward_due = params.reward_proportion * slash_due;
	log!(
		warn,
		"🦹 slashing validator {:?} of stake: {:?} for {:?} in era {:?}",
		params.stash,
		own_stake,
		slash_due,
		params.offence_era,
	);

	(slash_due, reward_due)
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

**File:** substrate/frame/staking/src/pallet/mod.rs (L2296-2304)
```rust
		#[pallet::call_index(33)]
		#[pallet::weight(T::WeightInfo::manual_slash())]
		pub fn manual_slash(
			origin: OriginFor<T>,
			validator_stash: T::AccountId,
			era: EraIndex,
			slash_fraction: Perbill,
		) -> DispatchResult {
			T::AdminOrigin::ensure_origin(origin)?;
```

**File:** substrate/frame/delegated-staking/src/lib.rs (L704-746)
```rust
	/// Take slash `amount` from agent's `pending_slash`counter and apply it to `delegator` account.
	pub fn do_slash(
		agent: Agent<T::AccountId>,
		delegator: Delegator<T::AccountId>,
		amount: BalanceOf<T>,
		maybe_reporter: Option<T::AccountId>,
	) -> DispatchResult {
		// get inner type
		let agent = agent.get();
		let delegator = delegator.get();

		let agent_ledger = AgentLedgerOuter::<T>::get(&agent)?;
		// ensure there is something to slash
		ensure!(agent_ledger.ledger.pending_slash > Zero::zero(), Error::<T>::NothingToSlash);

		let mut delegation = <Delegators<T>>::get(&delegator).ok_or(Error::<T>::NotDelegator)?;
		ensure!(delegation.agent == agent.clone(), Error::<T>::NotAgent);
		ensure!(delegation.amount >= amount, Error::<T>::NotEnoughFunds);

		// slash delegator
		let (mut credit, missing) =
			T::Currency::slash(&HoldReason::StakingDelegation.into(), &delegator, amount);

		defensive_assert!(missing.is_zero(), "slash should have been fully applied");

		let actual_slash = credit.peek();

		// remove the applied slashed amount from agent.
		agent_ledger.remove_slash(actual_slash).save();
		delegation.amount =
			delegation.amount.checked_sub(&actual_slash).ok_or(ArithmeticError::Overflow)?;
		delegation.update(&delegator);

		if let Some(reporter) = maybe_reporter {
			let reward_payout: BalanceOf<T> = T::SlashRewardFraction::get() * actual_slash;
			let (reporter_reward, rest) = credit.split(reward_payout);

			// credit is the amount that we provide to `T::OnSlash`.
			credit = rest;

			// reward reporter or drop it.
			let _ = T::Currency::resolve(&reporter, reporter_reward);
		}
```
