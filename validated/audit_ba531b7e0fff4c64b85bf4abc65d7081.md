This confirms the claim's technical accuracy. `payout_stakers_by_page` (via `do_payout_stakers_by_page`) resolves the ledger through `Self::ledger(StakingAccount::Stash(validator_stash.clone()))`, and `StakingLedger::get` requires `<Bonded<T>>::get(&stash)` to succeed, returning `Error::NotStash` otherwise [1](#0-0) [2](#0-1) . `reap_stash` is indeed a `ensure_signed`-only, permissionless extrinsic gated only on the stash/ledger balance falling below existential deposit, with no check on unclaimed era rewards, and calls `kill_stash` which removes `Bonded`/`Ledger`/`Payee` via `StakingLedger::<T>::kill` [3](#0-2) [4](#0-3) . The era reward accounting (`ErasValidatorReward`, `ErasRewardPoints`, `ErasStakersPaged`/`ErasStakersOverview`, `ClaimedRewards`) is separate storage only cleared later by `clear_era_information` at `HistoryDepth` expiry, and is untouched by `kill_stash` [5](#0-4) . There is no `pending_rewards` check gating `reap_stash`, confirming the gap described in the claim.

Audit Report

## Title
Permissionless `reap_stash` permanently locks unclaimed validator/nominator era rewards - (File: `substrate/frame/staking/src/pallet/impls.rs`)

## Summary
`reap_stash` is a signed, permissionless extrinsic callable against any stash whose balance/ledger total has fallen below the existential deposit. It calls `kill_stash`, which removes `Bonded`, `Ledger`, and `Payee` for the stash without verifying that all past-era rewards accrued to that stash have been claimed. Since `do_payout_stakers_by_page` requires a live `Bonded`/`Ledger` entry to resolve the stash, any unclaimed era reward for that stash becomes permanently unpayable once reaped.

## Finding Description
`reap_stash` in `substrate/frame/staking/src/pallet/mod.rs` (lines 1807-1831) checks only balance/ledger-total conditions against the existential deposit before calling `Self::kill_stash(&stash, num_slashing_spans)`. `kill_stash` (`substrate/frame/staking/src/pallet/impls.rs`, lines 787-798) calls `StakingLedger::<T>::kill(&stash)`, which removes `Bonded`, `Ledger`, and `Payee` entries. Neither function checks `EraInfo`/`ClaimedRewards` for outstanding claimable pages.

Separately, `do_payout_stakers_by_page` (`substrate/frame/staking/src/pallet/impls.rs`, lines 253-296) resolves the ledger via `Self::ledger(StakingAccount::Stash(validator_stash.clone()))`. `StakingLedger::get` (`substrate/frame/staking/src/ledger.rs`, lines 111-122) requires `<Bonded<T>>::get(&stash)` to succeed, returning `Error::NotStash` otherwise. Once `Bonded` is removed by `kill_stash`, this lookup fails permanently for that stash.

The era-indexed reward bookkeeping (`ErasValidatorReward`, `ErasRewardPoints`, `ErasStakersPaged`/`ErasStakersOverview`, `ClaimedRewards`) is stored independently of `Bonded`/`Ledger`/`Payee` and is only pruned by `clear_era_information` (lines 800-821) at `HistoryDepth` expiry — not by `kill_stash`. Consequently, if any era within `HistoryDepth` has an unclaimed reward page for the stash at the moment of reaping, that reward becomes permanently unclaimable, even though the underlying accounting data proving entitlement still exists.

## Impact Explanation
This matches the "permanent user-fund lock" impact category: rewards legitimately earned by a validator and its exposed nominators become permanently unpayable once the stash is reaped, with no recovery path since the only code path that can pay out (`payout_stakers`/`payout_stakers_by_page`) unconditionally requires a resolvable `Bonded`/`Ledger` entry for the stash.

## Likelihood Explanation
The precondition (stash balance or ledger total dropping below existential deposit) is a routine occurrence following slashing or a full `unbond`+`withdraw_unbonded` sequence, and is not otherwise rare. `reap_stash` requires only `ensure_signed`, is fee-refunded (`Pays::No`), and can be called by any unprivileged account racing an unclaimed `payout_stakers` call. No governance, validator privilege, or collusion is required.

## Recommendation
Before executing `kill_stash`, check for outstanding claimable reward pages for the stash across the claimable era window (e.g., via `EraInfo::get_next_claimable_page`/equivalent pending-rewards check) and either force-settle outstanding pages automatically or reject/defer the reap (return an error) until all claimable pages have been consumed.

