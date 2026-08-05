### Title
Pre-PGAS native storage deposits are burned instead of fully refunded after the v4 native→PGAS migration - (File: `substrate/frame/revive/src/migrations/v4.rs` / `substrate/frame/revive/src/deposit_payment.rs`)

### Summary
The `pallet-revive` v4 migration (PGAS storage-deposit backend) converts a contract's native `StorageDepositReserve` hold into an equivalent PGAS hold via `migrate_native_to_pgas`, but never records a matching `NativeDepositOf` entitlement for the users who originally paid that native deposit. When that contract's deposit is later refunded (partial refund or on termination), `PGasDeposit::refund_on_hold` / `refund_all` treat the migrated balance as ordinary PGAS, applying `RefundPercent` and permanently burning the remainder — turning a 100%-repayable native deposit into a partially-burned one, purely as a side effect of the migration.

### Finding Description
`PGasDeposit::charge_and_hold` [1](#0-0)  is the only place that populates `NativeDepositOf`, via `record_native_deposit(from, to, amount)` [2](#0-1) , and it is only invoked for charges made *after* the runtime adopts the PGAS backend.

Deposits paid before the migration (under the plain `()` native-only backend) never touch `NativeDepositOf` — that map is introduced by this migration itself, and phase 1 only backfills it for **code-upload** deposits (`CodeInfoOf`), not for per-contract **storage** deposits: [3](#0-2) .

Phase 2 (`step_2_contract`) then migrates every contract's entire native `StorageDepositReserve` hold to PGAS with a single pooled call to `migrate_native_to_pgas`, burning the native hold and minting/holding the same amount in PGAS, with no per-contributor breakdown and no `NativeDepositOf` update: [4](#0-3)  and [5](#0-4) .

After migration, when that contract's deposit is refunded (e.g. on `clear_storage` triggering a partial refund, or on contract termination via `refund_all`), `PGasDeposit::refund_on_hold`/`refund_all` compute `contribution = NativeDepositOf::<T>::get(from, to)`, which is `0` for these pre-migration depositors, so the full refund routes through `settle_pgas_refund`: [6](#0-5)  and [7](#0-6) .

`settle_pgas_refund` only returns `RefundPercent` of the amount and unconditionally burns the rest: [8](#0-7) . `RefundPercent` exists specifically to stop users from "harvesting" PGAS allowance they never earned by charging PGAS, releasing it, and pocketing the refund — but users who paid in DOT before the PGAS backend existed never interacted with PGAS at all; the migration silently reclassifies their principal as burnable PGAS.

### Impact Explanation
Every user who funded a contract's storage deposit in native currency prior to the runtime enabling the PGAS backend loses `(1 - RefundPercent)` of their deposit the moment that contract's deposit is refunded post-migration (partial storage-clear refund, or full refund on `do_terminate`/contract self-destruct). This is unconditional value destruction of previously-fully-refundable user principal, not bounded by admin action, malicious actor, or user error — it is triggered purely by normal contract lifecycle operations after a routine runtime upgrade. This is a genuine conservation-of-value violation ("settle exactly once to the rightful beneficiary and amount") and results in permanent, unrecoverable loss of user funds (burned, not merely delayed).

### Likelihood Explanation
Any parachain that (a) already has live contracts with native storage deposits and (b) upgrades to adopt the `PGasDeposit` backend will trigger this on every affected contract as soon as a refund event occurs (clearing storage, or contract termination) — both are ordinary, permissionless, frequently-occurring contract operations, not privileged or adversarial actions. No malicious relayer, validator, or governance abuse is required; the bug is purely in the migration's data-modeling gap (pooled per-contract hold vs. per-contributor entitlement).

### Recommendation
During phase 2 of the v4 migration, either (a) skip converting the native hold to PGAS for contracts whose deposit predates the PGAS backend and instead register a `NativeDepositOf` entitlement equal to the migrated amount attributed to the known depositor(s) (mirroring what phase 1 does for code-upload deposits), or (b) preserve a per-contract flag/metadata marking the migrated amount as "fully native-refundable" so `refund_on_hold`/`refund_all` bypass `RefundPercent` burning for that portion. If per-contributor attribution is unavailable at migration time (pooled deposits from multiple past depositors), the safer default is to leave the native hold as native currency for migrated contracts rather than converting it to punitively-refunded PGAS.

### Proof of Concept
1. Deploy `pallet-revive` on version 3 (native-only `()` `Deposit` backend). User `U` calls a contract `C`, causing a storage-deposit charge of `1_000` DOT held under `HoldReason::StorageDepositReserve` on `C`'s account (no `NativeDepositOf` entry exists yet, as confirmed by the migration's phase 2 test which seeds contracts this way): [9](#0-8) .
2. Runtime upgrades and enables the PGAS `Deposit` backend; the v4 migration runs. Phase 2 processes `C`: `migrate_native_to_pgas` burns `C`'s `1_000` native hold and re-holds `1_000` PGAS on `C`, as shown by `phase_two_burns_native_and_mints_pgas_on_contracts` [10](#0-9) . No `NativeDepositOf` entry is created for `(C, U)`.
3. `U` later clears the storage that backed the deposit (or the contract self-destructs), triggering `refund_deposit`/`refund_all` on `C`: `NativeDepositOf::get(C, U) == 0`, so `native_requested == 0` and the full `1_000` goes through `settle_pgas_refund`, which returns only `RefundPercent * 1_000` to `U` and burns `(1 - RefundPercent) * 1_000` — e.g. with `RefundPercent = 10%` (as used in the repo's own tests, see `mixed_native_pgas_refund_caps_pgas_without_reverting` [11](#0-10) ), `U` receives `100` instead of the `1_000` they are entitled to, and `900` is permanently burned.

### Citations

**File:** substrate/frame/revive/src/deposit_payment.rs (L348-375)
```rust
	fn charge_and_hold(
		reason: HoldReason,
		src: Funds<T::AccountId>,
		to: &T::AccountId,
		amount: BalanceOf<T>,
	) -> DispatchResult {
		let from = match &src {
			Funds::Balance(from) | Funds::TxFee(from) => *from,
		};

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

		Ok(())
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

**File:** substrate/frame/revive/src/deposit_payment.rs (L556-562)
```rust
	/// Record that user `from` contributed `amount` in native balance to contract `to`.
	/// Read by [`Self::refund_on_hold`] to cap the native portion of refunds.
	fn record_native_deposit(from: &T::AccountId, to: &T::AccountId, amount: BalanceOf<T>) {
		NativeDepositOf::<T>::mutate(to, from, |entitlement| {
			*entitlement = entitlement.saturating_add(amount);
		});
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
