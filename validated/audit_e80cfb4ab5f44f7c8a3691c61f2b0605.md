Based on my investigation, I found a strong structural analog to the "liquidator not incentivized when debt/position is too small to reward" problem: the permissionless `apply_slash` mechanism in `pallet-nomination-pools`, which similarly refuses to let a "reporter" (equivalent of a liquidator) correct a member's under-collateralized/slashed state when the amount is deemed "too small," leaving stale accounting until the affected member voluntarily unbonds.

### Title
Permissionless `apply_slash` in Nomination Pools blocks reward/correction for sub-ED member slashes, allowing stale exchange-rate accounting to persist - (File: `substrate/frame/nomination-pools/src/lib.rs`)

### Summary
`pallet-nomination-pools::apply_slash` is the permissionless "liquidator"-style entrypoint that lets anyone report and force-apply a pending slash for a pool member, receiving a slashing reward via `T::StakeAdapter::member_slash` → `delegated-staking::do_slash`'s reporter cut [1](#0-0) . Just like the VodkaVault liquidator who gets nothing when `returnedUSDC < debtPortion`, the reporter here gets nothing (and the call reverts outright) when a member's pending slash amount is below `ExistentialDeposit`, via the `enforce_min_slash` gate in `do_apply_slash` [2](#0-1) .

### Finding Description
`do_apply_slash` computes `pending_slash` as the delta between a member's actual (real) delegated balance and their recorded pool balance, then requires `pending_slash >= T::Currency::minimum_balance()` before allowing a permissionless caller to apply it and earn a reward [3](#0-2) . If the amount is below ED, the call fails with `Error::SlashTooLow` and no reward is paid at all — mirroring the reported pattern where "liquidators receive reward only if... debt [is] higher," so callers have zero incentive to trigger correction of small-but-real bad debt/slash exposure. This was confirmed as intentional behavior in `prdoc/stable2503/pr_6540.prdoc`, which states such small slashes are deferred until the member withdraws [4](#0-3) , and is exercised directly in the test suite where a member's pending slash of `1` (below ED of `2`) causes `apply_slash` to hard-fail with `SlashTooLow` while the pool-level `pending_slash` (tracked in `delegated-staking`'s `AgentLedger`) remains unresolved [5](#0-4) .

Because no unprivileged actor is incentivized (or even permitted) to correct these small member-level slashes, the member's recorded `PoolMember` balance stays inflated relative to their true delegated (already-slashed) balance until they personally call `unbond`/`withdraw_unbonded`, at which point `do_apply_slash` is invoked with `enforce_min_slash = false` [6](#0-5) . In the interim, other pool operations (reward accounting via `RewardPool`, exchange-rate math via `point_to_balance`, `bonded_pool.points`) rely on the pool's aggregate state which does not reflect this member's already-realized loss.

### Impact Explanation
This does not permit outright fund theft, but it reproduces the exact "unfair" incentive gap from the report: it deincentivizes timely correction of bad debt in a public accounting system, letting inconsistency accumulate silently until forced at exit. Given `pallet-nomination-pools` underlies real staking value across the ecosystem, protracted unresolved sub-ED slashes for many members can distort pool health signals (pending slash visible via `api_pool_pending_slash`/`api_member_pending_slash`) and delay recognition of loss, similar in spirit — though smaller in blast radius — to the "protocol deals with severe losses" concern in the original report.

### Likelihood Explanation
Low-to-medium. It requires an offending validator/agent slash event that produces a per-member share below the existential deposit, which is a realistic edge case for small-balance pool members after any slashing event, and is explicitly acknowledged/handled (deferred, not prevented) by the Parity team itself in `pr_6540.prdoc`.

### Recommendation
Consider paying a proportional (even if small) reward to the reporter regardless of whether the pending slash meets the ED threshold, batching sub-ED corrections instead of fully blocking them, so permissionless actors remain incentivized to keep pool accounting current — analogous to the bug report's recommendation to still reward liquidators partially even on underwater positions.

### Proof of Concept
The existing test `substrate/frame/nomination-pools/test-delegate-stake/src/lib.rs` (lines 679-712) demonstrates the exact mechanic: a pool-level slash of `3` leaves member `21` with a pending slash of `1` (below ED of `2`); `Pools::apply_slash(RuntimeOrigin::signed(10), 21)` is rejected with `PoolsError::SlashTooLow`, showing the permissionless correction/reward path is unusable for sub-ED slashes and the discrepancy persists until the member's own `unbond`/`withdraw_unbonded` call forces resolution [7](#0-6) .

### Citations

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

**File:** substrate/frame/nomination-pools/src/lib.rs (L3134-3160)
```rust
		/// Apply a pending slash on a member.
		///
		/// Fails unless [`crate::pallet::Config::StakeAdapter`] is of strategy type:
		/// [`adapter::StakeStrategyType::Delegate`].
		///
		/// The pending slash amount of the member must be equal or more than `ExistentialDeposit`.
		/// This call can be dispatched permissionlessly (i.e. by any account). If the execution
		/// is successful, fee is refunded and caller may be rewarded with a part of the slash
		/// based on the [`crate::pallet::Config::StakeAdapter`] configuration.
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

**File:** prdoc/stable2503/pr_6540.prdoc (L1-16)
```text
# Schema: Polkadot SDK PRDoc Schema (prdoc) v1.0.0
# See doc at https://raw.githubusercontent.com/paritytech/polkadot-sdk/master/prdoc/schema_user.json

title: Only allow apply slash to be executed if the slash amount is atleast ED

doc:
  - audience: Runtime User
    description: |
      This change prevents `pools::apply_slash` from being executed when the pending slash amount of the member is lower
      than the ED. With this change, such small slashes will still be applied but only when member funds are withdrawn.

crates:
- name: pallet-nomination-pools-runtime-api
  bump: patch
- name: pallet-nomination-pools
  bump: major
```

**File:** substrate/frame/nomination-pools/test-delegate-stake/src/lib.rs (L679-712)
```rust
		// Apply a slash that happened in era 100. This is typically applied with a delay.
		// Of the total 100, 50 is slashed.
		assert_eq!(BondedPools::<T>::get(1).unwrap().points, 40);

		// no pending slash yet.
		assert_eq!(Pools::api_pool_pending_slash(1), 0);
		// and therefore applying slash fails
		assert_noop!(
			Pools::apply_slash(RuntimeOrigin::signed(10), 21),
			PoolsError::<Runtime>::NothingToSlash
		);

		hypothetically!({
			// a very small amount is slashed
			pallet_staking_async::slashing::do_slash::<Runtime>(
				&POOL1_BONDED,
				3,
				&mut Default::default(),
				&mut Default::default(),
				100,
			);

			// ensure correct amount is pending to be slashed
			assert_eq!(Pools::api_pool_pending_slash(1), 3);

			// 21 has pending slash lower than ED (2)
			assert_eq!(Pools::api_member_pending_slash(21), 1);

			// slash fails as minimum pending slash amount not met.
			assert_noop!(
				Pools::apply_slash(RuntimeOrigin::signed(10), 21),
				PoolsError::<Runtime>::SlashTooLow
			);
		});
```
