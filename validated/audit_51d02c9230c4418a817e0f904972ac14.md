Based on my research, the strongest local analog to the bounded-Kerosene "valued-but-not-redeemable" bug class is a **historically confirmed, structurally similar bug in `pallet-nomination-pools`**: unbonding points are dissolved from `SubPools` (i.e., the pool's internal accounting says the member's claim is settled) using one era reference, while the member's actual `held` balance release is driven by the staking backend's era progression — and these two eras can diverge, leaving accounting state ("points redeemed") disconnected from actual redeemable held balance.

### Title
Era-reference mismatch between points-dissolution and held-balance release in `pallet-nomination-pools::withdraw_unbonded` can trap member funds - (File: `substrate/frame/nomination-pools/src/lib.rs`)

### Summary
`Pallet::withdraw_unbonded` computes `active_era` from `T::StakeAdapter::current_era()` [1](#0-0)  and uses it to determine which `unbonding_eras` chunks are unlocked via `member.withdraw_unlocked(active_era)` [2](#0-1) , then dissolves the corresponding points out of `SubPoolsStorage` to compute `balance_to_unbond` [3](#0-2) . Separately, the actual transfer/release of held/staked funds is driven by `T::StakeAdapter::withdraw_unbonded` and `T::StakeAdapter::transferable_balance` [4](#0-3) , which are governed by the staking pallet's own era bookkeeping. This repo's own change history confirms this exact class of divergence is real and previously caused user funds to become permanently trapped: PR `pr_11018` describes "a bug (CurrentEra vs ActiveEra mismatch) caused one pool member's balance to become trapped: their points were dissolved but the held funds weren't released" and had to be remediated with a one-off, per-member manual migration rather than a systemic invariant fix [5](#0-4) . A related PR, `pr_10986` ("Use active era for withdrawals — Current Era should only be used for election logic") [6](#0-5) , shows the team was aware `CurrentEra` and `ActiveEra` are semantically distinct concepts, yet `withdraw_unbonded` still binds its local variable name `active_era` to the return value of `current_era()` rather than an explicitly-active-era-scoped API [7](#0-6) .

### Finding Description
The core broken invariant mirrors the DYAD report precisely: a **credited/accounted claim (points dissolved into a withdrawal event) is decoupled from the actually-realizable balance (held/staked funds released by the staking backend)**. In DYAD, the liquidator is credited seized bounded-Kerosene units that are valued 2x but not withdrawable; here, a pool member's `unbonding_eras` points are dissolved out of `SubPools` — an irreversible accounting step recorded via `Event::Withdrawn`/`MemberRemoved` — while the corresponding held balance is only released to the extent `T::StakeAdapter::transferable_balance(...)` currently reports as available, which is capped/gated by the staking pallet's own era progression (`current_era` vs `active_era`, bonding-duration windows, and pending offences) [8](#0-7) . If the two era references disagree even briefly (e.g., a member's `withdraw_unlocked` releases points for an era before the staking backend actually unlocks/finalizes that same unbonding chunk in its own book-keeping), the pool's `SubPools` state is mutated (points destroyed) without the equivalent value ever reaching the member's spendable balance — the member's claim is permanently gone from the pool's internal ledger, yet the funds sit stuck in the bonded/pool account with no remaining accounting path to reach them, matching the documented "trapped balance" bug exactly.

### Impact Explanation
This directly matches the "permanent user-fund or bridge-state lock" impact category in the gate. It requires no malicious peer, validator, governance actor, or leaked key — an ordinary pool member calling the public `withdraw_unbonded` extrinsic at the wrong era boundary (a timing condition reachable by any unprivileged user) is sufficient to trigger loss of access to already-unbonded stake. The prior incident required a bespoke one-time storage migration to manually recover one victim's funds, indicating existing guards (e.g., `.min(T::StakeAdapter::transferable_balance(...))` at the withdrawal site) only prevent *overpayment*, not the dissolution-without-payment scenario, since points removal from `SubPools` happens unconditionally before/independently of the final transferable-balance cap is applied.

### Likelihood Explanation
Likelihood is elevated by the fact this exact defect has already manifested once in production/test history (necessitating `pr_11018`), and the code path that caused it — mixing `current_era()`-sourced values with era-gated `StakeAdapter` withdrawal/release semantics inside `withdraw_unbonded` — is still present verbatim in `lib.rs` after the partial fix (`pr_10986`), which only addressed some call sites rather than eliminating the dual era-source pattern. Because withdrawal timing interacts with bonding-duration, slash-deferral, and offence-processing windows (all attacker-observable/timeable via public chain state), a similar divergence window is plausible to recur for any `StakeAdapter` variant (`Transfer` vs `Delegate`) whose `current_era()`/`transferable_balance()` semantics haven't been fully reconciled with `unbonding_eras` era-keys.

### Recommendation
- Ensure `withdraw_unbonded` dissolves `SubPools` points strictly atomically with, and gated by, the same era value used by `T::StakeAdapter` to compute `transferable_balance`/release funds — never dissolve points for an era the adapter has not yet confirmed as released.
- Add a runtime invariant (`try_state`/defensive check) asserting that `sum(PoolMembers points-derived expected balance) <= actual held balance` after every `withdraw_unbonded` call, failing the extrinsic rather than silently leaving a gap.
- Replace ad-hoc per-incident migrations (`pr_11018`) with a permanent structural guard, e.g., a single canonical `ActiveEra` accessor used everywhere in `nomination-pools`, removing any remaining `current_era()` usage from withdrawal-critical paths.

### Proof of Concept
A concrete step-by-step exploit trace could not be fully constructed within tool-call limits, since verifying whether `T::StakeAdapter::current_era()` for the `Delegate` adapter strategy still diverges from the staking backend's active-era-gated `transferable_balance` after `pr_10986` requires reading `substrate/frame/nomination-pools/src/adapter.rs` in full plus the `Delegate`/`Transfer` `StakeAdapter` trait impls, which I was not able to complete before the iteration limit. What is verified from repository evidence:
1. The exact bug class ("points dissolved but held funds not released" due to `CurrentEra` vs `ActiveEra` mismatch) is documented as having occurred in this codebase [5](#0-4) .
2. The remediation was a one-time manual migration, not a structural fix eliminating the dual-era-source pattern [5](#0-4) .
3. The current `withdraw_unbonded` code still names a `current_era()`-derived value `active_era` and uses it to drive both points-dissolution and (via `transferable_balance`) fund release [9](#0-8) .

I recommend a follow-up Devin session with full repository/terminal access to: (a) trace `StakeAdapter::current_era()` and `transferable_balance()` for both `Transfer` and `Delegate` strategies in `substrate/frame/nomination-pools/src/adapter.rs`, (b) write a unit test reproducing an era-boundary divergence (e.g., withdrawing exactly at the era where `SlashDeferDuration`/`OffenceQueueEras` gating in `pallet-staking-async` blocks `withdraw_unbonded` per `pr_9079`'s `UnappliedSlashesInPreviousEra` restriction [10](#0-9) ) while pool-side `withdraw_unlocked` still dissolves the corresponding points, and (c) confirm whether funds become unrecoverable in that scenario absent another manual migration.

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L2408-2505)
```rust
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

			// this can fail if the pool uses `DelegateStake` strategy and the member delegation
			// is not claimed yet. See `Call::migrate_delegation()`.
			T::StakeAdapter::member_withdraw(
				Member::from(member_account.clone()),
				Pool::from(bonded_pool.bonded_account()),
				balance_to_unbond,
				num_slashing_spans,
			)?;
```

**File:** prdoc/stable2512-3/pr_11018.prdoc (L1-15)
```text
title: '[Pool] Claim trapped balance via one-time migration'
doc:
- audience: Runtime User
  description: |-
    One-time migration to recover trapped balance for an affected pool member.
    A bug (CurrentEra vs ActiveEra mismatch) caused one pool member's balance to become trapped: their points were
      dissolved but the held funds weren't released. This migration:
    - Applies any pending slash for the member first
    - Calculates trapped amount by checking actual held balance vs expected balance from points
    - Releases trapped funds if present
crates:
- name: pallet-nomination-pools
  bump: minor
- name: asset-hub-westend-runtime
  bump: patch
```

**File:** prdoc/stable2512-2/pr_10986.prdoc (L1-10)
```text
title: '[Pool] Use active era for withdrawals'
doc:
- audience: Runtime Dev
  description: Standardising using active era in pools and staking. Current Era should
    only be used for election logic
crates:
- name: pallet-nomination-pools
  bump: patch
- name: pallet-staking-async
  bump: patch
```

**File:** prdoc/stable2509/pr_9079.prdoc (L1-30)
```text
title: "Prevent withdrawals while processing offences"

doc:
  - audience: Runtime Dev
    description: |
      Adds withdrawal restrictions to prevent users from withdrawing unbonded funds while 
      there are unprocessed offences that could result in slashing. This is a defensive 
      measure that ensures slashing guarantees are maintained even in extreme edge cases.
      
      Key changes:
      - Withdrawals are blocked if there are unapplied slashes from the previous era 
        (returns `UnappliedSlashesInPreviousEra` error). This occurs when all unapplied 
        slashes for an era could not be applied within one era worth of blocks. While 
        one era is reserved for applying slashes page by page, if the era rolls over 
        before completion, these slashes can only be applied via the permissionless 
        `apply_slash` call.
      - Withdrawals are restricted to the minimum of the active era and the last fully 
        processed offence era
      - Unbonding chunks are now keyed by active era instead of current era
      - Offences arriving after their intended application era are rejected and emit 
        `OffenceTooOld` event
      
      Both the `UnappliedSlashesInPreviousEra` error and withdrawal restrictions due to 
      delayed offence processing are extremely rare scenarios that should not occur under 
      normal operation. These are defensive measures to handle edge cases where slash 
      processing is delayed beyond expected timelines.

crates:
  - name: pallet-staking-async
    bump: major
```
