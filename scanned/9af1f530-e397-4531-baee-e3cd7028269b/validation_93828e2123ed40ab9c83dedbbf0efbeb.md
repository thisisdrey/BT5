## Analysis

The reported bug class is: **a mandatory refund/payout inside a public exit-path function uses a `?`-propagated `transfer()` with no fallback claims mechanism, so if the transfer to the fixed beneficiary ever fails, the exit path becomes permanently unusable and funds are stuck.**

The closest verified local analog is in `pallet-nomination-pools`.

### Title
Mandatory reward transfer inside `unbond()` can permanently lock a member's bonded stake if the payout transfer to that member ever fails - (File: `substrate/frame/nomination-pools/src/lib.rs`)

### Summary
`Pallet::unbond` unconditionally calls `Self::do_reward_payout(...)` and propagates its error with `?` before performing any unbonding logic. `do_reward_payout` performs a direct `T::Currency::transfer(&bonded_pool.reward_account(), member_account, pending_rewards, Preservation::Preserve)?` to the member. There is no fallback "claims" ledger analogous to `pallet-treasury`'s retryable `PaymentState::Attempted/Failed` pattern. If this transfer to a specific member ever fails (destination-side restriction/rejection, e.g. via a runtime-configured `Currency`/`fungible` implementation that can reject deposits to certain accounts, or an implementation-specific deposit failure), `unbond()` for that member will fail every single time pending rewards are non-zero, since rewards keep accruing and can never be skipped.

