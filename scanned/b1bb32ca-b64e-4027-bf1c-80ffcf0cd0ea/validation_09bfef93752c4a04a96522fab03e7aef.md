## Analysis

The external report's core broken invariant: a governance-controlled pause disables the user's *protective/claim* action (`repay`) while leaving the *finalizing/penalizing* action (`finalizeLiquidation`) unguarded, so time passes and the user permanently loses funds with no chance to act.

The exact structural analog exists in `pallet-treasury`. `Treasury::payout` is the user's claim action, gated only by frame's dynamic call filter (e.g. `pallet-tx-pause`), while `Treasury::check_status` — a fully permissionless, unrelated dispatchable — silently *destroys* the pending spend once its `expire_at` passes, with no dependency on whether `payout` was ever actually callable. [1](#0-0) [2](#0-1) 

In this repo's own reference runtime, `TxPause` is wired into `BaseCallFilter`, and only `Balances::transfer_keep_alive` is whitelisted as un-pausable — `Treasury::payout` is not: [3](#0-2) 

`check_status` has no equivalent filter dependency and can be called by any signed account, for any spend index, at any time: [4](#0-3) 

### Title
Treasury spends are permanently destroyed by permissionless `check_status` while `payout` is paused, leaving beneficiaries no chance to claim - (File: `substrate/frame/treasury/src/lib.rs`)

### Summary
`pallet-treasury` computes a spend's `expire_at` independently of whether the beneficiary was ever able to call `payout`. If a runtime operator pauses `Treasury::payout` via `pallet-tx-pause` (or any other dynamic call filter) — a legitimate operational action, e.g. to halt a broken `Paymaster` implementation — the beneficiary cannot claim during that window. Meanwhile `Treasury::check_status`, which is *not* filtered and is fully permissionless, keeps working. Once `now > spend.expire_at`, any account can call `check_status(index)` and the pending `SpendStatus` entry is unconditionally `remove`d from storage and marked `SpendProcessed`, before the beneficiary ever got a chance to submit `payout`.

### Finding Description
`Treasury::spend` records `valid_from`/`expire_at` at approval time: [5](#0-4) 

`payout` is the only mechanism through which the beneficiary triggers actual payment, and it is a normal dispatchable subject to `BaseCallFilter` (so `TxPause::pause(("Treasury","payout"))` blocks it with `CallFiltered`): [6](#0-5) 

`check_status`, however, performs its own independent state transition based purely on the wall-clock block number, without checking whether `payout` was ever reachable during the spend's lifetime: [7](#0-6) 

Because `check_status` is `ensure_signed`-only (no `beneficiary` check, no `whenNotPaused`-equivalent guard, no linkage to the pause state of `payout`), any account — even one unrelated to the spend — can finalize the expiry and erase the beneficiary's entitlement the moment `expire_at` passes, exactly mirroring the reported pattern: the protective call (`repay`/`payout`) is disabled, but the finalizing call (`finalizeLiquidation`/`check_status`) is not, so the guarded resource is destroyed while the victim had no way to act.

### Impact Explanation
This causes a permanent, unrecoverable loss of an approved treasury allocation for the rightful beneficiary — funds that were already earmarked and approved by governance are voided by a routine, permissionless housekeeping call, with the beneficiary never having had an opportunity to exercise `payout`. This falls squarely under "permanent user-fund lock" / "duplicate settlement or payout" state-advancement-without-settlement in the required impact set, since the spend's `status` advances to a terminal `SpendProcessed` state without the payment ever executing.

### Likelihood Explanation
Pausing individual calls via `pallet-tx-pause` (or any other dynamic filter composed into `BaseCallFilter`) is an explicitly supported, expected operational tool in this codebase — the wiki/runtime already wires `TxPause` into `frame_system::Config::BaseCallFilter` and documents pausing pallets like `Balances` and `Utility`. Any runtime that pauses `Treasury::payout` for even a short incident window that straddles a spend's `PayoutPeriod` will trigger this: no attacker collusion, validator/relayer misbehavior, or key compromise is required — an ordinary permissionless account calling `check_status` after expiry suffices.

### Recommendation
Make `check_status`'s expiry-removal branch consistent with the pause state of `payout` (e.g., skip expiring/removing a `Pending` spend if `Treasury::payout` is currently filtered/paused, or extend `expire_at` while `payout` is unreachable), analogous to how `payout_extends_expiry` already resets `expire_at` on a failed payment attempt: [8](#0-7) 

### Proof of Concept
1. Configure a runtime with `pallet-treasury` and `pallet-tx-pause`, with `Treasury::payout` not whitelisted (as in the reference node runtime).
2. Governance approves a spend: `Treasury::spend(origin, asset_kind, amount, beneficiary, None)` → `expire_at = now + PayoutPeriod`.
3. Before the beneficiary claims, `PauseOrigin` calls `TxPause::pause(("Treasury","payout"))`.
4. Advance blocks until `now > expire_at` while the pause is still active. The beneficiary's `Treasury::payout(index)` call fails with `frame_system::Error::CallFiltered`.
5. Any account calls `Treasury::check_status(index)`. Since `now > spend.expire_at` and `status == Pending`, the entry is removed and `Event::SpendProcessed` is emitted — the approved spend is permanently gone.
6. Even after `TxPause::unpause(("Treasury","payout"))`, the beneficiary can no longer call `payout` (`Error::InvalidIndex`), having lost the funds without ever being able to claim them.

### Citations

**File:** substrate/frame/treasury/src/lib.rs (L661-664)
```rust
			let now = T::BlockNumberProvider::current_block_number();
			let valid_from = valid_from.unwrap_or(now);
			let expire_at = valid_from.saturating_add(T::PayoutPeriod::get());
			ensure!(expire_at > now, Error::<T, I>::SpendExpired);
```

**File:** substrate/frame/treasury/src/lib.rs (L734-757)
```rust
		#[pallet::call_index(6)]
		#[pallet::weight(T::WeightInfo::payout())]
		pub fn payout(origin: OriginFor<T>, index: SpendIndex) -> DispatchResult {
			ensure_signed(origin)?;
			let mut spend = Spends::<T, I>::get(index).ok_or(Error::<T, I>::InvalidIndex)?;
			let now = T::BlockNumberProvider::current_block_number();
			ensure!(now >= spend.valid_from, Error::<T, I>::EarlyPayout);
			ensure!(spend.expire_at > now, Error::<T, I>::SpendExpired);
			ensure!(
				matches!(spend.status, PaymentState::Pending | PaymentState::Failed),
				Error::<T, I>::AlreadyAttempted
			);

			let id = T::Paymaster::pay(&spend.beneficiary, spend.asset_kind.clone(), spend.amount)
				.map_err(|_| Error::<T, I>::PayoutError)?;

			spend.status = PaymentState::Attempted { id };
			spend.expire_at = now.saturating_add(T::PayoutPeriod::get());
			Spends::<T, I>::insert(index, spend);

			Self::deposit_event(Event::<T, I>::Paid { index, payment_id: id });

			Ok(())
		}
```

**File:** substrate/frame/treasury/src/lib.rs (L778-813)
```rust
		#[pallet::call_index(7)]
		#[pallet::weight(T::WeightInfo::check_status())]
		pub fn check_status(origin: OriginFor<T>, index: SpendIndex) -> DispatchResultWithPostInfo {
			use PaymentState as State;
			use PaymentStatus as Status;

			ensure_signed(origin)?;
			let mut spend = Spends::<T, I>::get(index).ok_or(Error::<T, I>::InvalidIndex)?;
			let now = T::BlockNumberProvider::current_block_number();

			if now > spend.expire_at && !matches!(spend.status, State::Attempted { .. }) {
				// spend has expired and no further status update is expected.
				Spends::<T, I>::remove(index);
				Self::deposit_event(Event::<T, I>::SpendProcessed { index });
				return Ok(Pays::No.into());
			}

			let payment_id = match spend.status {
				State::Attempted { id } => id,
				_ => return Err(Error::<T, I>::NotAttempted.into()),
			};

			match T::Paymaster::check_payment(payment_id) {
				Status::Failure => {
					spend.status = PaymentState::Failed;
					Spends::<T, I>::insert(index, spend);
					Self::deposit_event(Event::<T, I>::PaymentFailed { index, payment_id });
				},
				Status::Success | Status::Unknown => {
					Spends::<T, I>::remove(index);
					Self::deposit_event(Event::<T, I>::SpendProcessed { index });
					return Ok(Pays::No.into());
				},
				Status::InProgress => return Err(Error::<T, I>::Inconclusive.into()),
			}
			return Ok(Pays::Yes.into());
```

**File:** substrate/bin/node/runtime/src/lib.rs (L258-327)
```rust
/// Calls that cannot be paused by the tx-pause pallet.
pub struct TxPauseWhitelistedCalls;
/// Whitelist `Balances::transfer_keep_alive`, all others are pauseable.
impl Contains<RuntimeCallNameOf<Runtime>> for TxPauseWhitelistedCalls {
	fn contains(full_name: &RuntimeCallNameOf<Runtime>) -> bool {
		match (full_name.0.as_slice(), full_name.1.as_slice()) {
			(b"Balances", b"transfer_keep_alive") => true,
			_ => false,
		}
	}
}

#[cfg(feature = "runtime-benchmarks")]
pub struct AssetRateArguments;
#[cfg(feature = "runtime-benchmarks")]
impl AssetKindFactory<NativeOrWithId<u32>> for AssetRateArguments {
	fn create_asset_kind(seed: u32) -> NativeOrWithId<u32> {
		if !seed.is_multiple_of(2) {
			NativeOrWithId::Native
		} else {
			NativeOrWithId::WithId(seed / 2)
		}
	}
}

#[cfg(feature = "runtime-benchmarks")]
pub struct PalletTreasuryArguments;
#[cfg(feature = "runtime-benchmarks")]
impl PalletTreasuryArgumentsFactory<NativeOrWithId<u32>, AccountId> for PalletTreasuryArguments {
	fn create_asset_kind(seed: u32) -> NativeOrWithId<u32> {
		if !seed.is_multiple_of(2) {
			NativeOrWithId::Native
		} else {
			NativeOrWithId::WithId(seed / 2)
		}
	}

	fn create_beneficiary(seed: [u8; 32]) -> AccountId {
		AccountId::from_entropy(&mut seed.as_slice()).unwrap()
	}
}

#[cfg(feature = "runtime-benchmarks")]
pub struct PalletMultiAssetBountiesArguments;
#[cfg(feature = "runtime-benchmarks")]
impl PalletMultiAssetBountiesArgumentsFactory<NativeOrWithId<u32>, AccountId, u128>
	for PalletMultiAssetBountiesArguments
{
	fn create_asset_kind(seed: u32) -> NativeOrWithId<u32> {
		if !seed.is_multiple_of(2) {
			NativeOrWithId::Native
		} else {
			NativeOrWithId::WithId(seed / 2)
		}
	}

	fn create_beneficiary(seed: [u8; 32]) -> AccountId {
		AccountId::from_entropy(&mut seed.as_slice()).unwrap()
	}
}

impl pallet_tx_pause::Config for Runtime {
	type RuntimeEvent = RuntimeEvent;
	type RuntimeCall = RuntimeCall;
	type PauseOrigin = EnsureRoot<AccountId>;
	type UnpauseOrigin = EnsureRoot<AccountId>;
	type WhitelistedCalls = TxPauseWhitelistedCalls;
	type MaxNameLen = ConstU32<256>;
	type WeightInfo = pallet_tx_pause::weights::SubstrateWeight<Runtime>;
}
```

**File:** substrate/frame/treasury/src/tests.rs (L674-701)
```rust
#[test]
fn payout_extends_expiry() {
	ExtBuilder::default().build().execute_with(|| {
		assert_eq!(<Test as Config>::PayoutPeriod::get(), 5);

		System::set_block_number(1);
		assert_ok!(Treasury::spend(RuntimeOrigin::signed(10), Box::new(1), 2, Box::new(6), None));
		// Fail a payout at block 4
		System::set_block_number(4);
		assert_ok!(Treasury::payout(RuntimeOrigin::signed(1), 0));
		assert_eq!(paid(6, 1), 2);
		let payment_id = get_payment_id(0).expect("no payment attempt");
		// spend payment is failed
		set_status(payment_id, PaymentStatus::Failure);
		unpay(6, 1, 2);

		// check status to set the correct state
		assert_ok!(Treasury::check_status(RuntimeOrigin::signed(1), 0));
		System::assert_last_event(Event::<Test, _>::PaymentFailed { index: 0, payment_id }.into());

		// Retrying at after the initial expiry date but before the new one succeeds
		System::set_block_number(7);

		// the payout can be retried now
		assert_ok!(Treasury::payout(RuntimeOrigin::signed(1), 0));
		assert_eq!(paid(6, 1), 2);
	});
}
```
