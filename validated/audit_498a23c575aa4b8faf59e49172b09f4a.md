Audit Report

## Title
Unprivileged front-running of purchase payouts via public `vested_transfer` permanently locks user's crowdsale funds - (File: `polkadot/runtime/common/src/purchase/mod.rs`)

## Summary
The `pallet_purchase::payout` extrinsic requires that the target account have no existing vesting schedule before it will transfer funds and apply a new vesting schedule for the locked portion of a DOT purchase. Since `pallet_vesting::vested_transfer` is a public, unprivileged, signed extrinsic callable against any target account, an attacker can pre-emptively create a minimal vesting schedule on a known purchase participant's address, causing every subsequent `payout` call against that account to permanently fail with `Error::VestingScheduleExists`.

## Finding Description
`payout` in `polkadot/runtime/common/src/purchase/mod.rs` enforces a hard precondition that the target account has no vesting schedule: [1](#0-0) 

This check relies on `T::VestingSchedule::vesting_balance(&who)`, which reads pallet-vesting storage — storage that is globally mutable by any signed account via the public `vested_transfer` extrinsic: [2](#0-1) 

`do_vested_transfer` performs the transfer and adds a vesting schedule to an arbitrary `target` with no relation to, or awareness of, the purchase pallet's assumptions, and only validates `MinVestedTransfer` and schedule well-formedness: [3](#0-2) 

Because `Accounts` storage in the purchase pallet is on-chain state populated by `create_account`/`update_validity_status`, the addresses of validated purchasers are publicly discoverable. An attacker can call `vested_transfer` with a minimal schedule against any such known address before `PaymentAccount` calls `payout`. Once a vesting schedule exists — regardless of who created it — `payout` deterministically and permanently fails at the `ensure!(T::VestingSchedule::vesting_balance(&who).is_none(), ...)` check, since nothing in the purchase pallet clears or bypasses an externally injected vesting schedule. Recovery would require a root-only call (`force_remove_vesting_schedule`), which is outside the normal purchase flow and unavailable to the `PaymentAccount` operator or the victim themselves.

## Impact Explanation
This matches the "permanent user-fund lock" category in the impact gate: an unprivileged, non-governance attacker can grief any known purchase participant, indefinitely preventing their already-approved DOT purchase (`free_balance` + `locked_balance` in `AccountStatus`) from being paid out via the normal `payout` path. Since purchase settlement is presumably processed by the `PaymentAccount` for many accounts, this griefing can be applied at low cost across many known purchaser addresses, degrading or stalling the payout process until an out-of-band, root-privileged remediation occurs.

## Likelihood Explanation
Likelihood is high: `vested_transfer` is fully public and unprivileged, target addresses are discoverable via on-chain `Accounts` storage, and the attack only requires front-running the eventual `payout` call with a minimal-value vesting transfer (bounded by `MinVestedTransfer` and tx fees). No validator, collator, relayer, or governance capability is needed.

## Recommendation
Do not treat "no vesting schedule" as an unconditional hard precondition for `payout`. Options:
- Track whether the purchase-pallet-specific vesting application has already occurred via pallet-purchase's own storage flag, independent of the externally-mutable global vesting balance check.
- Still transfer the free-balance portion and defer/lock only the vested portion if an external vesting schedule already exists, rather than failing the entire extrinsic.
- Provide a non-root path to merge/supersede an externally-created vesting schedule that was not created by the purchase pallet.

## Proof of Concept
1. `ValidityOrigin` creates and validates purchaser `Alice` (`create_account`, `update_validity_status` to `ValidHigh`), then `update_balance` sets her `locked_balance` > 0.
2. Before `PaymentAccount` calls `payout(Alice)`, attacker `Mallory` calls `pallet_vesting::vested_transfer(target = Alice, schedule = { locked: MinVestedTransfer, per_block: 1, starting_block: current })`, which succeeds unconditionally per `do_vested_transfer`.
3. `PaymentAccount` calls `Purchase::payout(Alice)`; the check at `polkadot/runtime/common/src/purchase/mod.rs` lines 312-316 now fails with `Error::<T>::VestingScheduleExists`, as already demonstrated for legitimate pre-existing schedules in `polkadot/runtime/common/src/purchase/tests.rs` lines 471-478 (`assert_ok!(<Test as Config>::VestingSchedule::add_vesting_schedule(&bob(), 100, 1, 50)); assert_noop!(Purchase::payout(...), Error::<Test>::VestingScheduleExists)`).
4. Every future retry of `payout(Alice)` fails identically until a root-only `force_remove_vesting_schedule` is executed against the injected schedule.

### Citations

**File:** polkadot/runtime/common/src/purchase/mod.rs (L306-317)
```rust
		pub fn payout(origin: OriginFor<T>, who: T::AccountId) -> DispatchResult {
			// Payments must be made directly by the `PaymentAccount`.
			let payment_account = ensure_signed(origin)?;
			let test_against = PaymentAccount::<T>::get().ok_or(DispatchError::BadOrigin)?;
			ensure!(payment_account == test_against, DispatchError::BadOrigin);

			// Account should not have a vesting schedule.
			ensure!(
				T::VestingSchedule::vesting_balance(&who).is_none(),
				Error::<T>::VestingScheduleExists
			);

```

**File:** substrate/frame/vesting/src/lib.rs (L368-380)
```rust
		#[pallet::call_index(2)]
		#[pallet::weight(
			T::WeightInfo::vested_transfer(MaxLocksOf::<T>::get(), T::MAX_VESTING_SCHEDULES)
		)]
		pub fn vested_transfer(
			origin: OriginFor<T>,
			target: AccountIdLookupOf<T>,
			schedule: VestingInfo<BalanceOf<T>, BlockNumberFor<T>>,
		) -> DispatchResult {
			let transactor = ensure_signed(origin)?;
			let target = T::Lookup::lookup(target)?;
			Self::do_vested_transfer(&transactor, &target, schedule)
		}
```

**File:** substrate/frame/vesting/src/lib.rs (L552-586)
```rust
	// Execute a vested transfer from `source` to `target` with the given `schedule`.
	fn do_vested_transfer(
		source: &T::AccountId,
		target: &T::AccountId,
		schedule: VestingInfo<BalanceOf<T>, BlockNumberFor<T>>,
	) -> DispatchResult {
		// Validate user inputs.
		ensure!(schedule.locked() >= T::MinVestedTransfer::get(), Error::<T>::AmountLow);
		if !schedule.is_valid() {
			return Err(Error::<T>::InvalidScheduleParams.into());
		};

		// Check we can add to this account prior to any storage writes.
		Self::can_add_vesting_schedule(
			target,
			schedule.locked(),
			schedule.per_block(),
			schedule.starting_block(),
		)?;

		T::Currency::transfer(source, target, schedule.locked(), ExistenceRequirement::AllowDeath)?;

		// We can't let this fail because the currency transfer has already happened.
		// Must be successful as it has been checked before.
		// Better to return error on failure anyway.
		let res = Self::add_vesting_schedule(
			target,
			schedule.locked(),
			schedule.per_block(),
			schedule.starting_block(),
		);
		debug_assert!(res.is_ok(), "Failed to add a schedule when we had to succeed.");

		Ok(())
	}
```
