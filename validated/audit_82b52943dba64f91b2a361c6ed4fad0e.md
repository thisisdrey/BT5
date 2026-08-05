Audit Report

## Title
Restricted/blacklisted accounts can still receive staking reward payouts via `payee`, bypassing the staking participation `Filter` - (File: `substrate/frame/staking/src/pallet/mod.rs`, `substrate/frame/staking/src/pallet/impls.rs`)

## Summary
`pallet-staking`'s `T::Filter: Contains<AccountId>` check, intended to blacklist accounts from participating in staking (e.g. accounts already staking via `pallet-nomination-pools`), is enforced only on the `stash`/`controller` origin in `bond`, `bond_extra`, and the `StakingInterface::set_payee` implementation, never on the `payee`/reward-destination account [1](#0-0) . Any unrestricted account can therefore name a restricted account as `RewardDestination::Account` and route staking reward payouts to it indefinitely.

## Finding Description
`Pallet::<T>::bond` only checks the filter against the signer (`stash`), then binds an arbitrary `payee: RewardDestination<T::AccountId>` to the ledger without any filter check on that account [2](#0-1) . Likewise, `set_payee` retrieves the ledger by controller and sets the new `payee` with no call to `T::Filter::contains` on the destination account [3](#0-2) . The `Config::Filter` documentation confirms its intended purpose is to blacklist accounts participating in staking "another way (such as pools)" [4](#0-3) , but the implementation only gates the stash side of `bond`/`bond_extra`, leaving the reward-destination account entirely unchecked. This is a genuine asymmetry between the two sides of the same extrinsic parameters (stash vs. payee), matching the exact "receiver isn't checked while caller is" pattern described in the claim.

## Impact Explanation
This compromises the intended runtime invariant that restricted accounts cannot obtain staking-derived economic value, since an unrestricted account can always designate a restricted account as `payee` and continuously funnel reward payouts to it every era, defeating the purpose of `T::Filter`. This falls under "runtime bugs that compromise intended behavior" in the impact gate, affecting the correctness of staking reward payout beneficiary logic (the payout beneficiary is not validated against the same restriction rule that governs staking participation eligibility).

## Likelihood Explanation
This requires no privileged role, no compromised relayer/validator, and no off-repo assumptions. Any ordinary signed account can call the public `bond` or `set_payee` extrinsics and name any restricted account as `payee`, making the issue trivially and repeatedly reachable by any unprivileged user.

## Recommendation
Extend the `T::Filter::contains` check to also validate the `payee` account (when it resolves to `RewardDestination::Account`) in `bond`, `set_payee`, and the `StakingInterface::set_payee`/`bond` trait implementations, rejecting the call with `Error::<T>::Restricted` if the destination is restricted — mirroring the existing stash-side check.

## Proof of Concept
1. Configure `Filter = MockedRestrictList` and mark account `B` as restricted via `restrict(&bob)` in the mock [5](#0-4) .
2. Unrestricted account `A` calls `Staking::bond(origin_A, value, RewardDestination::Account(B))`; this succeeds because only `T::Filter::contains(&A)` is checked, not `B` — verified in `bond` at [6](#0-5) .
3. Confirm `B` itself cannot bond directly (`Error::<T>::Restricted`), as shown by the existing test `restricted_accounts_can_only_withdraw`, yet `B`'s free balance still receives era reward payouts credited via `A`'s `payee` setting.
4. `A` can additionally call `Staking::set_payee(origin_A, RewardDestination::Account(B))` at any time to redirect rewards to `B` with no filter check on `B`, per [7](#0-6) .

### Citations

**File:** substrate/frame/staking/src/pallet/mod.rs (L319-324)
```rust
		#[pallet::no_default_bounds]
		/// Filter some accounts from participating in staking.
		///
		/// This is useful for example to blacklist an account that is participating in staking in
		/// another way (such as pools).
		type Filter: Contains<Self::AccountId>;
```

**File:** substrate/frame/staking/src/pallet/mod.rs (L1180-1212)
```rust
		#[pallet::call_index(0)]
		#[pallet::weight(T::WeightInfo::bond())]
		pub fn bond(
			origin: OriginFor<T>,
			#[pallet::compact] value: BalanceOf<T>,
			payee: RewardDestination<T::AccountId>,
		) -> DispatchResult {
			let stash = ensure_signed(origin)?;

			ensure!(!T::Filter::contains(&stash), Error::<T>::Restricted);

			if StakingLedger::<T>::is_bonded(StakingAccount::Stash(stash.clone())) {
				return Err(Error::<T>::AlreadyBonded.into());
			}

			// An existing controller cannot become a stash.
			if StakingLedger::<T>::is_bonded(StakingAccount::Controller(stash.clone())) {
				return Err(Error::<T>::AlreadyPaired.into());
			}

			// Reject a bond which is considered to be _dust_.
			if value < asset::existential_deposit::<T>() {
				return Err(Error::<T>::InsufficientBond.into());
			}

			let stash_balance = asset::free_to_stake::<T>(&stash);
			let value = value.min(stash_balance);
			Self::deposit_event(Event::<T>::Bonded { stash: stash.clone(), amount: value });
			let ledger = StakingLedger::<T>::new(stash.clone(), value);

			// You're auto-bonded forever, here. We might improve this by only bonding when
			// you actually validate/nominate and remove once you unbond __everything__.
			ledger.bond(payee)?;
```

**File:** substrate/frame/staking/src/pallet/mod.rs (L1467-1489)
```rust
		#[pallet::call_index(7)]
		#[pallet::weight(T::WeightInfo::set_payee())]
		pub fn set_payee(
			origin: OriginFor<T>,
			payee: RewardDestination<T::AccountId>,
		) -> DispatchResult {
			let controller = ensure_signed(origin)?;
			let ledger = Self::ledger(Controller(controller.clone()))?;

			ensure!(
				(payee != {
					#[allow(deprecated)]
					RewardDestination::Controller
				}),
				Error::<T>::ControllerDeprecated
			);

			ledger
				.set_payee(payee)
				.defensive_proof("ledger was retrieved from storage, thus it's bonded; qed.")?;

			Ok(())
		}
```

**File:** substrate/frame/staking/src/mock.rs (L276-281)
```rust
pub struct MockedRestrictList;
impl Contains<AccountId> for MockedRestrictList {
	fn contains(who: &AccountId) -> bool {
		RestrictedAccounts::get().contains(who)
	}
}
```