### Finding Description
`unbond` is defined at [1](#0-0) . It always executes:

```
Self::do_reward_payout(&member_account, &mut member, &mut bonded_pool, &mut reward_pool)?;
```

before performing the actual unbond of stake — this is the mandatory-refund-before-exit pattern that mirrors `_closePosition`'s mandatory `refundWithCheck` before releasing liquidity in the source report.

`do_reward_payout` is defined at [2](#0-1) . The critical transfer is:

```
T::Currency::transfer(
    &bonded_pool.reward_account(),
    member_account,
    pending_rewards,
    Preservation::Preserve,
)?;
```

If `pending_rewards` is non-zero and the transfer errors, the `?` propagates the error out of `do_reward_payout`, then out of `unbond`, causing the whole extrinsic to fail — exactly like `refundWithCheck` reverting `_closePosition`. Since rewards continue to accrue for an active pool member every era, and there is no way to skip/forfeit or reroute the reward payout, once a transfer to a member's account is guaranteed to fail (for whatever destination-side reason applies in a given runtime configuration of `T::Currency`), `unbond()` becomes permanently blocked for that member. Because `withdraw_unbonded` requires funds to have gone through the `unbond` → unbonding-era pipeline first, the member's principal stake becomes permanently locked in the pool, unlike `pallet-treasury`, which handles the exact same class of failure via a `PaymentState::Failed`/`check_status`/retry design (`substrate/frame/treasury/src/lib.rs` lines 736-757, 778-814).

### Impact Explanation
A member whose account permanently fails to receive the reward-pool transfer can never call `unbond()` successfully again, since the mandatory `do_reward_payout` step will always fail before any unbonding state change is persisted. This permanently locks their principal (and their share of the bonded pool) inside the pool, with no user-facing recovery path — matching the "LP funds may never be able to be retrieved" impact class from the source report, translated to nomination-pool members' staked capital.

### Likelihood Explanation
This requires only an ordinary account interacting with the public, unprivileged `unbond` extrinsic and pool reward accrual; it does not require a malicious validator, governance action, or leaked keys. The likelihood is bounded by whether/when a `T::Currency` transfer to a specific account can be made to persistently fail in a given runtime's currency configuration for the pools pallet's reward account — the code path itself provides no defensive fallback regardless.

### Recommendation
Apply the same pattern already used by `pallet-treasury`: decouple the reward payout from the state-changing part of `unbond`. Either (a) allow `unbond` to proceed even if the reward transfer fails, recording the failed/forfeited reward in a claimable ledger the member can retry independently, or (b) make the reward-claim step optional/skippable rather than a hard precondition gated with `?`.

### Proof of Concept
1. Member `M` joins a pool and accrues points.
2. Pool reward account earns non-zero pending rewards for `M` (any era passes with rewards deposited to reward account, as in `deposit_rewards` used throughout `substrate/frame/nomination-pools/src/tests.rs`).
3. Construct/require a runtime configuration where `T::Currency::transfer` into `M`'s account deterministically errors (destination-side restriction on the specific `Currency`/`fungible` implementation configured for this pallet).
4. `M` calls `Pallet::unbond(origin, M, unbonding_points)` — [3](#0-2)  triggers `do_reward_payout`, which executes the transfer at [4](#0-3)  and errors.
5. The `?` propagates, `unbond` fails with no state change, and `M`'s principal remains locked in the pool for as long as rewards keep accruing and the transfer keeps failing — with no alternative claim path, unlike `pallet-treasury`'s `payout`/`check_status` retry flow.

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L2257-2296)
```rust
		pub fn unbond(
			origin: OriginFor<T>,
			member_account: AccountIdLookupOf<T>,
			#[pallet::compact] unbonding_points: BalanceOf<T>,
		) -> DispatchResult {
			let who = ensure_signed(origin)?;
			let member_account = T::Lookup::lookup(member_account)?;
			// ensure member is not in an un-migrated state.
			ensure!(
				!Self::api_member_needs_delegate_migration(member_account.clone()),
				Error::<T>::NotMigrated
			);

			let (mut member, mut bonded_pool, mut reward_pool) =
				Self::get_member_with_pools(&member_account)?;

			bonded_pool.ok_to_unbond_with(&who, &member_account, &member, unbonding_points)?;

			// Claim the the payout prior to unbonding. Once the user is unbonding their points no
			// longer exist in the bonded pool and thus they can no longer claim their payouts. It
			// is not strictly necessary to claim the rewards, but we do it here for UX.
			reward_pool.update_records(
				bonded_pool.id,
				bonded_pool.points,
				bonded_pool.commission.current(),
			)?;
			Self::do_reward_payout(
				&member_account,
				&mut member,
				&mut bonded_pool,
				&mut reward_pool,
			)?;

			let active_era = T::StakeAdapter::current_era();
			let unbond_era = T::StakeAdapter::bonding_duration().saturating_add(active_era);

			// Unbond in the actual underlying nominator.
			let unbonding_balance = bonded_pool.dissolve(unbonding_points);
			T::StakeAdapter::unbond(Pool::from(bonded_pool.bonded_account()), unbonding_balance)?;

```

**File:** substrate/frame/nomination-pools/src/lib.rs (L3524-3571)
```rust
	/// If the member has some rewards, transfer a payout from the reward pool to the member.
	// Emits events and potentially modifies pool state if any arithmetic saturates, but does
	// not persist any of the mutable inputs to storage.
	fn do_reward_payout(
		member_account: &T::AccountId,
		member: &mut PoolMember<T>,
		bonded_pool: &mut BondedPool<T>,
		reward_pool: &mut RewardPool<T>,
	) -> Result<BalanceOf<T>, DispatchError> {
		debug_assert_eq!(member.pool_id, bonded_pool.id);
		debug_assert_eq!(&mut PoolMembers::<T>::get(member_account).unwrap(), member);

		// a member who has no skin in the game anymore cannot claim any rewards.
		ensure!(!member.active_points().is_zero(), Error::<T>::FullyUnbonding);

		let (current_reward_counter, _) = reward_pool.current_reward_counter(
			bonded_pool.id,
			bonded_pool.points,
			bonded_pool.commission.current(),
		)?;

		// Determine the pending rewards. In scenarios where commission is 100%, `pending_rewards`
		// will be zero.
		let pending_rewards = member.pending_rewards(current_reward_counter)?;
		if pending_rewards.is_zero() {
			return Ok(pending_rewards);
		}

		// IFF the reward is non-zero alter the member and reward pool info.
		member.last_recorded_reward_counter = current_reward_counter;
		reward_pool.register_claimed_reward(pending_rewards);

		T::Currency::transfer(
			&bonded_pool.reward_account(),
			member_account,
			pending_rewards,
			// defensive: the depositor has put existential deposit into the pool and it stays
			// untouched, reward account shall not die.
			Preservation::Preserve,
		)?;

		Self::deposit_event(Event::<T>::PaidOut {
			member: member_account.clone(),
			pool_id: member.pool_id,
			payout: pending_rewards,
		});
		Ok(pending_rewards)
	}
```
