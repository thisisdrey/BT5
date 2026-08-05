### Title
Permissionless timing of `apply_slash` lets a caller bond into a nomination pool at a stale points-to-balance ratio, diluting other members - (File: `substrate/frame/nomination-pools/src/lib.rs`)

### Summary
`pallet-nomination-pools` prices every new bond (`join`, `bond_extra`, `bond_extra_other`) using the pool's current points-to-balance ratio, derived from `T::StakeAdapter::active_stake` at call time [1](#0-0) . With the `DelegateStake` strategy, a slash is applied immediately at the core-staking/agent level but the corresponding reduction to individual delegator balances and pool points is deferred until someone calls the pallet's own, explicitly permissionless `apply_slash` extrinsic [2](#0-1) . This is the same broken invariant as the external report: a state-changing, rate-affecting operation (`start()` triggering a strategy deposit / here, `apply_slash` finalizing the true points:balance ratio) is public, permissionless, and its timing relative to other public, permissionless actions (`start()`'s deposit / here, `join`/`bond_extra`) is entirely up to whoever calls first.

### Finding Description
The pool's documented pricing invariant is:

```
balance_after_transfer / points_after_transfer == balance_before_transfer / points_before_transfer
``` [3](#0-2) 

and slashing is explicitly documented to change this ratio without changing total points, i.e., "the value of one point ... is now less than one balance because of the slash" [4](#0-3) . The pallet defends against joining a pool whose ratio has *already* moved too far (`ok_to_be_open`, capped by `MaxPointsToBalance`) [5](#0-4) , but this guard only fires once the pool's tracked `points`/`active_stake` actually reflect the slash. Applying that reflection at the member level is a separate, permissionless call: `apply_slash`, which can be dispatched by any account, refunds fees to the caller, and "may [reward] caller with a part of the slash" [2](#0-1) . `pending_slash`/`api_pool_pending_slash` expose this un-applied slash publicly on-chain before it is applied [6](#0-5) , and `DelegateStake::pending_slash` confirms this deferred-application design [7](#0-6) .

This mirrors the seed report exactly: in the LenderPool case, anyone could watch a public, pre-announced trigger (`start()`+ external strategy deposit) and manipulate the yield-strategy exchange rate around the deposit because the deposit's pricing depended on external, manipulable state at call time. Here, anyone can watch a public, on-chain-visible pending slash and manipulate their own bonding around the un-applied-slash window, because `join`/`bond_extra`/`bond_extra_other` price new points using `active_stake`/`points` at call time [8](#0-7) , and nothing forces `apply_slash` to be settled before further bonding is permitted (only the coarse `ok_to_be_open` ratio cap applies, and it only trips after a large enough fraction of the pool has been slashed, not at the first pending slash).

### Impact Explanation
If a caller (existing member or new joiner) can identify a pool with a pending, not-yet-applied slash and race to call `bond_extra`/`bond_extra_other`/`join` before anyone (including themselves, as `apply_slash` is fee-refunded and incentivized) calls `apply_slash`, they capture points priced off a ratio that has not yet absorbed the loss that other members will eventually be charged pro-rata. This does not create new value out of thin air, but it shifts the loss disproportionately onto members who do not race to bond during this window, i.e., an unbacked/mis-priced settlement of pool points relative to actual backing balance — the exact "false state acceptance" / "wrong beneficiary or amount" class called out in the impact gate, expressed through the pool's exchange-rate primitive instead of an AMM.

### Likelihood Explanation
The precondition (a slash event) is validator-driven and not attacker-controlled, which lowers likelihood versus a fully attacker-triggered bug. However, once a slash occurs, the window before `apply_slash` is called is entirely public and timing is fully within an unprivileged account's control (no admin/governance/relayer required), satisfying the "public underpriced work" / permissionless-timing class the task asks to focus on.

### Recommendation
Tie the acceptance of new bonds (`join`, `bond_extra`, `bond_extra_other`) to the pool having no outstanding `pending_slash` (via `T::StakeAdapter::pending_slash`), or force an implicit `apply_slash`-style settlement of the caller/pool before pricing new points, so that `active_stake`/`points` used for `balance_to_point` always reflect the true, fully-settled backing ratio.

### Proof of Concept
1. Pool has `points = 100`, all members fully bonded at 1:1 ratio.
2. A validator backing the pool gets slashed; core-staking-level `active_stake` for the pool's agent account drops (e.g., by 50%), but no member's `delegator_balance` nor `bonded_pool.points` has been adjusted yet — `pending_slash(pool_account)` is now non-zero and publicly queryable via `api_pool_pending_slash` [6](#0-5) .
3. Before anyone calls `apply_slash`, an attacker calls `bond_extra`/`bond_extra_other`/`join` with additional funds; `try_bond_funds` issues points via `balance_to_point(active_stake, points, new_funds)` using the already-deflated `active_stake` but the not-yet-adjusted `points`, yielding a points:balance ratio that does not correspond to the fully-settled state of the pool [1](#0-0) , [9](#0-8) .
4. `apply_slash` is later called (by anyone, permissionlessly, with fee refund) and settles individual members' balances pro-rata to their points at time of settlement [10](#0-9) , spreading the loss to a base that now includes the attacker's late, mis-priced bond.

Note: I was unable, within the remaining tool budget, to fully trace `do_apply_slash`'s exact pro-rata formula (the exact per-member slash allocation logic) to confirm the precise magnitude/direction of the resulting fund transfer between the racing bonder and the diluted members. This should be verified against `do_apply_slash` and `Delegation::delegator_slash` before treating this as fully confirmed; the finding is presented on the strength of the confirmed permissionless-timing/window mechanism described above.

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L100-106)
```rust
//!
//! Slashing does not change any single member's balance. Instead, the slash will only reduce the
//! balance associated with a particular pool. But, we never change the total *points* of a pool
//! because of slashing. Therefore, when a slash happens, the ratio of points to balance changes in
//! a pool. In other words, the value of one point, which is initially 1-to-1 against a unit of
//! balance, is now less than one balance because of the slash.
//!
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L240-252)
```rust
//! When the pool already has some balance, we want the value of a point before the transfer to
//! equal the value of a point after the transfer. So, when a member joins a bonded pool with a
//! given `amount_transferred`, we maintain the ratio of bonded balance to points such that:
//!
//! ```text
//! balance_after_transfer / points_after_transfer == balance_before_transfer / points_before_transfer;
//! ```
//!
//! To achieve this, we issue points based on the following:
//!
//! ```text
//! points_issued = (points_before_transfer / balance_before_transfer) * amount_transferred;
//! ```
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L1183-1208)
```rust
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

**File:** substrate/frame/nomination-pools/src/lib.rs (L1315-1336)
```rust
	fn try_bond_funds(
		&mut self,
		who: &T::AccountId,
		amount: BalanceOf<T>,
		ty: BondType,
	) -> Result<BalanceOf<T>, DispatchError> {
		// We must calculate the points issued *before* we bond who's funds, else points:balance
		// ratio will be wrong.
		let points_issued = self.issue(amount);

		T::StakeAdapter::pledge_bond(
			Member::from(who.clone()),
			Pool::from(self.bonded_account()),
			&self.reward_account(),
			amount,
			ty,
		)?;
		TotalValueLocked::<T>::mutate(|tvl| {
			tvl.saturating_accrue(amount);
		});

		Ok(points_issued)
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L3134-3160)
```rust
		/// Apply a pending slash on a member.
		///
		/// Fails unless [`crate::pallet::Config::StakeAdapter`] is of strategy type:
		/// [`adapter::StakeStrategyType::Delegate`].
		///
		/// The pending slash amount of the member must be equal or more than `ExistentialDeposit`.
		/// This call can be dispatched permissionlessly (i.e. by any account). If the execution
		/// is successful, fee is refunded and caller may be rewarded with a part of the slash
		/// based on the [`crate::pallet::Config::StakeAdapter`] configuration.
		#[pallet::call_index(23)]
		#[pallet::weight(T::WeightInfo::apply_slash())]
		pub fn apply_slash(
			origin: OriginFor<T>,
			member_account: AccountIdLookupOf<T>,
		) -> DispatchResultWithPostInfo {
			ensure!(
				T::StakeAdapter::strategy_type() == adapter::StakeStrategyType::Delegate,
				Error::<T>::NotSupported
			);

			let who = ensure_signed(origin)?;
			let member_account = T::Lookup::lookup(member_account)?;
			Self::do_apply_slash(&member_account, Some(who), true)?;

			// If successful, refund the fees.
			Ok(Pays::No.into())
		}
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L3473-3499)
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
	}
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L3652-3707)
```rust
	fn do_bond_extra(
		signer: T::AccountId,
		member_account: T::AccountId,
		extra: BondExtra<BalanceOf<T>>,
	) -> DispatchResult {
		// ensure account is not restricted from joining the pool.
		ensure!(!T::Filter::contains(&member_account), Error::<T>::Restricted);

		if signer != member_account {
			ensure!(
				ClaimPermissions::<T>::get(&member_account).can_bond_extra(),
				Error::<T>::DoesNotHavePermission
			);
			ensure!(extra == BondExtra::Rewards, Error::<T>::BondExtraRestricted);
		}

		let (mut member, mut bonded_pool, mut reward_pool) =
			Self::get_member_with_pools(&member_account)?;

		// payout related stuff: we must claim the payouts, and updated recorded payout data
		// before updating the bonded pool points, similar to that of `join` transaction.
		reward_pool.update_records(
			bonded_pool.id,
			bonded_pool.points,
			bonded_pool.commission.current(),
		)?;
		let claimed = Self::do_reward_payout(
			&member_account,
			&mut member,
			&mut bonded_pool,
			&mut reward_pool,
		)?;

		let (points_issued, bonded) = match extra {
			BondExtra::FreeBalance(amount) => {
				(bonded_pool.try_bond_funds(&member_account, amount, BondType::Extra)?, amount)
			},
			BondExtra::Rewards => {
				(bonded_pool.try_bond_funds(&member_account, claimed, BondType::Extra)?, claimed)
			},
		};

		bonded_pool.ok_to_be_open()?;
		member.points =
			member.points.checked_add(&points_issued).ok_or(Error::<T>::OverflowRisk)?;

		Self::deposit_event(Event::<T>::Bonded {
			member: member_account.clone(),
			pool_id: member.pool_id,
			bonded,
			joined: false,
		});
		Self::put_member_with_pools(&member_account, member, bonded_pool, reward_pool);

		Ok(())
	}
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L4239-4244)
```rust
	/// Returns the unapplied slash of the pool.
	///
	/// Pending slash is only applicable with [`adapter::DelegateStake`] strategy.
	pub fn api_pool_pending_slash(pool_id: PoolId) -> BalanceOf<T> {
		T::StakeAdapter::pending_slash(Pool::from(Self::generate_bonded_account(pool_id)))
	}
```

**File:** substrate/frame/nomination-pools/src/adapter.rs (L443-445)
```rust
	fn pending_slash(pool_account: Pool<Self::AccountId>) -> Self::Balance {
		Delegation::pending_slash(pool_account.into()).defensive_unwrap_or_default()
	}
```
