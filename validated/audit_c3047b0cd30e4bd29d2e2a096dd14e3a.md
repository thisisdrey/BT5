All the cited code exactly matches the actual repository content, confirming the claim's technical accuracy: phase 1 (`step_1_code_upload`) records `NativeDepositOf` entitlements for code-upload deposits, but phase 2 (`step_2_contract`) calls `migrate_native_to_pgas` for per-contract storage deposits without any corresponding `NativeDepositOf` update. This means pre-migration native depositors have zero recorded entitlement, so `refund_on_hold`/`refund_all` route the full amount through `settle_pgas_refund`, which burns `(1 - RefundPercent)` of it.

Audit Report

## Title
Pre-PGAS native storage deposits are burned instead of fully refunded after the v4 native→PGAS migration - (File: `substrate/frame/revive/src/migrations/v4.rs` / `substrate/frame/revive/src/deposit_payment.rs`)

## Summary
The `pallet-revive` v4 migration's phase 2 (`step_2_contract`) converts a contract's native `StorageDepositReserve` hold into PGAS via `migrate_native_to_pgas`, but never records a `NativeDepositOf` entitlement for the depositor, unlike phase 1 (`step_1_code_upload`) which does so for code-upload deposits. As a result, subsequent refunds via `PGasDeposit::refund_on_hold`/`refund_all` treat the migrated balance as ordinary PGAS subject to `RefundPercent`, permanently burning the non-refunded portion of what was originally a 100%-repayable native deposit.

