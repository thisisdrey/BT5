# Finding: Nomination-pools `withdraw_unbonded` permanently burns a member's unlocked balance when the payout is capped by `transferable_balance`

### Title
Withdrawn unbonding points/sub-pool balances are unconditionally erased before the payout is capped to `transferable_balance`, permanently destroying the un-transferred remainder - (File: `substrate/frame/nomination-pools/src/lib.rs`)

### Summary
This is a direct structural analog of the Aragon Tap.sol bug: a claimable amount is computed from accounting state, then capped to what is actually available, but the accounting state that represents the *claim entitlement* is fully cleared to reflect the uncapped amount, not the capped one. The difference between the two is permanently lost rather than preserved for a later claim.

### Finding Description
In `Pallet::withdraw_unbonded` [1](#0-0) , the member's unlocking entries are removed and their associated sub-pool points fully dissolved **before** the transferable/actual amount is known: [2](#0-1) 

Specifically:
1. `member.withdraw_unlocked(active_era)` permanently drains the matured entries out of `member.unbonding_eras` (`substrate/frame/nomination-pools/src/lib.rs:668-686`), and the returned `withdrawn_points` are then fully dissolved out of `sub_pools.with_era` / `sub_pools.no_era` (lines 2469-2485), removing the era pool entirely if its points hit zero.
2. Only *after* this destructive accounting mutation is `balance_to_unbond` capped with `.min(T::StakeAdapter::transferable_balance(...))` (lines 2493-2496).
3. The code comment at lines 2486-2492 explicitly acknowledges the truncation case: "A call to this transaction may cause the pool's stash to get dusted. If this happens before the last member has withdrawn, then all subsequent withdraws will be 0. However the unbond pools do not get updated to reflect this... We gracefully proceed in order to ensure members can leave the pool."

This is exactly the Tap.sol pattern: `tapped` (the full entitlement) is computed, then silently capped to `balance - minimum` (here, `transferable_balance`), and the entitlement bookkeeping (`lastWithdrawals`/`tapped`, here `unbonding_eras`/`sub_pools` points) is reset as if the full amount had been paid. There is no mechanism that preserves "amount owed but not yet paid" — once `withdraw_unlocked` and the sub-pool `dissolve()` calls execute, the member's points and the pool's points are gone forever, regardless of how much of `balance_to_unbond` actually reaches the member via `T::StakeAdapter::member_withdraw` at line 2500.

### Impact Explanation
If `transferable_balance` for the pool's bonded account is lower than the sum of the dissolved sub-pool balances (the scenario the code's own comment describes — dusting of the bonded stash from a prior withdrawal, or any other divergence between sub-pool bookkeeping and actual staking-side transferable balance), the member is paid only the reduced, capped amount, yet their unbonding points/era entries and the corresponding sub-pool points are already irreversibly removed. The unpaid remainder becomes permanently unclaimable — it is neither transferred to the member nor retained in any storage item that tracks an outstanding entitlement. This is a permanent user-fund lock under the "Required Impacts" gate (unbacked loss / permanent fund lock via a public dispatchable, no privileged/malicious actor required — any unprivileged pool member calling `withdraw_unbonded` in a normal multi-member unbonding sequence can trigger it).

### Likelihood Explanation
The trigger condition is not hypothetical/attacker-exclusive: it requires only the natural sequence of multiple pool members holding matured unbonding entries where the pool's bonded account balance/existential-deposit dusting interacts with staking-side transferable balance in the exact way the pallet's own comment anticipates. No malicious peer, validator, collator, relayer, or governance/admin action is needed — an ordinary unprivileged member calling the public extrinsic `withdraw_unbonded` (call index 5) is sufficient to realize the loss once the pool state reaches this condition.

