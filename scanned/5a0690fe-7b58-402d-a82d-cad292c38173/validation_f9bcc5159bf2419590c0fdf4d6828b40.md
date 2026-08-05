Found a genuine analog: **`pallet_staking::do_bond_extra`** (`substrate/frame/staking/src/pallet/impls.rs:164-190`) silently clamps the bonded amount while `pallet-nomination-pools` computes LP-style "points" from the *requested* amount before that clamping is applied and never learns the actual bonded delta.

### Title
Nomination pool points minted from requested bond amount while `pallet-staking::do_bond_extra` silently clamps to free balance, causing pool share/points to balance desync - (File: `substrate/frame/nomination-pools/src/lib.rs`)

### Summary
This mirrors the MarginalV1 bug class: an accounting variable (`reserve0/1`) is decremented by a *desired/ratio-derived* amount instead of the amount the downstream system actually consumed, causing the internal ledger to diverge from real backing value. In `pallet-nomination-pools`, `BondedPool::try_bond_funds` (`substrate/frame/nomination-pools/src/lib.rs:1315-1337`) issues pool points using the caller-supplied `amount` *before* invoking `T::StakeAdapter::pledge_bond`, which for the `TransferStake` adapter (`substrate/frame/nomination-pools/src/adapter.rs:289-308`) ultimately calls `pallet_staking::bond_extra`. Inside `pallet-staking`, `do_bond_extra` (`substrate/frame/staking/src/pallet/impls.rs:164-190`) computes:
```rust
let extra = if Self::is_virtual_staker(stash) {
    additional
} else {
    additional.min(asset::free_to_stake::<T>(stash))
};
```
i.e. the *actually bonded* delta (`extra`) can be **less** than the `additional` (== pool's `amount`) requested, if the stash's free-to-stake balance is lower than expected at bonding time (e.g. holds/freezes/locks placed on the pool's bonded account by other logic, existential-deposit interactions, or concurrent balance mutations within the same block). `do_bond_extra` returns `Ok(())` regardless — it never signals back how much was actually bonded.

### Finding Description
`BondedPool::try_bond_funds` computes `points_issued = self.issue(amount)` using the full requested `amount` (`substrate/frame/nomination-pools/src/lib.rs:1321-1323`), then calls `pledge_bond(..., amount, ty)` and, on success, unconditionally accrues `TotalValueLocked` by the same `amount` (`substrate/frame/nomination-pools/src/lib.rs:1332-1334`). Because `pledge_bond` → `Staking::bond_extra` → `do_bond_extra` clamps the actually-staked delta to `free_to_stake` and swallows the shortfall, the pool's points-to-balance ratio and `TotalValueLocked` accounting are computed from the *requested* value, not the value actually reflected in the staking ledger. This is exactly the reported bug pattern: subtracting/crediting a ratio/desired amount instead of the value returned by the downstream call that can legitimately under-deliver. [1](#0-0) [2](#0-1) [3](#0-2) 

### Impact Explanation
If points are minted for value that never actually reached the staking ledger, the points:balance ratio of the bonded pool is inflated relative to the real bonded stake. Every member's share of the pool (and thus withdrawable/unbondable value computed via `points_to_balance`) is computed against a ledger balance that is lower than what the points imply, meaning later members redeeming points can drain real bonded value belonging to earlier depositors, or the pool can end up unable to fully honor unbonds for all members — a fund-accounting integrity break for staking/asset accounting, which is in the accepted impact category ("Balances ... staking, pools ... must conserve value and settle exactly once to the rightful beneficiary and amount").

### Likelihood Explanation
Reaching this requires the pool's bonded (stash) account's free-to-stake balance to actually be lower than the transferred `amount` at the moment `bond_extra` executes — this can occur, for example, when the bonded pool account is holding other locks/freezes/reserves (via generic `fungible` hold/freeze usage that reduces `free_to_stake` without pool code being aware), or through rounding/existential-deposit edge effects in the same block as `bond_extra`. This does not require any privileged, governance, validator, or malicious-peer action — it is triggerable purely through the ordinary `bond_extra`/`join` public extrinsics under conditions where the stash account's stakeable balance is momentarily constrained, so it satisfies the "unprivileged attacker/ordinary user path" requirement, though the exact conditions to force the clamp deterministically are somewhat contingent on runtime-specific hold/freeze configuration.

### Recommendation
`pledge_bond`/`try_bond_funds` should use the amount actually reflected in the staking ledger, not the requested amount, when issuing points and updating `TotalValueLocked`. Concretely: have `StakingInterface::bond_extra` (and the `pledge_bond` adapter methods) return the actually-bonded delta, and have `BondedPool::try_bond_funds` compute `points_issued` and `TotalValueLocked` accrual from that returned value instead of the caller-supplied `amount`. Alternatively, assert/verify post-call that `ledger.active` increased by exactly `amount` and fail the extrinsic otherwise, rather than silently proceeding with points minted for value that was never actually staked.

### Proof of Concept
Not independently reproducible from static analysis alone: exploiting this requires constructing a runtime configuration/scenario where the pool's bonded (stash) account's `free_to_stake` balance is strictly less than the `amount` passed into `bond_extra` at call time (e.g., an externally-imposed hold/freeze on the bonded account reducing free balance between the `T::Currency::transfer` in `pledge_bond` and the `Staking::bond_extra` call, or a race within the same extrinsic execution). I was not able to fully confirm within this session whether any current runtime configuration in this repo actually places such holds/freezes on nomination-pool bonded accounts, so likelihood/exploitability should be validated with a concrete integration test (e.g., in `substrate/frame/nomination-pools/src/tests.rs` or `test-delegate-stake`) that engineers a `free_to_stake` shortfall before calling `bond_extra`, then checks whether `BondedPool.points` / `TotalValueLocked` diverge from `Staking::ledger(stash).active`.

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L1315-1337)
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
	}