## Proof of Concept
1. Validator `V` with nominators earns reward points in era `E` via `reward_by_ids`, recorded in `ErasRewardPoints`/`ErasValidatorReward`.
2. `V`'s stash balance or `ledger.total` drops below the existential deposit (e.g., via slashing or `unbond`+`withdraw_unbonded`) while era `E`'s reward page for `V` remains unclaimed (`ClaimedRewards` does not include that page).
3. Any unprivileged account calls `Staking::reap_stash(origin, V, num_slashing_spans)`; the `reapable` check passes and `kill_stash` removes `Bonded::<T>` and `Ledger::<T>` entries for `V`.
4. Any account calls `Staking::payout_stakers_by_page(origin, V, E, page)`.
5. `do_payout_stakers_by_page` calls `Self::ledger(StakingAccount::Stash(V))`, which fails with `Error::NotStash` because `Bonded::<T>::get(&V)` is `None`, permanently blocking payout of the still-outstanding reward for era `E`/page for validator `V` and its nominators.

### Citations

**File:** substrate/frame/staking/src/pallet/impls.rs (L283-290)
```rust
		let account = StakingAccount::Stash(validator_stash.clone());
		let mut ledger = Self::ledger(account.clone()).or_else(|_| {
			if StakingLedger::<T>::is_bonded(account) {
				Err(Error::<T>::NotController.into())
			} else {
				Err(Error::<T>::NotStash.with_weight(T::WeightInfo::payout_stakers_alive_staked(0)))
			}
		})?;
```

**File:** substrate/frame/staking/src/pallet/impls.rs (L787-798)
```rust
	pub(crate) fn kill_stash(stash: &T::AccountId, num_slashing_spans: u32) -> DispatchResult {
		slashing::clear_stash_metadata::<T>(&stash, num_slashing_spans)?;

		// removes controller from `Bonded` and staking ledger from `Ledger`, as well as reward
		// setting of the stash in `Payee`.
		StakingLedger::<T>::kill(&stash)?;

		Self::do_remove_validator(&stash);
		Self::do_remove_nominator(&stash);

		Ok(())
	}
```

**File:** substrate/frame/staking/src/pallet/impls.rs (L800-821)
```rust
	/// Clear all era information for given era.
	pub(crate) fn clear_era_information(era_index: EraIndex) {
		// FIXME: We can possibly set a reasonable limit since we do this only once per era and
		// clean up state across multiple blocks.
		let mut cursor = <ErasStakers<T>>::clear_prefix(era_index, u32::MAX, None);
		debug_assert!(cursor.maybe_cursor.is_none());
		cursor = <ErasStakersClipped<T>>::clear_prefix(era_index, u32::MAX, None);
		debug_assert!(cursor.maybe_cursor.is_none());
		cursor = <ErasValidatorPrefs<T>>::clear_prefix(era_index, u32::MAX, None);
		debug_assert!(cursor.maybe_cursor.is_none());
		cursor = <ClaimedRewards<T>>::clear_prefix(era_index, u32::MAX, None);
		debug_assert!(cursor.maybe_cursor.is_none());
		cursor = <ErasStakersPaged<T>>::clear_prefix((era_index,), u32::MAX, None);
		debug_assert!(cursor.maybe_cursor.is_none());
		cursor = <ErasStakersOverview<T>>::clear_prefix(era_index, u32::MAX, None);
		debug_assert!(cursor.maybe_cursor.is_none());

		<ErasValidatorReward<T>>::remove(era_index);
		<ErasRewardPoints<T>>::remove(era_index);
		<ErasTotalStake<T>>::remove(era_index);
		ErasStartSessionIndex::<T>::remove(era_index);
	}
```

**File:** substrate/frame/staking/src/ledger.rs (L111-122)
```rust
	pub(crate) fn get(account: StakingAccount<T::AccountId>) -> Result<StakingLedger<T>, Error<T>> {
		let (stash, controller) = match account.clone() {
			StakingAccount::Stash(stash) => {
				(stash.clone(), <Bonded<T>>::get(&stash).ok_or(Error::<T>::NotStash)?)
			},
			StakingAccount::Controller(controller) => (
				Ledger::<T>::get(&controller)
					.map(|l| l.stash)
					.ok_or(Error::<T>::NotController)?,
				controller,
			),
		};
```

**File:** substrate/frame/staking/src/pallet/mod.rs (L1807-1831)
```rust
		pub fn reap_stash(
			origin: OriginFor<T>,
			stash: T::AccountId,
			num_slashing_spans: u32,
		) -> DispatchResultWithPostInfo {
			ensure_signed(origin)?;

			// virtual stakers should not be allowed to be reaped.
			ensure!(!Self::is_virtual_staker(&stash), Error::<T>::VirtualStakerNotAllowed);

			let ed = asset::existential_deposit::<T>();
			let origin_balance = asset::total_balance::<T>(&stash);
			let ledger_total =
				Self::ledger(Stash(stash.clone())).map(|l| l.total).unwrap_or_default();
			let reapable = origin_balance < ed ||
				origin_balance.is_zero() ||
				ledger_total < ed ||
				ledger_total.is_zero();
			ensure!(reapable, Error::<T>::FundedTarget);

			// Remove all staking-related information and lock.
			Self::kill_stash(&stash, num_slashing_spans)?;

			Ok(Pays::No.into())
		}
```