### Recommendation
Do not remove the member's `unbonding_eras` entries or dissolve the sub-pool points for the full `withdrawn_points` amount before the actual transferable/paid amount is known. Instead, either (a) compute `transferable_balance` first and only dissolve the sub-pool/member accounting proportional to what can actually be paid, leaving the remainder's points intact for a future `withdraw_unbonded` call, or (b) if a partial payout is unavoidable, retain an explicit "amount owed" record (analogous to the Tap.sol fix in AragonBlack/fundraising#162) so the member can later reclaim the shortfall once the pool has sufficient transferable balance.

### Proof of Concept
1. Create a pool with multiple members; have each member fully unbond so multiple `sub_pools.with_era`/`no_era` entries accumulate balances tied to member points.
2. Drive the bonded (stash) account balance/dusting such that after one member's `withdraw_unbonded` call the stash is dusted (as described in the code comment at lines 2486-2492), so `T::StakeAdapter::transferable_balance` for the pool account returns less than the sum of remaining un-withdrawn sub-pool balances.
3. Have a remaining member call `withdraw_unbonded`: `withdrawn_points` and the associated `sub_pools` entry are fully dissolved (lines 2469-2485), but `balance_to_unbond` is capped to the (lower or zero) `transferable_balance` (lines 2493-2496), so `T::StakeAdapter::member_withdraw` transfers less than the member's true entitlement.
4. Confirm via storage inspection that `PoolMembers` no longer records any outstanding points/eras for the member and `SubPoolsStorage` no longer holds the dissolved entry, while the member's received balance is strictly less than their original bonded contribution — the shortfall is unrecoverable through any other pallet call.

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L2395-2496)
```rust
		pub fn withdraw_unbonded(
			origin: OriginFor<T>,
			member_account: AccountIdLookupOf<T>,
			num_slashing_spans: u32,
		) -> DispatchResultWithPostInfo {
			let caller = ensure_signed(origin)?;
			let member_account = T::Lookup::lookup(member_account)?;
			// ensure member is not in an un-migrated state.
			ensure!(
				!Self::api_member_needs_delegate_migration(member_account.clone()),
				Error::<T>::NotMigrated
			);

			let mut member =
				PoolMembers::<T>::get(&member_account).ok_or(Error::<T>::PoolMemberNotFound)?;
			let active_era = T::StakeAdapter::current_era();

			let bonded_pool = BondedPool::<T>::get(member.pool_id)
				.defensive_ok_or::<Error<T>>(DefensiveError::PoolNotFound.into())?;
			let mut sub_pools =
				SubPoolsStorage::<T>::get(member.pool_id).ok_or(Error::<T>::SubPoolsNotFound)?;

			let slash_weight =
				// apply slash if any before withdraw.
				match Self::do_apply_slash(&member_account, None, false) {
					Ok(_) => T::WeightInfo::apply_slash(),
					Err(e) => {
						let no_pending_slash: DispatchResult = Err(Error::<T>::NothingToSlash.into());
						// This is an expected error. We add appropriate fees and continue withdrawal.
						if Err(e) == no_pending_slash {
							T::WeightInfo::apply_slash_fail()
						} else {
							// defensive: if we can't apply slash for some reason, we abort.
							return Err(Error::<T>::Defensive(DefensiveError::SlashNotApplied).into());
						}
					}

				};

			bonded_pool.ok_to_withdraw_unbonded_with(&caller, &member_account)?;
			let pool_account = bonded_pool.bonded_account();

			// NOTE: must do this after we have done the `ok_to_withdraw_unbonded_other_with` check.
			let withdrawn_points = member.withdraw_unlocked(active_era);
			ensure!(!withdrawn_points.is_empty(), Error::<T>::CannotWithdrawAny);

			// Before calculating the `balance_to_unbond`, we call withdraw unbonded to ensure the
			// `transferable_balance` is correct.
			let stash_killed = T::StakeAdapter::withdraw_unbonded(
				Pool::from(bonded_pool.bonded_account()),
				num_slashing_spans,
			)?;

			// defensive-only: the depositor puts enough funds into the stash so that it will only
			// be destroyed when they are leaving.
			ensure!(
				!stash_killed || caller == bonded_pool.roles.depositor,
				Error::<T>::Defensive(DefensiveError::BondedStashKilledPrematurely)
			);

			if stash_killed {
				// Maybe an extra consumer left on the pool account, if so, remove it.
				if frame_system::Pallet::<T>::consumers(&pool_account) == 1 {
					frame_system::Pallet::<T>::dec_consumers(&pool_account);
				}

				// Note: This is not pretty, but we have to do this because of a bug where old pool
				// accounts might have had an extra consumer increment. We know at this point no
				// other pallet should depend on pool account so safe to do this.
				// Refer to following issues:
				// - https://github.com/paritytech/polkadot-sdk/issues/4440
				// - https://github.com/paritytech/polkadot-sdk/issues/2037
			}

			let mut sum_unlocked_points: BalanceOf<T> = Zero::zero();
			let balance_to_unbond = withdrawn_points
				.iter()
				.fold(BalanceOf::<T>::zero(), |accumulator, (era, unlocked_points)| {
					sum_unlocked_points = sum_unlocked_points.saturating_add(*unlocked_points);
					if let Some(era_pool) = sub_pools.with_era.get_mut(era) {
						let balance_to_unbond = era_pool.dissolve(*unlocked_points);
						if era_pool.points.is_zero() {
							sub_pools.with_era.remove(era);
						}
						accumulator.saturating_add(balance_to_unbond)
					} else {
						// A pool does not belong to this era, so it must have been merged to the
						// era-less pool.
						accumulator.saturating_add(sub_pools.no_era.dissolve(*unlocked_points))
					}
				})
				// A call to this transaction may cause the pool's stash to get dusted. If this
				// happens before the last member has withdrawn, then all subsequent withdraws will
				// be 0. However the unbond pools do no get updated to reflect this. In the
				// aforementioned scenario, this check ensures we don't try to withdraw funds that
				// don't exist. This check is also defensive in cases where the unbond pool does not
				// update its balance (e.g. a bug in the slashing hook.) We gracefully proceed in
				// order to ensure members can leave the pool and it can be destroyed.
				.min(T::StakeAdapter::transferable_balance(
					Pool::from(bonded_pool.bonded_account()),
					Member::from(member_account.clone()),
				));
```
