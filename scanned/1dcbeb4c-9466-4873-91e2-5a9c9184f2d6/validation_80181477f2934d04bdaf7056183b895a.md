## Analysis

I found a real local analog: `pallet-nomination-pools`'s `unbond` extrinsic unconditionally push-transfers pending rewards to the member before allowing the unbond to proceed, and propagates any transfer failure with `?`, aborting the entire unbond. [1](#0-0) 

The reward push happens in `do_reward_payout`, which calls `T::Currency::transfer(..., Preservation::Preserve)?` directly to the member account: [2](#0-1) 

### Title
Forced reward push in `unbond` can permanently lock a member's principal stake if the reward transfer fails - (File: `substrate/frame/nomination-pools/src/lib.rs`)

### Summary
`Pallet::unbond` is documented to "implicitly collect the rewards one last time" before letting a member unbond, and it does so by calling `Self::do_reward_payout(...)?` unconditionally. `do_reward_payout` performs a push-style `T::Currency::transfer` of pending rewards straight to `member_account` with the `?` operator, so any failure of that transfer aborts the whole `unbond` call — including the unbonding of the member's principal stake. This is the same "push over pull" failure mode described in the external report: a legitimate action (liquidation there, unbonding here) is blocked because a mandatory push-payment step to a possibly-non-cooperative recipient account reverts.

### Finding Description
The `unbond` extrinsic doc comment explicitly states the reward payout is not strictly necessary but is done "for UX": [3](#0-2) 

Despite being optional in intent, the code makes it mandatory in practice by using `?`: [4](#0-3) 

`do_reward_payout` itself uses `T::Currency::transfer(&bonded_pool.reward_account(), member_account, pending_rewards, Preservation::Preserve)?`, propagating any transfer error rather than catching/deferring it: [5](#0-4) 

In a Substrate `Balances`-based runtime, a `transfer` to a given account can fail for reasons entirely outside the pool's control and not requiring any privileged actor:
- The account has a `Currency::hold`/`freeze` or `MaxLocks`/`MaxFreezes`/`MaxHolds` at capacity, causing the deposit-side accounting to reject an additional lock/hold entry.
- The account is at `MaxConsumers` and the deposit would create a new consumer reference, if the account doesn't already exist and `Preservation::Preserve` still requires updating consumer/provider counts.
- In a runtime configured with a pallet-assets or other `fungibles` implementation for `T::Currency` (asset-based staking), the recipient could be an account frozen/blocked at the asset level (directly analogous to a USDC-style blacklist).

None of the existing checks in `unbond` guard against this: `bonded_pool.ok_to_unbond_with` only checks pool-state and permission logic, not whether the reward-account transfer will succeed. Because the reward payout happens *before* any of the unbonding state mutations (`bonded_pool.dissolve`, `T::StakeAdapter::unbond`, `sub_pools` update, `member.try_unbond`), a failing transfer means the member can never call `unbond` successfully while they have any non-zero pending reward, permanently freezing their ability to exit the pool and reclaim their principal stake — a fund lock, not merely a UX inconvenience.

### Impact Explanation
This falls under "permanent user-fund or bridge-state lock": a pool member's staked principal becomes permanently unbondable as long as (a) they have nonzero pending rewards and (b) their account cannot accept the reward transfer. Since reward accrual is continuous and automatic (`reward_pool.update_records` recomputes `current_reward_counter` on every call), the member cannot trivially "drain" pending rewards without going through `claim_payout`, which uses the exact same failing transfer path (`do_claim_payout` → `do_reward_payout`). Thus both the reward-claim and unbond paths are blocked simultaneously, and the member's funds become stuck in the pool with no dispatchable in the pallet that lets them exit without a successful push-transfer succeeding first.

### Likelihood Explanation
Likelihood is low-to-moderate: it requires the member's own account to be in a state that rejects transfers (e.g., a runtime where `T::Currency` is asset-based with blacklisting, or an account that has reached `MaxFreezes`/`MaxHolds`/`MaxConsumers`, or is otherwise deliberately configured to reject deposits). This does not require a malicious peer, validator, governance actor, or leaked key — only the ordinary interaction of pallet logic with account-level constraints, matching the report's "push transfer can revert due to recipient state" bug class.

### Recommendation
Do not let a failing reward payout abort unbonding. Options:
1. Make the reward-payout step in `unbond` best-effort: catch the `Result` from `do_reward_payout` and, on error, skip the payout (leaving rewards claimable later) instead of propagating the error with `?`.
2. Switch reward payments to a pull-based/claimable model consistent with the recommendation in the source report: credit the member's pending-reward accounting and let them separately retry `claim_payout`, decoupling it from the state-changing `unbond` path entirely.
3. At minimum, ensure `unbond`'s core state transition (dissolving points, `StakeAdapter::unbond`, sub-pool bookkeeping) is not gated on the reward transfer succeeding.

### Proof of Concept
1. Configure a runtime where `T::Currency` for `pallet-nomination-pools` can reject transfers to a specific account (e.g. an asset-based `fungible` implementation with a freeze/block list, or an account already at `MaxFreezes`/`MaxHolds` capacity for `Preservation::Preserve`).
2. Member `A` joins a pool and rewards accrue (`deposit_rewards` in tests demonstrates this mechanism, see `substrate/frame/nomination-pools/src/tests.rs:2416-2417`).
3. Put `A`'s account into a state where `T::Currency::transfer(reward_account, A, pending_rewards, Preservation::Preserve)` returns `Err`.
4. Call `Pools::unbond(RuntimeOrigin::signed(A), A, unbonding_points)`.
5. Observe the call fails at `Self::do_reward_payout(...)?` (`substrate/frame/nomination-pools/src/lib.rs:2283-2288`), and no unbonding state change occurs — `A`'s principal remains locked in the pool indefinitely, and `claim_payout` fails identically for the same reason.

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L2224-2288)
```rust
		/// Unbond up to `unbonding_points` of the `member_account`'s funds from the pool. It
		/// implicitly collects the rewards one last time, since not doing so would mean some
		/// rewards would be forfeited.
		///
		/// Under certain conditions, this call can be dispatched permissionlessly (i.e. by any
		/// account).
		///
		/// # Conditions for a permissionless dispatch.
		///
		/// * The pool is blocked and the caller is either the root or bouncer. This is refereed to
		///   as a kick.
		/// * The pool is destroying and the member is not the depositor.
		/// * The pool is destroying, the member is the depositor and no other members are in the
		///   pool.
		///
		/// ## Conditions for permissioned dispatch (i.e. the caller is also the
		/// `member_account`):
		///
		/// * The caller is not the depositor.
		/// * The caller is the depositor, the pool is destroying and no other members are in the
		///   pool.
		///
		/// # Note
		///
		/// If there are too many unlocking chunks to unbond with the pool account,
		/// [`Call::pool_withdraw_unbonded`] can be called to try and minimize unlocking chunks.
		/// The [`StakingInterface::unbond`] will implicitly call [`Call::pool_withdraw_unbonded`]
		/// to try to free chunks if necessary (ie. if unbound was called and no unlocking chunks
		/// are available). However, it may not be possible to release the current unlocking chunks,
		/// in which case, the result of this call will likely be the `NoMoreChunks` error from the
		/// staking system.
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
