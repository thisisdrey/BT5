Audit Report

## Title
Not a valid new vulnerability — the underlying `CurrentEra`/`ActiveEra` desync in pool withdrawals has already been remediated in the current codebase

## Summary
The claim reproduces the exact narrative already documented in `prdoc/stable2512-3/pr_11018.prdoc`: a `CurrentEra` vs `ActiveEra` mismatch previously caused one pool member's points to be dissolved without releasing the matching held balance, which was fixed via a one-off migration `ClaimTrappedBalance` backed by the generic internal function `do_claim_trapped_balance` [1](#0-0) . Critically, the repository also contains `prdoc/stable2512-2/pr_10986.prdoc`, titled "[Pool] Use active era for withdrawals," whose description states the fix standardized on using active era consistently in pools and staking, explicitly restricting `CurrentEra` usage to election logic only — this is the generic root-cause fix the claim asserts does not exist.

## Finding Description
The claim's own evidence undermines its central assertion. It states: "this migration only fixes the balance for one already-affected, statically-known account... it does not correct the general withdraw_unlocked/withdraw_unbonded control flow that produced the mismatch in the first place." However, `prdoc/stable2512-2/pr_10986.prdoc` shows the general control-flow root cause (era-source inconsistency between `CurrentEra` and `ActiveEra`) was addressed separately and generically:

```
title: '[Pool] Use active era for withdrawals'
doc:
- audience: Runtime Dev
  description: Standardising using active era in pools and staking. Current Era should
    only be used for election logic
crates:
- name: pallet-nomination-pools
- name: pallet-staking-async
```

This means the `pr_11018` migration was a targeted remediation for balance that was *already* trapped by the historical bug, while `pr_10986` is the generic fix preventing recurrence — exactly the "generic self-healing" fix the claim recommends but claims is missing. Additionally, in `pallet-staking-async`'s current `do_withdraw_unbonded` implementation, era-gating logic uses a single, consistently derived `active_era = Rotator::<T>::active_era()` value throughout (for `calculate_earliest_withdrawal_era`, slash-application checks, and `consolidate_unlocked`), with no `CurrentEra` read in this path [2](#0-1) , consistent with the `pr_10986` standardization. The claim does not identify any current code path where `CurrentEra` and `ActiveEra` are still mixed inconsistently in the pools withdrawal flow; it only cites the historical prdoc and migration as evidence of a currently exploitable defect.

Furthermore, the claim requires "a runtime/timing condition" in the staking backend for era desync to occur — this is not an externally triggerable input from an unprivileged extrinsic caller. It depends on an internal state inconsistency between `CurrentEra` and `ActiveEra` bookkeeping that the project has already targeted for structural fix, rather than a reachable exploit path from attacker-controlled input to bad state under the current code.

## Impact Explanation
No currently reachable impact is demonstrated. The claim relies entirely on historical evidence (the `pr_11018` migration and its pre/post trapped-balance checks) which documents an *already-fixed* incident, not a live defect in the current `withdraw_unlocked`/`withdraw_unbonded` flow. No new code path is shown where an unprivileged caller can trigger points-dissolution ahead of balance release under present logic.

## Likelihood Explanation
Not applicable — the claim does not establish a currently reachable trigger condition distinct from the historical, already-remediated bug class.

## Recommendation
N/A — the requested generic fix (standardizing on active era, avoiding `CurrentEra`/`ActiveEra` divergence in pool withdrawal accounting) already appears to have been implemented per `pr_10986`. If residual risk remains, it would need to be demonstrated via a specific, currently-reachable code path in `withdraw_unlocked`/`do_withdraw_unbonded` that still mixes era sources, which the claim does not provide.

## Proof of Concept
Not provided by the claim beyond citing historical prdoc/migration artifacts; no reproducible test against current logic demonstrating a live desync was presented.

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L3295-3334)
```rust
	/// Claim trapped balance for a pool member.
	///
	/// In rare scenarios, pool members may have excess held balance that is not accounted
	/// for in their pool points. This can occur when points are incorrectly dissolved
	/// without releasing the corresponding held funds.
	///
	/// If the pool has any pending slash, it will be applied to the member first before
	/// claiming the trapped balance.
	///
	/// Safe to call multiple times or for non-existent members — returns `Ok(())` as a
	/// no-op when there is nothing to do.
	pub fn do_claim_trapped_balance(member_account: &T::AccountId) -> DispatchResult {
		ensure!(
			T::StakeAdapter::strategy_type() == adapter::StakeStrategyType::Delegate,
			Error::<T>::NotSupported
		);

		// Apply any pending slash first. Ignore NothingToSlash and PoolMemberNotFound
		// (member existence is validated below).
		match Self::do_apply_slash(member_account, None, false) {
			Ok(_) => {},
			Err(e)
				if e == Error::<T>::NothingToSlash.into() ||
					e == Error::<T>::PoolMemberNotFound.into() => {},
			Err(_) => {
				return Err(Error::<T>::Defensive(DefensiveError::SlashNotApplied).into());
			},
		};

		let member = match PoolMembers::<T>::get(member_account) {
			Some(m) => m,
			None => return Ok(()),
		};

		let expected_balance = member.total_balance();
		let actual_balance =
			T::StakeAdapter::member_delegation_balance(Member::from(member_account.clone()))
				.unwrap_or_default();

		let trapped_amount = actual_balance.saturating_sub(expected_balance);
```

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L259-280)
```rust
	pub(super) fn do_withdraw_unbonded(controller: &T::AccountId) -> Result<Weight, DispatchError> {
		let mut ledger = Self::ledger(Controller(controller.clone()))?;
		let (stash, old_total) = (ledger.stash.clone(), ledger.total);
		let active_era = Rotator::<T>::active_era();

		// Ensure last era slashes are applied. Else we block the withdrawals.
		if active_era > 1 {
			Self::ensure_era_slashes_applied(active_era.saturating_sub(1))?;
		}

		let earliest_era_to_withdraw = Self::calculate_earliest_withdrawal_era(active_era);

		log!(
			debug,
			"Withdrawing unbonded stake. Active_era is: {:?} | \
			Earliest era we can allow withdrawing: {:?}",
			active_era,
			earliest_era_to_withdraw
		);

		// withdraw unbonded balance from the ledger until earliest_era_to_withdraw.
		ledger = ledger.consolidate_unlocked(earliest_era_to_withdraw);
```
