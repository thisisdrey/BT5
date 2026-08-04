## Analysis Summary

The EigenPod bug's core invariant break is: **a slashing/exchange-rate factor is computed from a balance value that has already been reduced by one accounting layer, while the corresponding counter-value (points/shares) used in the same ratio has not been synchronized**, producing an incorrect exchange rate that depends purely on transaction ordering relative to when the slash bookkeeping is "collected."

I found a structurally identical pattern in `pallet-nomination-pools` when used with the `DelegateStake` strategy, where slashing is intentionally lazy (`pending_slash`) but the balance used to price new pool points is **not** lazy. [1](#0-0) 

### Title
Mispriced Pool-Point Issuance During Pending-Slash Window — ([File: substrate/frame/nomination-pools/src/lib.rs])

### Summary
`pallet-nomination-pools` (with `StakeAdapter = DelegateStake`) defers the actual reduction of a slashed member's held balance until someone permissionlessly calls `apply_slash` (`Call::apply_slash` → `do_apply_slash`) [2](#0-1) . Slash bookkeeping is only accrued as `pending_slash` on the `AgentLedger` [1](#0-0) , while members' actual held (delegated) balances stay untouched until `apply_slash` runs.

However, the price used to issue new pool points on `join`/`bond_extra` is `bonded_balance = T::StakeAdapter::active_stake(...)`, and for `DelegateStake` this falls back to the default trait implementation that reads straight from the core `Staking` ledger's `active`/`total` fields [3](#0-2) , which `pallet_staking`/`pallet_staking_async` slashing reduces **synchronously** at the moment of slash [4](#0-3) . Meanwhile `pool.points` is never adjusted by slashing (by design) [5](#0-4) .

### Finding Description
`balance_to_point()` prices new deposits as `(current_points / current_balance) * new_funds` [6](#0-5) , and `api_balance_to_points`/join call this with `current_balance = T::StakeAdapter::active_stake(...)` [7](#0-6) .

Sequence:
1. Pool has `active_stake = 1000`, `points = 1000` (1:1 ratio), all bonded via `DelegateStake`.
2. Pool gets slashed 50% on the core `Staking` pallet. This immediately reduces the ledger's `active`/`total` to `500` [4](#0-3) , and separately triggers `OnStakingUpdate::on_slash`, which only bumps `AgentLedger.pending_slash += 500` — no member's held balance changes yet [1](#0-0) .
3. Before anyone calls the permissionless `apply_slash` on the existing (harmed) members, an attacker calls `join`/`bond_extra` with `new_funds = 500`. `balance_to_point` computes `points = (1000 / 500) * 500 = 1000` — i.e. the attacker receives points at the **post-slash discounted rate**, doubling their share-per-balance versus a depositor who joined before the slash.
4. The attacker's own `member_delegation_balance` (actual held funds) is untouched by `pending_slash` (it isn't their agent-wide slash to bear individually until `member_pending_slash`/`do_apply_slash` targets them specifically) — their fresh, unslashed 500 buys 1000 points, exactly the same point-buying-power as the original depositor's un-slashed 1000. This means the attacker now holds 50% of the pool's points (`1000/2000`) for only 500 real value, while the original depositor, who put in 1000 real value and suffered the slash, is diluted to the other 50% of points but still bears the entire un-applied `pending_slash` of 500 once it is later applied against them specifically (per-member slash is computed against `total_balance()` vs actual held balance, see `member_pending_slash`) [8](#0-7) .

The guard that should prevent this — `ok_to_be_open`, which caps `points / bonded_balance` — does not help, because it checks the ratio is below `MaxPointsToBalance`, not whether the *denominator itself* is a value already discounted by an unapplied slash that hasn't yet been debited from any specific member [9](#0-8) .

### Impact Explanation
This breaks the pool's core invariant that "points to balance ratio changes only via slashing pro-rata to all members" (as documented) [10](#0-9) . An attacker can buy pool points at an unfairly discounted rate during the window between a slash event and the (optional, unforced, permissionless) `apply_slash` call, transferring value away from members who were already victims of the slash. This is a fund-conservation violation ("Balances… must conserve value and settle exactly once to the rightful beneficiary and amount") reached through an ordinary, unprivileged extrinsic (`join`/`bond_extra`), requiring no malicious validator, relayer, or governance action.

### Likelihood Explanation
`apply_slash` is permissionless but not automatic or bounded in time — nothing forces it to be called immediately after a slash is recorded, and the window can persist indefinitely for pools that no one bothers to "collect" (docs explicitly describe pending_slash as lazily applied and readable via a public RPC `pool_pending_slash`/`member_pending_slash`) [11](#0-10) . Any account can observe a `Slashed`/`PoolSlashed` event on-chain and then call `join` or `bond_extra` before anyone calls `apply_slash`, so this does not require racing a specific mempool transaction — it is a persisting state window, not a front-run-only scenario.

### Recommendation
When pricing new points for `join`/`bond_extra` under `DelegateStake`, the "current_balance" used in `balance_to_point` should net out any un-applied `pending_slash` reported by `T::StakeAdapter::pending_slash(...)` from the numerator/points side consistently, or equivalently should be computed as `effective_balance` consistent with what backs existing points (i.e., ensure `active_stake` and `pool.points` are always measured against the same slash-adjusted reference), analogous to scaling `prevRestakedBalanceWei` by the deposit-scaling-factor in the EigenPod fix. Concretely, block or reprice `join`/`bond_extra` while `T::StakeAdapter::pending_slash(pool_account) > 0`, or force-settle/distribute pending slash impact against points before allowing new deposits to be priced.

### Proof of Concept
1. Create pool `P` with `DelegateStake` adapter; depositor bonds `1000`, `points = 1000`.
2. Trigger an offence causing a 50% slash on `P`'s bonded account via `pallet_staking::slashing::do_slash` (or via era-processing offence report) — core ledger `active`/`total` becomes `500`; `AgentLedger.pending_slash = 500`; `pool.points` remains `1000`.
3. Before calling `Pools::apply_slash`, attacker calls `Pools::bond_extra`/`Pools::join` with `500` new funds. Observe `balance_to_point(500, 1000, 500) = 1000` new points minted.
4. Compare: attacker now owns `1000/2000 = 50%` of pool points for `500` real value, while original depositor's `1000` real value (minus the still-unapplied `500` slash once later collected specifically from them) is diluted to the other `50%` — demonstrating value transfer inconsistent with pro-rata slashing invariant documented in the pallet.

### Citations

**File:** substrate/frame/delegated-staking/src/impls.rs (L141-155)
```rust
impl<T: Config> OnStakingUpdate<T::AccountId, BalanceOf<T>> for Pallet<T> {
	fn on_slash(
		who: &T::AccountId,
		_slashed_active: BalanceOf<T>,
		_slashed_unlocking: &alloc::collections::btree_map::BTreeMap<EraIndex, BalanceOf<T>>,
		slashed_total: BalanceOf<T>,
	) {
		<Agents<T>>::mutate(who, |maybe_register| match maybe_register {
			// if existing agent, register the slashed amount as pending slash.
			Some(register) => register.pending_slash.saturating_accrue(slashed_total),
			None => {
				// nothing to do
			},
		});
	}
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L90-105)
```rust
//! ### Slashes
//!
//! Slashes are distributed evenly across the bonded pool and the unbonding pools from slash era+1
//! through the slash apply era. Thus, any member who either
//!
//! 1. unbonded, or
//! 2. was actively bonded
//
//! in the aforementioned range of eras will be affected by the slash. A member is slashed pro-rata
//! based on its stake relative to the total slash amount.
//!
//! Slashing does not change any single member's balance. Instead, the slash will only reduce the
//! balance associated with a particular pool. But, we never change the total *points* of a pool
//! because of slashing. Therefore, when a slash happens, the ratio of points to balance changes in
//! a pool. In other words, the value of one point, which is initially 1-to-1 against a unit of
//! balance, is now less than one balance because of the slash.
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L1181-1208)
```rust
	/// Whether or not the pool is ok to be in `PoolSate::Open`. If this returns an `Err`, then the
	/// pool is unrecoverable and should be in the destroying state.
	fn ok_to_be_open(&self) -> Result<(), DispatchError> {
		ensure!(!self.is_destroying(), Error::<T>::CanNotChangeState);

		let bonded_balance = T::StakeAdapter::active_stake(Pool::from(self.bonded_account()));
		ensure!(!bonded_balance.is_zero(), Error::<T>::OverflowRisk);

		let points_to_balance_ratio_floor = self
			.points
			// We checked for zero above
			.div(bonded_balance);

		let max_points_to_balance = T::MaxPointsToBalance::get();

		// Pool points can inflate relative to balance, but only if the pool is slashed.
		// If we cap the ratio of points:balance so one cannot join a pool that has been slashed
		// by `max_points_to_balance`%, if not zero.
		ensure!(
			points_to_balance_ratio_floor < max_points_to_balance.into(),
			Error::<T>::OverflowRisk
		);

		// then we can be decently confident the bonding pool points will not overflow
		// `BalanceOf<T>`. Note that these are just heuristics.

		Ok(())
	}
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L3473-3498)
```rust
	/// Calculate the equivalent point of `new_funds` in a pool with `current_balance` and
	/// `current_points`.
	fn balance_to_point(
		current_balance: BalanceOf<T>,
		current_points: BalanceOf<T>,
		new_funds: BalanceOf<T>,
	) -> BalanceOf<T> {
		let u256 = T::BalanceToU256::convert;
		let balance = T::U256ToBalance::convert;
		match (current_balance.is_zero(), current_points.is_zero()) {
			(_, true) => new_funds.saturating_mul(POINTS_TO_BALANCE_INIT_RATIO.into()),
			(true, false) => {
				// The pool was totally slashed.
				// This is the equivalent of `(current_points / 1) * new_funds`.
				new_funds.saturating_mul(current_points)
			},
			(false, false) => {
				// Equivalent to (current_points / current_balance) * new_funds
				balance(
					u256(current_points)
						.saturating_mul(u256(new_funds))
						// We check for zero above
						.div(u256(current_balance)),
				)
			},
		}
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L3816-3841)
```rust
	/// Slash member against the pending slash for the pool.
	fn do_apply_slash(
		member_account: &T::AccountId,
		reporter: Option<T::AccountId>,
		enforce_min_slash: bool,
	) -> DispatchResult {
		let member = PoolMembers::<T>::get(member_account).ok_or(Error::<T>::PoolMemberNotFound)?;

		let pending_slash =
			Self::member_pending_slash(Member::from(member_account.clone()), member.clone())?;

		// ensure there is something to slash.
		ensure!(!pending_slash.is_zero(), Error::<T>::NothingToSlash);

		if enforce_min_slash {
			// ensure slashed amount is at least the minimum balance.
			ensure!(pending_slash >= T::Currency::minimum_balance(), Error::<T>::SlashTooLow);
		}

		T::StakeAdapter::member_slash(
			Member::from(member_account.clone()),
			Pool::from(Pallet::<T>::generate_bonded_account(member.pool_id)),
			pending_slash,
			reporter,
		)
	}
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L3843-3873)
```rust
	/// Pending slash for a member.
	///
	/// Takes the pool_member object corresponding to the `member_account`.
	fn member_pending_slash(
		member_account: Member<T::AccountId>,
		pool_member: PoolMember<T>,
	) -> Result<BalanceOf<T>, DispatchError> {
		// only executed in tests: ensure the member account is correct.
		debug_assert!(
			PoolMembers::<T>::get(member_account.clone().get()).expect("member must exist") ==
				pool_member
		);

		let pool_account = Pallet::<T>::generate_bonded_account(pool_member.pool_id);
		// if the pool doesn't have any pending slash, it implies the member also does not have any
		// pending slash.
		if T::StakeAdapter::pending_slash(Pool::from(pool_account.clone())).is_zero() {
			return Ok(Zero::zero());
		}

		// this is their actual held balance that may or may not have been slashed.
		let actual_balance = T::StakeAdapter::member_delegation_balance(member_account)
			// no delegation implies the member delegation is not migrated yet to `DelegateStake`.
			.ok_or(Error::<T>::NotMigrated)?;

		// this is their balance in the pool
		let expected_balance = pool_member.total_balance();

		// return the amount to be slashed.
		Ok(actual_balance.saturating_sub(expected_balance))
	}
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L4226-4237)
```rust
	/// Returns the equivalent `new_funds` balance to point conversion for a specified pool.
	///
	/// If the pool ID does not exist, returns 0 ratio balance to points. Used by runtime API.
	pub fn api_balance_to_points(pool_id: PoolId, new_funds: BalanceOf<T>) -> BalanceOf<T> {
		if let Some(pool) = BondedPool::<T>::get(pool_id) {
			let bonded_balance =
				T::StakeAdapter::active_stake(Pool::from(Self::generate_bonded_account(pool_id)));
			Pallet::<T>::balance_to_point(bonded_balance, pool.points, new_funds)
		} else {
			Zero::zero()
		}
	}
```

**File:** substrate/frame/nomination-pools/src/adapter.rs (L128-131)
```rust
	/// See [`StakingInterface::active_stake`].
	fn active_stake(pool_account: Pool<Self::AccountId>) -> Self::Balance {
		Self::CoreStaking::active_stake(&pool_account.0).unwrap_or_default()
	}
```

**File:** substrate/frame/staking/src/lib.rs (L671-683)
```rust
	pub fn slash(
		&mut self,
		slash_amount: BalanceOf<T>,
		minimum_balance: BalanceOf<T>,
		slash_era: EraIndex,
	) -> BalanceOf<T> {
		if slash_amount.is_zero() {
			return Zero::zero();
		}

		use sp_runtime::PerThing as _;
		let mut remaining_slash = slash_amount;
		let pre_slash_total = self.total;
```

**File:** substrate/frame/nomination-pools/runtime-api/src/lib.rs (L42-49)
```rust
		/// Returns the pending slash for a given pool.
		fn pool_pending_slash(pool_id: PoolId) -> Balance;

		/// Returns the pending slash for a given pool member.
		///
		/// If pending slash of the member exceeds `ExistentialDeposit`, it can be reported on
		/// chain.
		fn member_pending_slash(member: AccountId) -> Balance;
```