## Finding Description
`PGasDeposit::charge_and_hold` [1](#0-0)  is the only site that populates `NativeDepositOf`, via `record_native_deposit`, and only for charges made after the runtime adopts the PGAS backend. Deposits paid under the pre-migration `()` native-only backend never touch this map, since it is introduced by migration v4 itself. Phase 1 backfills it only for code-upload deposits via `CodeInfoOf` iteration: [2](#0-1) . Phase 2 migrates every contract's storage-deposit hold to PGAS with no per-contributor breakdown and no `NativeDepositOf` update: [3](#0-2) , backed by `migrate_native_to_pgas` which purely burns the native hold and mints/holds the equivalent PGAS: [4](#0-3) .

After migration, `refund_on_hold` computes `contribution = NativeDepositOf::<T>::get(from, to)`, which is `0` for pre-migration depositors, so the entire refund is routed through `settle_pgas_refund`: [5](#0-4) , and similarly `refund_all` at contract termination: [6](#0-5) . `settle_pgas_refund` returns only `RefundPercent` of the amount to the recipient and unconditionally burns the remainder: [7](#0-6) . `RefundPercent` is designed to prevent users from harvesting PGAS they never earned, but this logic incorrectly applies to users who never interacted with PGAS at all — their principal was paid entirely in native currency before the PGAS backend existed. The test `phase_two_burns_native_and_mints_pgas_on_contracts` confirms phase 2's pooled, non-attributed migration behavior with no `NativeDepositOf` entries created: [8](#0-7) , and `mixed_native_pgas_refund_caps_pgas_without_reverting` demonstrates the 10% `RefundPercent` haircut applied to PGAS-routed refunds with no native entitlement: [9](#0-8) .

## Impact Explanation
This is unconditional, permanent loss of user principal — `(1 - RefundPercent)` of any pre-migration native storage deposit is burned the first time that contract's deposit is refunded post-migration, whether via partial storage-clearing refunds or full refunds at contract termination (`do_terminate`). This violates the settlement invariant that contract-held value must conserve and settle exactly once to the rightful beneficiary and amount. The loss is triggered by ordinary, permissionless contract lifecycle operations (clearing storage, contract self-destruct) rather than by any privileged or adversarial action, and the burned funds are unrecoverable.

## Likelihood Explanation
Any chain running `pallet-revive` with pre-existing contracts under the native-only `()` deposit backend that upgrades to the `PGasDeposit` backend and runs the v4 migration will trigger this loss for every affected contract as soon as any refund event occurs. Both storage-clearing (routine contract execution) and contract termination are common, unprivileged operations performed by ordinary users/contracts, making this a highly reachable and repeatable data-modeling defect rather than an edge case.

## Recommendation
During phase 2 (`step_2_contract`), either (a) attribute the migrated native hold to a `NativeDepositOf` entitlement (analogous to phase 1's handling of `CodeInfoOf`) if per-contributor data is available, or (b) if deposits are pooled from multiple past contributors and cannot be individually attributed, preserve the native hold as native currency rather than converting it into PGAS subject to `RefundPercent` burning, so that `refund_on_hold`/`refund_all` do not apply the punitive haircut to previously fully-refundable native principal.

## Proof of Concept
1. Deploy `pallet-revive` under the native-only `()` backend; user `U` triggers a `1_000`-unit native storage-deposit charge on contract `C` under `HoldReason::StorageDepositReserve`, with no `NativeDepositOf` entry (matches migration test setup in `seed_contract`: [10](#0-9) ).
2. Runtime adopts `PGasDeposit`; v4 migration's phase 2 (`step_2_contract`) calls `migrate_native_to_pgas`, burning `C`'s `1_000` native hold and re-holding `1_000` PGAS, with no `NativeDepositOf` entry created for `(C, U)`.
3. `U` clears storage or the contract terminates, invoking `refund_on_hold`/`refund_all`; `NativeDepositOf::get(C, U) == 0`, so the full `1_000` routes through `settle_pgas_refund`, which returns only `RefundPercent * 1_000` (e.g., `100` at a 10% `RefundPercent`) to `U` and burns the remaining `900`, matching the behavior demonstrated in `mixed_native_pgas_refund_caps_pgas_without_reverting`.

### Citations

**File:** substrate/frame/revive/src/deposit_payment.rs (L358-372)
```rust
		if Self::pgas_reducible_balance(from) >= amount {
			<Holder as fungibles::MutateHold<T::AccountId>>::transfer_and_hold(
				Id::get(),
				&reason.into(),
				from,
				to,
				amount,
				Precision::Exact,
				Preservation::Expendable,
				Fortitude::Polite,
			)?;
		} else {
			<() as Deposit<T>>::charge_and_hold(reason, src, to, amount)?;
			Self::record_native_deposit(from, to, amount);
		}
```

**File:** substrate/frame/revive/src/deposit_payment.rs (L384-412)
```rust
	fn refund_on_hold(
		reason: HoldReason,
		from: &T::AccountId,
		dst: Funds<T::AccountId>,
		amount: BalanceOf<T>,
	) -> DispatchResult {
		let to = match &dst {
			Funds::Balance(to) | Funds::TxFee(to) => *to,
		};
		let contribution = NativeDepositOf::<T>::get(from, to);
		let native_requested = amount.min(contribution);

		let native_refunded = if !native_requested.is_zero() {
			<() as Deposit<T>>::refund_on_hold(reason, from, dst, native_requested)?;
			let new_val = contribution.saturating_sub(native_requested);
			if new_val.is_zero() {
				NativeDepositOf::<T>::remove(from, to);
			} else {
				NativeDepositOf::<T>::insert(from, to, new_val);
			}
			native_requested
		} else {
			BalanceOf::<T>::zero()
		};

		let pgas_needed = amount.saturating_sub(native_refunded);
		Self::settle_pgas_refund(reason, from, to, pgas_needed)?;
		Ok(())
	}
```

**File:** substrate/frame/revive/src/deposit_payment.rs (L427-440)
```rust
	fn refund_all(
		from: &T::AccountId,
		dst: Funds<T::AccountId>,
	) -> Result<BalanceOf<T>, DispatchError> {
		let to = match &dst {
			Funds::Balance(to) | Funds::TxFee(to) => *to,
		};
		let native = <() as Deposit<T>>::refund_all(from, dst)?;
		let reason = HoldReason::StorageDepositReserve;

		let pgas = Self::pgas_on_hold(reason, from);
		let pgas = Self::settle_pgas_refund(reason, from, to, pgas)?;
		Ok(native.saturating_add(pgas))
	}
```

**File:** substrate/frame/revive/src/deposit_payment.rs (L445-516)
```rust
	fn migrate_native_to_pgas(
		reason: HoldReason,
		contract: &T::AccountId,
		amount: BalanceOf<T>,
	) -> DispatchResult {
		let pgas_ed = <Mutator as fungibles::Inspect<T::AccountId>>::minimum_balance(Id::get());
		let freeze_id = FreezeReason::PGasMinBalance.into();
		if <Freezer as fungibles::freeze::Inspect<T::AccountId>>::balance_frozen(
			Id::get(),
			&freeze_id,
			contract,
		) < pgas_ed
		{
			if <Mutator as fungibles::Inspect<T::AccountId>>::balance(Id::get(), contract) < pgas_ed
			{
				<Mutator as fungibles::Mutate<T::AccountId>>::mint_into(
					Id::get(),
					contract,
					pgas_ed,
				)
				.inspect_err(|err| {
					log::debug!(
						target: LOG_TARGET,
						"Failed to mint PGAS ED for contract: {err:?}",
					)
				})?;
			}
			<Freezer as fungibles::freeze::Mutate<T::AccountId>>::set_freeze(
				Id::get(),
				&freeze_id,
				contract,
				pgas_ed,
			)
			.inspect_err(|err| {
				log::debug!(
					target: LOG_TARGET,
					"Failed to freeze PGAS ED for contract: {err:?}",
				)
			})?;
		}

		if amount.is_zero() {
			return Ok(());
		}

		T::Currency::burn_held(
			&reason.into(),
			contract,
			amount,
			Precision::Exact,
			Fortitude::Polite,
		)
		.inspect_err(
			|err| log::debug!(target: LOG_TARGET, "Failed to burn held amount {amount:?}: {err:?}"),
		)?;

		<Mutator as fungibles::Mutate<T::AccountId>>::mint_into(Id::get(), contract, amount)
			.inspect_err(
				|err| log::debug!(target: LOG_TARGET, "Failed to mint to {contract:?} amount: {amount:?}: {err:?}"),
			)?;

		<Holder as fungibles::MutateHold<T::AccountId>>::hold(
			Id::get(),
			&reason.into(),
			contract,
			amount,
		)
		.inspect_err(
			|err| log::debug!(target: LOG_TARGET, "Failed to hold amount in {contract:?}: {amount:?}: {err:?}"),
		)?;
		Ok(())
	}
```

**File:** substrate/frame/revive/src/deposit_payment.rs (L575-633)
```rust
	fn settle_pgas_refund(
		reason: HoldReason,
		from: &T::AccountId,
		to: &T::AccountId,
		amount: BalanceOf<T>,
	) -> Result<BalanceOf<T>, DispatchError> {
		if amount.is_zero() {
			return Ok(BalanceOf::<T>::zero());
		}
		// Cap the amount we settle at what's actually held in PGAS. A refund recipient with
		// no `NativeDepositOf` credit on a contract whose deposit was paid in native would
		// otherwise route the full amount through PGAS and revert on `Precision::Exact`.
		let pgas_held = Self::pgas_on_hold(reason, from);
		let amount = amount.min(pgas_held);
		if amount.is_zero() {
			return Ok(BalanceOf::<T>::zero());
		}
		let refund = RefundPercent::get().mul_floor(amount);
		let mut burn = amount.saturating_sub(refund);
		let mut refunded = BalanceOf::<T>::zero();

		if !refund.is_zero() {
			let can_credit = matches!(
				<Mutator as fungibles::Inspect<T::AccountId>>::can_deposit(
					Id::get(),
					to,
					refund,
					Provenance::Extant,
				),
				DepositConsequence::Success
			);
			if can_credit {
				refunded = <Holder as fungibles::MutateHold<T::AccountId>>::transfer_on_hold(
					Id::get(),
					&reason.into(),
					from,
					to,
					refund,
					Precision::BestEffort,
					Restriction::Free,
					Fortitude::Polite,
				)?;
			} else {
				burn = burn.saturating_add(refund);
			}
		}

		if !burn.is_zero() {
			<Holder as fungibles::MutateHold<T::AccountId>>::burn_held(
				Id::get(),
				&reason.into(),
				from,
				burn,
				Precision::Exact,
				Fortitude::Polite,
			)?;
		}
		Ok(refunded)
	}
```

**File:** substrate/frame/revive/src/migrations/v4.rs (L236-251)
```rust
	/// Phase 1: credit the next `CodeInfoOf` entry's owner in [`NativeDepositOf`]. Returns
	/// `Some(Cursor::Contract(None))` when phase 1 is exhausted.
	fn step_1_code_upload(last: Option<H256>) -> Option<Cursor> {
		let mut iter = match last {
			Some(last) => CodeInfoOf::<T>::iter_from(CodeInfoOf::<T>::hashed_key_for(last)),
			None => CodeInfoOf::<T>::iter(),
		};

		let Some((hash, info)) = iter.next() else { return Some(Cursor::Contract(None)) };

		let pallet_account = Pallet::<T>::account_id();
		NativeDepositOf::<T>::mutate(&pallet_account, info.owner(), |entitlement| {
			*entitlement = entitlement.saturating_add(info.deposit());
		});
		Some(Cursor::CodeUpload(hash))
	}
```

**File:** substrate/frame/revive/src/migrations/v4.rs (L253-280)
```rust
	/// Phase 2: hand the next contract to [`Deposit::migrate_native_to_pgas`]. EOAs are
	/// skipped but still advance the cursor.
	fn step_2_contract(last: Option<H160>) -> Option<H160> {
		use frame_support::traits::fungible::InspectHold;

		let mut iter = match last {
			Some(last) => AccountInfoOf::<T>::iter_from(AccountInfoOf::<T>::hashed_key_for(last)),
			None => AccountInfoOf::<T>::iter(),
		};

		let (addr, info) = iter.next()?;
		if matches!(info.account_type, AccountType::Contract(_)) {
			let contract = T::AddressMapper::to_account_id(&addr);
			let held =
				T::Currency::balance_on_hold(&HoldReason::StorageDepositReserve.into(), &contract);
			if let Err(err) = T::Deposit::migrate_native_to_pgas(
				HoldReason::StorageDepositReserve,
				&contract,
				held,
			) {
				log::error!(
					target: LOG_TARGET,
					"v4: failed to migrate native -> PGAS deposit for contract {addr:?}: {err:?}",
				);
			}
		}
		Some(addr)
	}
```

**File:** substrate/frame/revive/src/migrations/v4.rs (L345-362)
```rust
	fn seed_contract(address: H160, code_hash: H256, storage_deposit: u128) {
		let contract_account = <Test as Config>::AddressMapper::to_account_id(&address);
		let info = ContractInfo::<Test>::new(&address, 0u32.into(), code_hash).unwrap();
		AccountInfoOf::<Test>::insert(
			address,
			AccountInfo::<Test> { account_type: AccountType::Contract(info), dust: 0 },
		);

		let ed = <Test as Config>::Currency::minimum_balance();
		<Test as Config>::Currency::mint_into(&contract_account, ed).unwrap();
		<Test as Config>::Currency::mint_into(&contract_account, storage_deposit).unwrap();
		<Test as Config>::Currency::hold(
			&HoldReason::StorageDepositReserve.into(),
			&contract_account,
			storage_deposit,
		)
		.unwrap();
	}
```

**File:** substrate/frame/revive/src/migrations/v4.rs (L397-477)
```rust
	#[test]
	fn phase_two_burns_native_and_mints_pgas_on_contracts() {
		ExtBuilder::default().genesis_config(None).build().execute_with(|| {
			let owner = AccountId32::new([1; 32]);
			let hash = H256::repeat_byte(0xCC);
			seed_code_upload(hash, owner.clone(), 0);

			let c1 = H160::repeat_byte(0x10);
			let c2 = H160::repeat_byte(0x20);
			seed_contract(c1, hash, 700);
			seed_contract(c2, hash, 1_300);

			let c1_acc = <Test as Config>::AddressMapper::to_account_id(&c1);
			let c2_acc = <Test as Config>::AddressMapper::to_account_id(&c2);

			let total_issuance_before = <Test as Config>::Currency::total_issuance();

			V4::run_to_completion();

			assert_eq!(
				<Test as Config>::Currency::balance_on_hold(
					&HoldReason::StorageDepositReserve.into(),
					&c1_acc,
				),
				0,
			);
			assert_eq!(
				<Test as Config>::Currency::balance_on_hold(
					&HoldReason::StorageDepositReserve.into(),
					&c2_acc,
				),
				0,
			);

			assert_eq!(
				total_issuance_before - <Test as Config>::Currency::total_issuance(),
				700 + 1_300,
			);

			use frame_support::traits::tokens::fungibles::{Inspect, InspectHold};
			let pgas_ed = Assets::minimum_balance(PGasAssetId::get());
			assert_eq!(
				AssetsHolder::balance_on_hold(
					PGasAssetId::get(),
					&HoldReason::StorageDepositReserve.into(),
					&c1_acc,
				),
				700,
			);
			assert_eq!(
				AssetsHolder::balance_on_hold(
					PGasAssetId::get(),
					&HoldReason::StorageDepositReserve.into(),
					&c2_acc,
				),
				1_300,
			);
			// Each migrated contract also gets the PGAS ED minted into its free balance and
			// frozen under `FreezeReason::PGasMinBalance`, matching the post-`init_contract`
			// invariant.
			assert_eq!(Assets::balance(PGasAssetId::get(), &c1_acc), pgas_ed);
			assert_eq!(Assets::balance(PGasAssetId::get(), &c2_acc), pgas_ed);
			use frame_support::traits::tokens::fungibles::InspectFreeze;
			assert_eq!(
				AssetsFreezer::balance_frozen(
					PGasAssetId::get(),
					&FreezeReason::PGasMinBalance.into(),
					&c1_acc,
				),
				pgas_ed,
			);
			assert_eq!(
				AssetsFreezer::balance_frozen(
					PGasAssetId::get(),
					&FreezeReason::PGasMinBalance.into(),
					&c2_acc,
				),
				pgas_ed,
			);
		});
	}
```

**File:** substrate/frame/revive/src/tests/deposit_payment.rs (L616-673)
```rust
/// Mixed native/PGAS holds must not revert when a PGAS-routed refund request exceeds the
/// contract's PGAS hold. PGAS settlement is capped to the PGAS actually held, and unrelated
/// native entitlements stay with their original contributor.
#[test]
fn mixed_native_pgas_refund_caps_pgas_without_reverting() {
	run(TestCase {
		accounts: vec![
			AccountSetup { account: ALICE, native: 1_000, pgas: 0 },
			AccountSetup { account: CHARLIE, native: 1_000, pgas: 1_000 },
		],
		charges: vec![
			Charge {
				payer: ALICE,
				amount: 100,
				expected: State {
					payer_native: 900,
					contract_native_held: 100,
					native_entitlement: 100,
					..State::default()
				},
			},
			Charge {
				payer: CHARLIE,
				amount: 40,
				expected: State {
					payer_native: 1_000,
					payer_pgas: 960,
					contract_native_held: 100,
					contract_pgas_held: 40,
					..State::default()
				},
			},
		],
		refund: (CHARLIE, 80),
		expected_after_refund: vec![
			(
				ALICE,
				State {
					payer_native: 900,
					contract_native_held: 100,
					native_entitlement: 100,
					..State::default()
				},
			),
			(
				CHARLIE,
				State {
					payer_native: 1_000,
					// CHARLIE pays 40 PGAS, then receives a 10% refund on the capped 40 PGAS
					// settlement: 1_000 - 40 + 4.
					payer_pgas: 964,
					contract_native_held: 100,
					..State::default()
				},
			),
		],
	});
}
```
