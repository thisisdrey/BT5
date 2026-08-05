## Analog identified

The BakerFi bug reduces to: *a withdrawal path performs a mandatory sub-operation against an external/complex accounting subsystem before releasing already-available funds, and any failure of that sub-operation other than the single expected "nothing to do" case aborts the entire withdrawal — with no way to skip or exclude the failing sub-operation.*

The closest local analog is `pallet-nomination-pools::withdraw_unbonded`, which unconditionally calls `Self::do_apply_slash` before releasing funds that are already sitting, fully unlocked, in `SubPoolsStorage`.

### Title
Nomination-pools `withdraw_unbonded` permanently locks already-unlocked member funds if `do_apply_slash` fails for any reason other than `NothingToSlash` - (File: `substrate/frame/nomination-pools/src/lib.rs`)

### Summary
`Call::withdraw_unbonded` first tries to apply any pending slash via `Self::do_apply_slash`, and treats every error except the expected `NothingToSlash` as fatal, aborting the whole extrinsic with `DefensiveError::SlashNotApplied` — even though the member's actual withdrawable balance (tracked independently in `SubPoolsStorage`) is completely unrelated to the slash-application step and is otherwise ready to be released.

### Finding Description
`withdraw_unbonded` gates the release of already-unbonded funds behind a mandatory slash-application step: [1](#0-0) 

`do_apply_slash` computes `member_pending_slash`, and if it's non-zero, forwards to `T::StakeAdapter::member_slash`: [2](#0-1) 

`member_pending_slash` itself can fail with `Error::NotMigrated` when `member_delegation_balance` returns `None` for a member whose delegation state is inconsistent with the pool's `pending_slash()` view: [3](#0-2) 

Back in `withdraw_unbonded`, only `Error::<T>::NothingToSlash` is treated as an expected/recoverable outcome. Any other error path (e.g. `NotMigrated`, or a genuine failure returned from `T::StakeAdapter::member_slash`, whose `DelegateStake` implementation ultimately calls into `pallet-delegated-staking`/`pallet-staking` hold/transfer primitives that can fail) causes the pallet to `return Err(Error::<T>::Defensive(DefensiveError::SlashNotApplied))` before the member's genuinely-unlocked balance in `SubPoolsStorage` (see `withdraw_unlocked`/`SubPools::dissolve`) is ever touched: [4](#0-3) [5](#0-4) 

This is structurally identical to the BakerFi bug: the vault's `_deallocateAssets`/`removeStrategy` loops always attempt an operation against *every* strategy (including a paused one) before funds can move, and a single failing "leg" (there, an external protocol pause; here, the slash-application leg tied to `pending_slash`/`member_slash`) reverts the whole withdrawal even though the requested funds live in a completely separate, healthy part of the accounting (the `no_era`/`with_era` unbond sub-pools). There is no code path in `withdraw_unbonded` that lets a caller skip slash application and still withdraw the unlocked sub-pool balance, and no permissioned or permissionless call exists to reset/clear a stuck `pending_slash` condition independently of `apply_slash`, which itself requires the exact same `member_slash` call to succeed: [6](#0-5) 

### Impact Explanation
If the pool is in `DelegateStake` mode and `T::StakeAdapter::pending_slash` for the pool remains persistently non-zero for a given member (whether due to `NotMigrated` state, or any non-trivial failure surfaced by the delegated-staking hold/release primitives underlying `member_slash`), then:
- `withdraw_unbonded` for that member permanently reverts via `Error::Defensive(SlashNotApplied)`.
- `apply_slash` (the only other entry point that could clear the condition) drives through the identical `do_apply_slash`/`member_slash` call and fails the same way.
- The member's already-unbonded balance recorded in `SubPoolsStorage` (real, non-slashed funds tied to specific eras) becomes permanently unreachable, exactly mirroring the "funds deposited in the vault will be permanently locked" outcome from the source report.

This is a direct, protocol-level fund-lock condition reachable without any privileged actor, matching the "permanent user-fund lock" impact class.

### Likelihood Explanation
Reaching this requires the pool to be on `DelegateStake` with a lingering `pending_slash` for the affected member and the `member_slash` step to keep failing (or the `NotMigrated` mismatch to persist). This is a narrower trigger than the BakerFi case (which needs only a third-party pause), and I could not find in the indexed code an explicit reproducible unit test demonstrating `member_slash` itself failing after a legitimate non-zero `pending_slash` (the delegated-staking hold accounting is largely defensive/asserted rather than fallible in the paths I could inspect). Confidence in exploitability under normal, correctly-configured `DelegateStake` deployments is therefore moderate, not certain — the `NotMigrated` sub-case is the more concretely demonstrable trigger, but it is already partially guarded by the `api_member_needs_delegate_migration` check at the top of `withdraw_unbonded`, which reduces (but does not fully eliminate, since `pending_slash` and delegation-migration state are tracked separately) the practical likelihood.

### Recommendation
Decouple "release of already-unlocked sub-pool balance" from "slash application." `withdraw_unbonded` should be able to withdraw the member's already-unbonded/unslashed sub-pool balance even when `do_apply_slash` cannot currently be applied, deferring/queuing the slash rather than blocking the whole extrinsic; alternatively, provide a governance/permissionless recovery call that can force-resolve or reset a stuck `pending_slash` for a member independent of the exact `member_slash` code path used by `withdraw_unbonded`/`apply_slash`.

### Proof of Concept
Conceptual reproduction (would need to be validated in a `DelegateStake`-configured nomination-pools test harness):
1. Configure a pool with `StakeAdapter = DelegateStake`, add member `M`, have `M` `unbond` and wait past `bonding_duration` so `SubPoolsStorage` for `M`'s era contains a real withdrawable balance.
2. Cause the pool's `pending_slash()` to become non-zero (a slash event on the pool) and manipulate/observe a state where `T::StakeAdapter::member_slash` for `M` fails on a subsequent call — e.g., by returning a non-`NothingToSlash`/`SlashTooLow` error from `member_slash` (any real error surfaced by `pallet-delegated-staking`'s hold/transfer logic would suffice) — or where `member_delegation_balance` for `M` returns `None` producing `Error::NotMigrated`.
3. Call `Pools::withdraw_unbonded(M, ..)`. Observe it returns `Err(Error::Defensive(DefensiveError::SlashNotApplied))` per [7](#0-6) , even though `M`'s unlocked `SubPools` balance is untouched and legitimately withdrawable.
4. Call `Pools::apply_slash(M)`; observe it fails identically, since it drives through the same `do_apply_slash`/`member_slash` call at [8](#0-7) , leaving `M` with no path to ever withdraw the unlocked funds.

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L662-687)
```rust
	/// Withdraw any funds in [`Self::unbonding_eras`] who's deadline in reached and is fully
	/// unlocked.
	///
	/// Returns a a subset of [`Self::unbonding_eras`] that got withdrawn.
	///
	/// Infallible, noop if no unbonding eras exist.
	fn withdraw_unlocked(
		&mut self,
		active_era: EraIndex,
	) -> BoundedBTreeMap<EraIndex, BalanceOf<T>, T::MaxUnbonding> {
		// NOTE: if only drain-filter was stable..
		let mut removed_points =
			BoundedBTreeMap::<EraIndex, BalanceOf<T>, T::MaxUnbonding>::default();
		self.unbonding_eras.retain(|e, p| {
			if *e > active_era {
				true
			} else {
				removed_points
					.try_insert(*e, *p)
					.expect("source map is bounded, this is a subset, will be bounded; qed");
				false
			}
		});
		removed_points
	}
}
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L1567-1578)
```rust
	/// Dissolve some points from the unbonding pool, reducing the balance of the pool
	/// proportionally. This is the opposite of `issue`.
	///
	/// Returns the actual amount of `Balance` that was removed from the pool.
	fn dissolve(&mut self, points: BalanceOf<T>) -> BalanceOf<T> {
		let balance_to_unbond = self.point_to_balance(points);
		self.points = self.points.saturating_sub(points);
		self.balance = self.balance.saturating_sub(balance_to_unbond);

		balance_to_unbond
	}
}
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L2417-2432)
```rust
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
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L3143-3160)
```rust
		#[pallet::call_index(23)]
		#[pallet::weight(T::WeightInfo::apply_slash())]
		pub fn apply_slash(
			origin: OriginFor<T>,
			member_account: AccountIdLookupOf<T>,
		) -> DispatchResultWithPostInfo {
			ensure!(
				T::StakeAdapter::strategy_type() == adapter::StakeStrategyType::Delegate,
				Error::<T>::NotSupported
			);

			let who = ensure_signed(origin)?;
			let member_account = T::Lookup::lookup(member_account)?;
			Self::do_apply_slash(&member_account, Some(who), true)?;

			// If successful, refund the fees.
			Ok(Pays::No.into())
		}
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L3816-3841)
```rust
	/// Slash member against the pending slash for the pool.
	fn do_apply_slash(
		member_account: &T::AccountId,
		reporter: Option<T::AccountId>,
		enforce_min_slash: bool,
	) -> DispatchResult {
		let member = PoolMembers::<T>::get(member_account).ok_or(Error::<T>::PoolMemberNotFound)?;

		let pending_slash =
			Self::member_pending_slash(Member::from(member_account.clone()), member.clone())?;

		// ensure there is something to slash.
		ensure!(!pending_slash.is_zero(), Error::<T>::NothingToSlash);

		if enforce_min_slash {
			// ensure slashed amount is at least the minimum balance.
			ensure!(pending_slash >= T::Currency::minimum_balance(), Error::<T>::SlashTooLow);
		}

		T::StakeAdapter::member_slash(
			Member::from(member_account.clone()),
			Pool::from(Pallet::<T>::generate_bonded_account(member.pool_id)),
			pending_slash,
			reporter,
		)
	}
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L3846-3873)
```rust
	fn member_pending_slash(
		member_account: Member<T::AccountId>,
		pool_member: PoolMember<T>,
	) -> Result<BalanceOf<T>, DispatchError> {
		// only executed in tests: ensure the member account is correct.
		debug_assert!(
			PoolMembers::<T>::get(member_account.clone().get()).expect("member must exist") ==
				pool_member
		);

		let pool_account = Pallet::<T>::generate_bonded_account(pool_member.pool_id);
		// if the pool doesn't have any pending slash, it implies the member also does not have any
		// pending slash.
		if T::StakeAdapter::pending_slash(Pool::from(pool_account.clone())).is_zero() {
			return Ok(Zero::zero());
		}

		// this is their actual held balance that may or may not have been slashed.
		let actual_balance = T::StakeAdapter::member_delegation_balance(member_account)
			// no delegation implies the member delegation is not migrated yet to `DelegateStake`.
			.ok_or(Error::<T>::NotMigrated)?;

		// this is their balance in the pool
		let expected_balance = pool_member.total_balance();

		// return the amount to be slashed.
		Ok(actual_balance.saturating_sub(expected_balance))
	}
```
