Based on my research, I found the actual `do_reward_payout` implementation region (lines ~3540-3570), which shows it ends with a `T::Currency::transfer(...)?` with `Preservation::Preserve` right before depositing the `PaidOut` event and returning `pending_rewards`. This transfer, and the reward-counter arithmetic that precedes it, is a fallible step embedded inside a call whose primary purpose is principal withdrawal. [1](#0-0) [2](#0-1) 

### Title
`unbond()` forces an implicit reward payout before releasing principal, coupling an optional claim path to a mandatory withdrawal - (File: `substrate/frame/nomination-pools/src/lib.rs`)

### Summary
`pallet-nomination-pools::unbond()` unconditionally calls `Self::do_reward_payout(...)` before performing the actual unbonding of the member's principal stake. `do_reward_payout` performs reward-counter arithmetic and a `T::Currency::transfer` from the pool's reward account to the member. If this reward-claim sub-step fails for any reason (arithmetic error, insufficient reward-account balance to preserve existential deposit, `RewardCounter` overflow, etc.), the entire `unbond` extrinsic reverts via `?`, and the member cannot unbond/withdraw their principal — mirroring the ECG bug class where a mandatory, unrelated sub-operation (minting rewards) blocks withdrawal of funds that should always be retrievable.

### Finding Description
`Call::unbond` is documented as implicitly collecting rewards "one last time" for UX reasons, not because it's strictly necessary: [3](#0-2) 

The implementation calls `reward_pool.update_records(...)` and then `Self::do_reward_payout(...)` with the `?` operator, meaning any error from this reward path aborts the whole call before the principal-unbonding logic (`bonded_pool.dissolve`, `T::StakeAdapter::unbond`, `SubPoolsStorage` update) ever executes: [4](#0-3) 

This is structurally identical to the ECG bug: `SurplusGuildMinter.unstake()` calls `getRewards()` → `RateLimitedMinter.mint()` before unstaking, so a failure/pause in the reward-mint path blocks withdrawal of the user's staked principal. Here, `unbond()` calls `do_reward_payout()` (which performs a `Currency::transfer` with `Preservation::Preserve`, so it can fail with `Underflow`/`Expendability` type errors if the reward account balance can't sustain the transfer while staying above ED) before the principal-unbonding logic runs.

### Impact Explanation
If the reward-claim step fails (e.g., due to `RewardCounter` arithmetic saturation for pools with skewed points-to-balance ratios after heavy slashing — a scenario the pallet's own doc comments flag as a known risk area — or the reward account being unable to preserve its existential deposit after transfer), the affected pool member is permanently unable to call `unbond()` to start withdrawing their principal stake, even though the GUARDIAN-style intent of nomination pools is that members can always leave. This is a fund-lock class issue: the same mechanism (mandatory reward payout as a precondition) that is meant to be a UX convenience becomes a hard blocker for the primary withdrawal action.

### Likelihood Explanation
The `RewardCounter` type is explicitly documented as prone to saturation under severe slashing scenarios ("if this happens, the pool basically needs to be dismantled"), and `do_reward_payout`'s underlying transfer requires `Preservation::Preserve` on the reward account. Both are realistic failure conditions reachable without any privileged actor — purely through pool economics (slashing events, reward account depletion) — making this a plausible, unprivileged-triggerable state, not a contrived edge case.

### Recommendation
Decouple the mandatory reward-claim step from the unbonding path: if `do_reward_payout` fails, `unbond()` should either (a) proceed with unbonding the principal while emitting an event/warning that rewards were forfeited for this call, or (b) provide an explicit "force unbond without claiming rewards" variant analogous to an `emergencyWithdraw`, so that a reward-accounting failure never blocks a member from starting to withdraw their staked principal.

### Proof of Concept
1. Set up a nomination pool with a member holding pending rewards.
2. Drive the pool's `RewardCounter` toward saturation, or drain the reward account near its existential deposit through repeated legitimate `claim_payout`/slashing-adjustment operations, until a subsequent internal `T::Currency::transfer(...)` inside `do_reward_payout` (called from `unbond`) would violate `Preservation::Preserve` or the `RewardCounter` arithmetic would overflow.
3. Call `Pools::unbond(origin, member_account, unbonding_points)`.
4. Observe that `unbond()` returns an error from the `do_reward_payout` step (propagated via `?` at line 2283-2288) before ever reaching `bonded_pool.dissolve`/`T::StakeAdapter::unbond`, leaving the member's principal permanently locked in the pool with no way to invoke `unbond` successfully.

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L2224-2226)
```rust
		/// Unbond up to `unbonding_points` of the `member_account`'s funds from the pool. It
		/// implicitly collects the rewards one last time, since not doing so would mean some
		/// rewards would be forfeited.
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L2255-2296)
```rust
		#[pallet::call_index(3)]
		#[pallet::weight(T::WeightInfo::unbond())]
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

**File:** substrate/frame/nomination-pools/src/lib.rs (L3556-3571)
```rust
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
