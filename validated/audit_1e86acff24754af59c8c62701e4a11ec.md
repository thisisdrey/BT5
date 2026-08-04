## Analysis

The external report's core broken invariant: **escrowed value can become permanently stuck because the only path to reconcile it requires a privileged, ad-hoc, one-off intervention rather than a general self-service recovery mechanism reachable by the affected user.**

The closest local analog is in `pallet-nomination-pools`. The pallet documents and even tests for a known trapped-balance condition: [1](#0-0) 

`do_claim_trapped_balance` reconciles a pool member's `PoolMembers` points against the actual delegated/held balance and releases the difference, and its own doc comment states this trapped-balance condition can occur "in rare scenarios" when points are dissolved without releasing the corresponding held funds — the exact CurrentEra/ActiveEra mismatch class documented in [2](#0-1) .

Critically, `do_claim_trapped_balance` is **not exposed as a public dispatchable**. It is only invoked internally by a one-time `OnRuntimeUpgrade` migration that hardcodes a single specific beneficiary account via a `Get<T::AccountId>` type parameter: [3](#0-2) 

and in the runtime, the affected account is hardcoded as a constant to be manually wired into the migration: [4](#0-3) 

There is no `#[pallet::call]` entry point that lets an arbitrary affected pool member self-serve their own trapped balance. The only other invocation site is a manual, offline, snapshot-driven remote test used to *discover* affected members after the fact: [5](#0-4) 

This reproduces the same structural flaw as the report: a value-holding mechanism (pool member points vs. actual held/delegated stake) has a known edge case that can desynchronize and trap user funds, but the fix path is entirely governance/operator-driven (a hardcoded, per-account, one-time runtime upgrade) rather than a permissionless recovery call. Any pool member who hits this desync in the future has no way to reclaim their own funds without a chain-specific governance-approved runtime upgrade naming their account — an arbitrarily long, indefinite lock that depends entirely on someone noticing and acting on their behalf.

### Title
Trapped pool-member balance has no permissionless recovery path; only reachable via governance-gated, hardcoded-beneficiary runtime migration - (File: substrate/frame/nomination-pools/src/migration.rs)

### Summary
`pallet-nomination-pools` can desynchronize a member's recorded points from their actual held/delegated stake balance (as already observed and admitted in `pr_11018`/`ClaimTrappedBalance`), permanently trapping the difference. The only recovery mechanism, `do_claim_trapped_balance`, is not exposed to affected users; it is only reachable through a one-time `OnRuntimeUpgrade` migration hardcoded to a single account chosen by the runtime maintainers ahead of time.

### Finding Description
`Pallet::do_claim_trapped_balance` computes `trapped_amount = actual_balance.saturating_sub(expected_balance)` where `expected_balance` comes from `PoolMembers` points bookkeeping and `actual_balance` comes from the stake adapter's delegated balance [6](#0-5) . This function is the sole reconciliation path for the exact bug class already documented in the changelog: "points were dissolved but the held funds weren't released" [7](#0-6) .

However, this function is wired up only via `ClaimTrappedBalance<T, A>`, a migration parameterized by a compile-time `Get<T::AccountId>` for a single, specific, already-known-affected account [8](#0-7) , and by an offline/ignored remote test that requires a state snapshot and manual execution to enumerate affected members [5](#0-4) . There is no `#[pallet::call]` dispatchable that lets any pool member call `do_claim_trapped_balance` for themselves.

Consequently, if the same desync recurs for a different member (or on a different chain), that member has no on-chain, permissionless way to reclaim their own funds. Recovery depends entirely on: (1) someone off-chain noticing the discrepancy (via manual snapshot analysis), (2) governance approving a new runtime upgrade, and (3) that upgrade hardcoding the specific affected account. This mirrors the reported bug class precisely — the affected party (a borrower/depositor with legitimately-owed funds) is left with no self-service exit once the pool/pallet enters the broken state, and is fully dependent on a privileged, ad hoc fix.

### Impact Explanation
Medium: user funds (staked/delegated balances legitimately owed to pool members) can become indefinitely locked with no on-chain recovery path available to the affected account holder. This is a direct fund-lock condition consistent with "permanent user-fund lock" in the impact gate. The impact is capped at Medium because triggering the underlying desync itself requires the pre-existing (separately tracked) accounting bug rather than a new attacker-controlled trigger; the finding here is that the *fix* mechanism is not general/self-service.

### Likelihood Explanation
Medium: the desync condition has already occurred at least once in production (per `pr_11018` and the hardcoded `TrappedBalanceMember` constant), demonstrating the underlying accounting bug is not purely theoretical. Given no self-service recovery exists, any recurrence (e.g., from a similar era-based accounting edge case elsewhere in slashing/unbonding paths) will again require a bespoke governance migration, with affected users being locked out for however long it takes to notice and act.

### Recommendation
Expose `do_claim_trapped_balance` as a permissionless, non-privileged `#[pallet::call]` dispatchable (e.g., `claim_trapped_balance(origin, member_account)`), analogous to how `withdraw_unbonded` and `apply_slash` are already permissionless, so any pool member (or anyone on their behalf) can reconcile and release trapped balance without waiting on a governance-approved, account-specific runtime migration.

### Proof of Concept
1. A pool member's points become desynchronized from their actual delegated stake balance (as already documented to have occurred, via an era-accounting mismatch in unbonding/slashing bookkeeping).
2. `PoolMembers::<T>::get(member).total_balance()` now returns less than `T::StakeAdapter::member_delegation_balance(...)` for that member.
3. The member calls existing dispatchables (`unbond`, `withdraw_unbonded`) but these only interact with their `PoolMembers` points-based bookkeeping, not the raw delegation excess; the excess balance is not returned to them.
4. There is no dispatchable calling `do_claim_trapped_balance` [9](#0-8) , so the member cannot self-serve.
5. The funds remain trapped until node operators/governance identify the affected account (as done manually for `TrappedBalanceMember` in asset-hub-westend [4](#0-3) ) and ship a dedicated runtime upgrade naming that account.

### Citations

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

**File:** substrate/frame/nomination-pools/src/migration.rs (L241-262)
```rust
	/// One-time migration to claim trapped balance for a specific pool member.
	///
	/// Generic over `T: Config` and `A: Get<T::AccountId>` where `A` provides the account
	/// of the affected member. If `A` does not have trapped balance, this is a no-op.
	pub struct ClaimTrappedBalance<T, A>(core::marker::PhantomData<(T, A)>);

	impl<T: Config, A: Get<T::AccountId>> OnRuntimeUpgrade for ClaimTrappedBalance<T, A> {
		fn on_runtime_upgrade() -> Weight {
			let member_account = A::get();
			match Pallet::<T>::do_claim_trapped_balance(&member_account) {
				Ok(()) => {
					log!(info, "Successfully claimed trapped balance for {:?}", member_account);
				},
				Err(e) => {
					log!(info, "No trapped balance to claim for {:?}: {:?}", member_account, e);
				},
			}

			// Worst case: slash applied + trapped balance withdrawn.
			T::WeightInfo::apply_slash()
				.saturating_add(T::WeightInfo::withdraw_unbonded_update(T::MaxUnbonding::get()))
		}
```

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/src/lib.rs (L1806-1813)
```rust
parameter_types! {
	// Account `15jAYzPdLorBGAj4LLGaqohpzpw4mEohVkzszNpaBPbnDaXn` (Nomination Pool #296)
	// has trapped funds on PAH. On WAH this will be a no-op (member won't exist), but
	// leaving here as a reference when we add to PAH. To be skipped on KAH.
	pub TrappedBalanceMember: AccountId = AccountId::from(
		hex_literal::hex!("d11964e74f0571827c231ee07fc7268fc835499db3a0089c9e6f02c2435f50fc")
	);
}
```

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/tests/tests.rs (L2505-2522)
```rust
	/// Test claim_trapped_balance for all pool members using a state snapshot.
	///
	/// The test iterates through all pool members, computes trapped amounts, and calls
	/// `do_claim_trapped_balance` for those with trapped funds. Only successful claims are printed.
	///
	/// Run with:
	/// ```bash
	/// SNAP=<PATH_TO_SNAP> cargo test -r -p asset-hub-westend-runtime np_claim_trapped_balance \
	/// -- --ignored --nocapture
	/// ```
	///
	/// Note: If you want to test this with PAH snapshot, ensure (locally, DO NOT COMMIT)
	/// 1) WAH staking pallet indices align with PAH
	/// 2) WAH ED is same as PAH (decrease it by 10x in `../../../constants/src/westend.rs`)
	/// 3) Staking Bonding Duration is 28 eras.
	#[tokio::test]
	#[ignore]
	async fn np_claim_trapped_balance() {
```
