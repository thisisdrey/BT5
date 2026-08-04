## Title
`unbond()` in `pallet-nomination-pools` computes withdrawal-era state from `CurrentEra` instead of `ActiveEra`, causing points-to-balance mismatch and trapped funds - (File: `substrate/frame/nomination-pools/src/lib.rs`)

## Summary
The external report's core defect is a function that is supposed to key its range/index calculation off a caller-supplied context value (`id`), but instead reads a different global "current" state variable (`currentChest`), producing an internally inconsistent record and locking user funds. The exact same defect class — using the pallet's `CurrentEra` (an election-bookkeeping value) where the settled `ActiveEra` should be used — exists in `pallet-nomination-pools::unbond()`, and is independently confirmed by this repository's own change history as a real, previously-exploited bug that trapped a pool member's funds.

## Finding Description
In `Pallet::<T>::unbond` [1](#0-0) , the era used to (a) merge stale unbonding sub-pools and (b) key the new unbonding chunk is computed as:
```rust
let active_era = T::StakeAdapter::current_era();
let unbond_era = T::StakeAdapter::bonding_duration().saturating_add(active_era);
```
The local variable is named `active_era`, but its value is sourced from `T::StakeAdapter::current_era()` rather than an active/settled era. `current_era()` is documented in this same codebase as an election-only counter that can be ahead of the era that is actually applied to bonded funds — this is precisely the distinction the repository's own PR docs describe as the root cause of a real trapped-funds bug: [2](#0-1)  and the follow-up migration written to compensate an already-affected user: [3](#0-2) .

The consequence mirrors the external report's mechanism exactly: `unbond_era` (analogous to the improperly-referenced `currentChest`) is used to key `sub_pools.with_era` and to call `member.try_unbond(unbonding_points, points_unbonded, unbond_era)` [4](#0-3) . If `CurrentEra` and the era that is actually finalized for bonding/slashing diverge, points are dissolved from the bonded pool immediately, but the corresponding held balance is keyed to an era value that does not correspond to when the funds are actually released by the underlying staking pallet — exactly the "entries dissolved, funds not released" failure the repository's own migration doc describes: [5](#0-4) .

## Impact Explanation
This falls squarely within the required impact class "permanent user-fund or bridge-state lock" and "duplicate settlement or payout" analog for staking/asset accounting: a pool member's bonded points are burned from the bonded pool (irreversible), while the balance intended to back those points is keyed/tracked against the wrong era boundary, leading to funds that cannot be correctly claimed via `withdraw_unbonded`. This is not a hypothetical — the repository's own PR history shows this exact bug already trapped a real user's balance and required a bespoke, one-time on-chain migration to manually recover it, confirming the invariant break is genuine and has real financial consequence.

## Likelihood Explanation
This is triggered through the fully public, unprivileged `unbond` extrinsic that any pool member can call with no special preconditions — no malicious peer, validator, governance actor, or privileged role is required. The only condition needed is that `CurrentEra` and the era actually settled for bonding/unbonding diverge at the time of the call, which is a normal, recurring runtime condition (election lookahead vs. era application timing), not an edge case requiring adversarial setup.

## Recommendation
Ensure `unbond()` (and any other nomination-pools call path performing era-keyed bookkeeping for unbonding/sub-pool management) sources the era from the settled/active era abstraction rather than `T::StakeAdapter::current_era()`, consistent with the stated intent in `pr_10986` that "Current Era should only be used for election logic." Audit all remaining `current_era()` call sites in `pallet-nomination-pools` and `pallet-staking-async` for the same mismatch, and add a `try-state` invariant asserting that `sub_pools.with_era` keys are always drawn from the active-era domain, not the current/election-era domain.

## Proof of Concept
Conceptual reproduction, consistent with the repository's own confirmed incident:
1. A pool member calls the public `unbond` extrinsic [6](#0-5)  while `CurrentEra` (election bookkeeping) is ahead of the era actually applied to bonded stake.
2. `unbond()` computes `active_era = T::StakeAdapter::current_era()` and derives `unbond_era` from it [1](#0-0) , then dissolves the member's points from the bonded pool and records the unbonding chunk keyed by this `unbond_era` [7](#0-6) .
3. Because the underlying staking pallet settles/unlocks funds based on the actual active era rather than `CurrentEra`, the member's held balance is never correctly released when they later call `withdraw_unbonded`, mirroring the documented real-world trapped-balance incident that required a manual remediation migration [8](#0-7) .

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L2256-2263)
```rust
		#[pallet::weight(T::WeightInfo::unbond())]
		pub fn unbond(
			origin: OriginFor<T>,
			member_account: AccountIdLookupOf<T>,
			#[pallet::compact] unbonding_points: BalanceOf<T>,
		) -> DispatchResult {
			let who = ensure_signed(origin)?;
			let member_account = T::Lookup::lookup(member_account)?;
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L2290-2291)
```rust
			let active_era = T::StakeAdapter::current_era();
			let unbond_era = T::StakeAdapter::bonding_duration().saturating_add(active_era);
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L2293-2323)
```rust
			// Unbond in the actual underlying nominator.
			let unbonding_balance = bonded_pool.dissolve(unbonding_points);
			T::StakeAdapter::unbond(Pool::from(bonded_pool.bonded_account()), unbonding_balance)?;

			// Note that we lazily create the unbonding pools here if they don't already exist
			let mut sub_pools = SubPoolsStorage::<T>::get(member.pool_id)
				.unwrap_or_default()
				.maybe_merge_pools(active_era);

			// Update the unbond pool associated with the current era with the unbonded funds. Note
			// that we lazily create the unbond pool if it does not yet exist.
			if !sub_pools.with_era.contains_key(&unbond_era) {
				sub_pools
					.with_era
					.try_insert(unbond_era, UnbondPool::default())
					// The above call to `maybe_merge_pools` should ensure there is
					// always enough space to insert.
					.defensive_map_err::<Error<T>, _>(|_| {
						DefensiveError::NotEnoughSpaceInUnbondPool.into()
					})?;
			}

			let points_unbonded = sub_pools
				.with_era
				.get_mut(&unbond_era)
				// The above check ensures the pool exists.
				.defensive_ok_or::<Error<T>>(DefensiveError::PoolNotFound.into())?
				.issue(unbonding_balance);

			// Try and unbond in the member map.
			member.try_unbond(unbonding_points, points_unbonded, unbond_era)?;
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
