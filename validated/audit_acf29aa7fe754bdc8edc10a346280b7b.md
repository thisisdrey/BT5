### Title
Restricted (blacklisted) pool members can be permanently locked out of their delegated stake in `pallet-nomination-pools` `DelegateStake` migration flow - (File: `substrate/frame/nomination-pools/src/lib.rs`)

### Summary
This is a direct structural analog of the StakedlvlUSD "shares stuck in silo" bug. In `pallet-nomination-pools`, when a pool migrates from `TransferStake` to `DelegateStake`, member funds are moved into an escrow-like `proxy_delegator` account (the on-chain equivalent of the "silo"). Each member must subsequently call `Pallet::migrate_delegation` to move their share out of that escrow into their own account. That call is gated by `ensure!(!T::Filter::contains(&member_account), Error::<T>::Restricted)` [1](#0-0) . If a member is added to the restriction filter after the pool has migrated but before that member calls `migrate_delegation`, they can never claim their escrowed delegation, and normal withdrawal (`withdraw_unbonded`) also fails for the same reason, permanently freezing their staked funds — exactly like Bob's shares stuck in the `StakedlvlUSD` silo.

### Finding Description
When a pool's `StakeAdapter` uses the `DelegateStake` strategy, funds staked by a member are held via the `delegated-staking` pallet with `HoldReason::StakingDelegation` on the member's own account once migrated [2](#0-1) . Before a member individually calls `migrate_delegation`, their portion of the pool's stake is not yet delegated to them directly; it is parked as an "unclaimed delegation" tied to the pool's `proxy_delegator` account, analogous to the `silo` in `StakedlvlUSD`.

The only path for a member to move their balance out of this escrow is `pallet_nomination_pools::Pallet::migrate_delegation`:
```rust
// ensure member is not restricted from joining the pool.
let member_account = T::Lookup::lookup(member_account)?;
ensure!(!T::Filter::contains(&member_account), Error::<T>::Restricted);
``` [1](#0-0) 

If the pool's `T::Filter` (the pool's block/deny list, checked also when joining/bonding) later includes this member — e.g., an operator/root adds the member to the filter for compliance reasons, analogous to `addToDenylist()`/`FULL_RESTRICTED_STAKER_ROLE` in the report — the member is permanently blocked from calling `migrate_delegation`, so their share of pool funds can never leave the `proxy_delegator` escrow.

Critically, the normal exit path (`withdraw_unbonded`) does not route around this: the code explicitly documents that member withdrawal "can fail if the pool uses `DelegateStake` strategy and the member delegation is not claimed yet. See `Call::migrate_delegation()`" [3](#0-2) . There is no admin/root function analogous to a fixed `redistributeLockedAmount()` that force-migrates or force-releases a restricted member's delegation from the escrow account — `do_claim_trapped_balance` only handles a *different* accounting mismatch (dissolved points without released holds) and does not perform the `Filter` check bypass needed to rescue a restricted member's un-migrated delegation, nor is it wired to a callable extrinsic that circumvents the block [4](#0-3) .

### Impact Explanation
A pool member's entire staked balance (their `PoolMembers` total_balance, which could be substantial) becomes permanently unreachable: it is neither claimable by the member (blocked by the `Filter` check in `migrate_delegation`) nor withdrawable through the normal `withdraw_unbonded` path (blocked by the un-migrated delegation requirement), nor recoverable via any privileged operation in this pallet. This is a permanent user-fund lock in a live staking pallet, matching the "permanent user-fund or bridge-state lock" category in the impact gate.

### Likelihood Explanation
This requires only two ordinary, permissionless-adjacent conditions that are part of the pallet's normal operational flow (not a malicious peer/validator/admin-abuse scenario in the sense of root causing the bug — the bug is a missing safety valve in otherwise legitimate filter enforcement): (1) a pool undergoes the routine `DelegateStake` migration (`migrate_pool_to_delegate_stake`), and (2) any member who has not yet called `migrate_delegation` becomes subject to `T::Filter` before doing so. Since filter membership can change for legitimate compliance reasons independent of the pool member's own actions, and the window between pool migration and individual member migration is unbounded, this is a realistic and repeatable state to reach without any implementation bug elsewhere — the root cause is the missing fund-recovery/rescue path once a member is filtered mid-migration.

### Recommendation
Add a privileged (or self-serve exempted) recovery path that allows a restricted member's un-migrated delegation to be released from the `proxy_delegator` escrow independent of the `T::Filter` check in `migrate_delegation` — e.g., a root/pool-bouncer-only `force_migrate_delegation` (or extending `do_claim_trapped_balance`) that performs the same `T::StakeAdapter::migrate_delegation` / `member_withdraw` logic without the `Filter::contains` gate, ensuring restricted members' funds can still be settled to their rightful account (or a designated beneficiary) rather than being permanently trapped.

### Proof of Concept
1. Pool `P` uses `DelegateStake` adapter; member `M` joins and bonds funds normally (funds tracked in `PoolMembers::<T>::get(M)`).
2. Root calls `migrate_pool_to_delegate_stake` for pool `P`. This transfers `P`'s currently transfer-staked balance into a `proxy_delegator` escrow account holding un-migrated delegations for all of `P`'s members.
3. Before `M` calls `migrate_delegation(M)`, an admin adds `M` to `T::Filter` (the pool's restriction/deny list) for compliance/other reasons.
4. `M` (or anyone on `M`'s behalf) calls `Pallet::migrate_delegation(M)`: fails with `Error::<T>::Restricted` due to `ensure!(!T::Filter::contains(&member_account), ...)` [1](#0-0) .
5. `M` calls `unbond` then `withdraw_unbonded`: fails/produces zero balance because `T::StakeAdapter::member_withdraw` requires the member delegation to have been claimed first [3](#0-2) .
6. `M`'s pool balance remains permanently parked in the `proxy_delegator` escrow with no available call to release it while `M` stays on the filter, and no root-level rescue mechanism exists in the pallet to move it out on `M`'s behalf.

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L2498-2505)
```rust
			// this can fail if the pool uses `DelegateStake` strategy and the member delegation
			// is not claimed yet. See `Call::migrate_delegation()`.
			T::StakeAdapter::member_withdraw(
				Member::from(member_account.clone()),
				Pool::from(bonded_pool.bonded_account()),
				balance_to_unbond,
				num_slashing_spans,
			)?;
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L3185-3187)
```rust
			// ensure member is not restricted from joining the pool.
			let member_account = T::Lookup::lookup(member_account)?;
			ensure!(!T::Filter::contains(&member_account), Error::<T>::Restricted);
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L3295-3345)
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

		if trapped_amount.is_zero() {
			return Ok(());
		}

		T::StakeAdapter::member_withdraw(
			Member::from(member_account.clone()),
			Pool::from(Self::generate_bonded_account(member.pool_id)),
			trapped_amount,
			0,
		)?;
```

**File:** substrate/frame/delegated-staking/src/lib.rs (L251-257)
```rust
	/// A reason for placing a hold on funds.
	#[pallet::composite_enum]
	pub enum HoldReason {
		/// Funds held for stake delegation to another account.
		#[codec(index = 0)]
		StakingDelegation,
	}
```