```

**File:** substrate/frame/staking/src/pallet/impls.rs (L164-190)
```rust
	pub(super) fn do_bond_extra(stash: &T::AccountId, additional: BalanceOf<T>) -> DispatchResult {
		let mut ledger = Self::ledger(StakingAccount::Stash(stash.clone()))?;

		// for virtual stakers, we don't need to check the balance. Since they are only accessed
		// via low level apis, we can assume that the caller has done the due diligence.
		let extra = if Self::is_virtual_staker(stash) {
			additional
		} else {
			// additional amount or actual balance of stash whichever is lower.
			additional.min(asset::free_to_stake::<T>(stash))
		};

		ledger.total = ledger.total.checked_add(&extra).ok_or(ArithmeticError::Overflow)?;
		ledger.active = ledger.active.checked_add(&extra).ok_or(ArithmeticError::Overflow)?;
		// last check: the new active amount of ledger must be more than ED.
		ensure!(ledger.active >= asset::existential_deposit::<T>(), Error::<T>::InsufficientBond);

		// NOTE: ledger must be updated prior to calling `Self::weight_of`.
		ledger.update()?;
		// update this staker in the sorted list, if they exist in it.
		if T::VoterList::contains(stash) {
			let _ = T::VoterList::on_update(&stash, Self::weight_of(stash)).defensive();
		}

		Self::deposit_event(Event::<T>::Bonded { stash: stash.clone(), amount: extra });

		Ok(())
```

**File:** substrate/frame/nomination-pools/src/adapter.rs (L289-308)
```rust
	fn pledge_bond(
		who: Member<T::AccountId>,
		pool_account: Pool<Self::AccountId>,
		reward_account: &Self::AccountId,
		amount: BalanceOf<T>,
		bond_type: BondType,
	) -> DispatchResult {
		match bond_type {
			BondType::Create => {
				// first bond
				T::Currency::transfer(&who.0, &pool_account.0, amount, Preservation::Expendable)?;
				Staking::bond(&pool_account.0, amount, &reward_account)
			},
			BondType::Extra => {
				// additional bond
				T::Currency::transfer(&who.0, &pool_account.0, amount, Preservation::Preserve)?;
				Staking::bond_extra(&pool_account.0, amount)
			},
		}
	}
```
