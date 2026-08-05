### Title
`withdraw_unbonded` burns full unbonding points from a pool member while capping the actual balance released to `transferable_balance`, causing silent permanent loss of pooled funds - ([File: substrate/frame/nomination-pools/src/lib.rs])

### Summary
`Pallet::withdraw_unbonded` (permissionless, self-callable by any pool member) unconditionally consumes/burns all of a member's matured unbonding "points" via `member.withdraw_unlocked(active_era)`, but then computes the actual balance to be released to the member (`balance_to_unbond`) as the points-derived balance **capped** with `.min(T::StakeAdapter::transferable_balance(...))`. There is no corresponding reduction of the points that were burned. If the pool's actual transferable balance is smaller than what the unlocked points are nominally worth (e.g. due to a lagging/incomplete slash application, dust removal of the bonded stash, or any other divergence between recorded points and the pool's real balance), the member's points are destroyed in full while only the smaller, capped amount is ever paid out — an unrecoverable, one-way loss of value, mirroring the Frax `liquidateClean` pattern where the "debt" side (points/shares burned) is not adjusted down to match the "collateral" side (available balance).

### Finding Description
In `withdraw_unbonded`: [1](#0-0) 
the member's unlocking chunks matured up to `active_era` are unconditionally drained via `member.withdraw_unlocked(active_era)`, producing `withdrawn_points` — this removes the points from the member's ledger regardless of what happens next.

Immediately after, the balance to actually transfer to the member is computed as: [2](#0-1) 
the accumulated `balance_to_unbond` (derived from dissolving the withdrawn points against `sub_pools`) is capped with `.min(T::StakeAdapter::transferable_balance(...))`. The code's own comment acknowledges the divergence scenario explicitly: "A call to this transaction may cause the pool's stash to get dusted... the unbond pools do no get updated to reflect this... This check is also defensive in cases where the unbond pool does not update its balance (e.g. a bug in the slashing hook.)".

The critical asymmetry: the **points burn is final and irreversible** (done at line 2438, before the cap is even computed), while the **payout is capped** at line 2493-2496. There is no mechanism that reduces the amount of points consumed to match the capped balance, nor any path that restores the member's claim to the shortfall. This is structurally identical to the Frax `liquidateClean` bug: the "repayment" side (points destroyed, analogous to shares repaid by the liquidator) is not bounded by the "collateral" side (`transferable_balance`, analogous to available collateral), so whenever the two diverge the difference is permanently forfeited by the party on the burning side (here, the pool member) rather than being reduced/refunded.

This can be reached by any pool member calling the plain permissionless extrinsic `withdraw_unbonded` — no admin, governance, relayer, or malicious peer is required. The divergence between points-derived balance and actual transferable balance is a state that can arise from ordinary protocol operation (partial/late slash application, `min_join_bond`/dust handling around stash killing, or any accounting drift between `SubPoolsStorage` and the real staking ledger balance) — the pallet authors already anticipated and coded around the existence of this divergence, but chose to silently absorb it against the member rather than reconcile the points.

### Impact Explanation
This falls under "permanent user-fund lock" / value non-conservation for a public entrypoint: an unprivileged pool member's already-unbonded stake (points that should map 1:1 to withdrawable balance) can be permanently destroyed for an amount exceeding what is actually paid out, with no way to recover the difference (the points are gone from `PoolMembers` storage, and the corresponding `SubPools`/ledger entries are also dissolved). Because pool balances are shared collectively, any drift between the aggregate pool balance and the sum of members' point claims directly reduces individual members' recoverable funds without their knowledge, breaking the "settle exactly once to the rightful beneficiary and amount" invariant.

### Likelihood Explanation
The divergence precondition is not attacker-injectable at will (it requires slashing/dusting edge cases mentioned in-code), so likelihood is lower than a fully attacker-controlled primitive; however, the pallet code explicitly documents multiple realistic triggers (defensive-only slashing hook bugs, stash dusting before last withdrawal, era-pool balance not being updated) as already-occurring conditions, meaning the vulnerable code path is exercised under normal operational failure modes, not just theoretical inputs. No governance, validator, relayer, or privileged actor involvement is needed to trigger or be harmed by it — only an ordinary member calling `withdraw_unbonded` under these (documented-as-possible) conditions.

### Recommendation
When `balance_to_unbond` is capped by `transferable_balance`, proportionally reduce the points actually burned/removed from the member (and re-derive `sum_unlocked_points`/event data) so that the member retains a claim on the un-paid remainder instead of forfeiting it, or track a `UnbondedDeficit` for the member/pool that can be claimed later once the pool's balance catches up. At minimum, do not treat this as merely "defensive" logging — reconcile or refund the shortfall rather than silently dropping it.

### Proof of Concept
1. Set up a nomination pool with a member who has fully unbonded points scheduled for release in `era`.
2. Induce a divergence between the pool's real transferable balance and the points-derived `balance_to_unbond` — e.g., trigger a scenario matching the code's own defensive comment: a partial/late slash application combined with pool dusting before the last member withdraws (see the existing test acknowledging this exact scenario), or any state where `SubPoolsStorage` balances have not been fully synced to actual stake reductions.
3. Call `Pools::withdraw_unbonded(origin, member_account, num_slashing_spans)` as the member.
4. Observe: `withdrawn_points` (full nominal amount) is removed from `member` at line 2438, but `balance_to_unbond` transferred is `.min(transferable_balance)` (line 2493-2496) — strictly less than the nominal value of the burned points.
5. The member's `PoolMembers` entry no longer reflects any claim to the shortfall; the funds are unrecoverable by the member, yet no error, event, or accounting entry marks the loss. [3](#0-2) [4](#0-3)

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L2437-2439)
```rust
			// NOTE: must do this after we have done the `ok_to_withdraw_unbonded_other_with` check.
			let withdrawn_points = member.withdraw_unlocked(active_era);
			ensure!(!withdrawn_points.is_empty(), Error::<T>::CannotWithdrawAny);
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L2469-2496)
```rust
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
