Based on the investigation, the strongest and most directly analogous pattern in this repository is in `pallet-treasury`'s spend/payout retry flow, where a spend approved against a given `asset_kind` is never re-validated against the current `pallet-asset-rate` (or `Paymaster`) whitelist state before a delayed or retried `payout`.

### Title
Treasury `payout`/`check_status` retry re-executes a payment for a delisted `asset_kind` without revalidating it against `pallet-asset-rate` - ([File: substrate/frame/treasury/src/lib.rs])

### Summary
The Paladin bug is a class of "approve once, keep using forever" bug: a resource (reward token) is validated only at creation time, and a later "continuation" action (`extend_lock`) reuses the stored resource reference without re-checking whether it is still whitelisted. The closest local analog is the Treasury `spend` → `payout` → `check_status`/retry flow, where `asset_kind` validity (via `T::BalanceConverter`/`pallet-asset-rate`) is checked only once, at `spend()` time, and never re-checked on subsequent `payout()` retries, even though a `Spend` can remain in `Pending`/`Failed` state and be retried across many blocks/eras during which the asset's conversion rate can be removed.

### Finding Description
`Pallet::spend` performs a validity/permission check on the `asset_kind` via `T::BalanceConverter::from_asset_balance`, which for the reference runtime configuration is backed by `pallet_asset_rate::Pallet` reading `ConversionRateToNative::<T>::get(asset_kind)`: [1](#0-0) 

This check exists solely to bound the spend under `SpendOrigin`'s max amount at approval time: [2](#0-1) 

Once approved, the `SpendStatus` record (`asset_kind`, `amount`, `beneficiary`) is persisted in `Spends` storage and is claimable within `PayoutPeriod` via `payout`: [3](#0-2) 

Crucially, `payout` never calls `T::BalanceConverter` (i.e., `pallet_asset_rate`) again — it only calls `T::Paymaster::pay(&spend.beneficiary, spend.asset_kind.clone(), spend.amount)` using the `asset_kind` frozen at approval time. If a payout attempt fails (`PaymentStatus::Failure`), `check_status` flips the spend back to `PaymentState::Failed` rather than removing it, explicitly permitting the same `asset_kind`/`amount` to be retried via `payout` again — this is the "extend" analog: [4](#0-3) 

The retry semantics are demonstrated directly in `payout_retry_works`/`payout_extends_expiry`, showing that `expire_at` is pushed forward and `payout` can be called again on the same `SpendIndex` using the original `asset_kind`: [5](#0-4) 

Meanwhile, `pallet_asset_rate::remove` allows `RemoveOrigin` to delist an `asset_kind`'s conversion rate at any time, with no coupling to outstanding `Spends`: [6](#0-5) 

Because `payout`'s only guards are `valid_from`/`expire_at`/`PaymentState`, none of which reference `T::BalanceConverter` or asset-kind validity, a `Spend` created for an `asset_kind` that is later delisted from `pallet-asset-rate` (or whose backing asset is destroyed/frozen) continues to be replayable via `payout` for as long as it keeps failing and being retried within the rolling `PayoutPeriod` window extended by `check_status`/`payout`, i.e., indefinitely, exactly mirroring the pledge-extension bug's "checked once at creation, never re-checked on continuation" pattern.

### Impact Explanation
This does not enable outright theft (the beneficiary and amount are unchanged from approval), but it does allow an approved-but-unclaimed treasury commitment involving a delisted/invalid asset kind to survive indefinitely through the failure/retry cycle instead of being forced to expire and go through re-approval under `SpendOrigin` with current whitelist state. In configurations where asset delisting is meant to be a hard security control (e.g., an asset was found to be malicious, mispriced, or is being sunset), this bypasses that governance intent — the same underlying issue flagged as Medium risk in the source report (bypassing token delisting via a "continuation" primitive rather than a fresh approval).

### Likelihood Explanation
Likelihood is limited by the practical need for: (1) a pending/failed spend outstanding at the time of delisting, and (2) `T::Paymaster`/`pallet_assets` implementation details determining whether `pay()` against a delisted/destroyed asset kind actually succeeds. In many concrete runtime wirings, `PayAssetFromAccount` will itself fail if the underlying asset no longer exists, which would prevent exploitation. This makes the issue conditional on runtime configuration rather than universally exploitable, and no unprivileged attacker action is required — the "attacker" role here is really a governance actor delisting an asset while spends are outstanding, which is a lower-confidence trigger than the original report's pure user-facing bug.

### Recommendation
Re-validate the `asset_kind` at `payout()` time (and/or at `check_status`) by calling `T::BalanceConverter::from_asset_balance` (or an equivalent asset-kind-liveness check) before invoking `T::Paymaster::pay`, and reject/void the spend if the asset kind is no longer recognized, rather than silently retrying indefinitely.

### Proof of Concept
1. `SpendOrigin` calls `Treasury::spend(origin, asset_kind = X, amount, beneficiary, None)` while `pallet_asset_rate::ConversionRateToNative(X)` exists — passes the `native_amount` check in `spend()`.
2. Before `payout` succeeds, `RemoveOrigin` calls `pallet_asset_rate::remove(X)`, delisting `X`.
3. Caller invokes `Treasury::payout(index)`. Because `payout` never calls `T::BalanceConverter`/`pallet_asset_rate` again, only the paymaster's own asset lookup gates success; if the paymaster's underlying asset for `X` still exists/is spendable (e.g., asset not yet destroyed, only the AssetRate delisted), `payout` succeeds despite `X` being delisted.
4. Even if a given attempt fails (`Status::Failure`), `check_status` sets state to `Failed` and extends `expire_at`, permitting the caller to call `payout` again later with the same delisted `asset_kind`, indefinitely repeating step 3 within each new `PayoutPeriod` window — mirroring the "extend pledge to keep using a delisted reward token" pattern from the source report. [3](#0-2) [4](#0-3)

### Citations

**File:** substrate/frame/treasury/src/lib.rs (L651-713)
```rust
		pub fn spend(
			origin: OriginFor<T>,
			asset_kind: Box<T::AssetKind>,
			#[pallet::compact] amount: AssetBalanceOf<T, I>,
			beneficiary: Box<BeneficiaryLookupOf<T, I>>,
			valid_from: Option<BlockNumberFor<T, I>>,
		) -> DispatchResult {
			let max_amount = T::SpendOrigin::ensure_origin(origin)?;
			let beneficiary = T::BeneficiaryLookup::lookup(*beneficiary)?;

			let now = T::BlockNumberProvider::current_block_number();
			let valid_from = valid_from.unwrap_or(now);
			let expire_at = valid_from.saturating_add(T::PayoutPeriod::get());
			ensure!(expire_at > now, Error::<T, I>::SpendExpired);

			let native_amount =
				T::BalanceConverter::from_asset_balance(amount, *asset_kind.clone())
					.map_err(|_| Error::<T, I>::FailedToConvertBalance)?;

			ensure!(native_amount <= max_amount, Error::<T, I>::InsufficientPermission);

			with_context::<SpendContext<BalanceOf<T, I>>, _>(|v| {
				let context = v.or_default();
				// We group based on `max_amount`, to distinguish between different kind of
				// origins. (assumes that all origins have different `max_amount`)
				//
				// Worst case is that we reject some "valid" request.
				let spend = context.spend_in_context.entry(max_amount).or_default();

				// Ensure that we don't overflow nor use more than `max_amount`
				if spend.checked_add(&native_amount).map(|s| s > max_amount).unwrap_or(true) {
					Err(Error::<T, I>::InsufficientPermission)
				} else {
					*spend = spend.saturating_add(native_amount);
					Ok(())
				}
			})
			.unwrap_or(Ok(()))?;

			let index = SpendCount::<T, I>::get();
			Spends::<T, I>::insert(
				index,
				SpendStatus {
					asset_kind: *asset_kind.clone(),
					amount,
					beneficiary: beneficiary.clone(),
					valid_from,
					expire_at,
					status: PaymentState::Pending,
				},
			);
			SpendCount::<T, I>::put(index + 1);

			Self::deposit_event(Event::AssetSpendApproved {
				index,
				asset_kind: *asset_kind,
				amount,
				beneficiary,
				valid_from,
				expire_at,
			});
			Ok(())
		}
```

**File:** substrate/frame/treasury/src/lib.rs (L736-757)
```rust
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

**File:** substrate/frame/treasury/src/lib.rs (L778-814)
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

**File:** substrate/frame/asset-rate/src/lib.rs (L218-235)
```rust
		/// Remove an existing conversion rate to native balance for the given asset.
		///
		/// ## Complexity
		/// - O(1)
		#[pallet::call_index(2)]
		#[pallet::weight(T::WeightInfo::remove())]
		pub fn remove(origin: OriginFor<T>, asset_kind: Box<T::AssetKind>) -> DispatchResult {
			T::RemoveOrigin::ensure_origin(origin)?;

			ensure!(
				ConversionRateToNative::<T>::contains_key(asset_kind.as_ref()),
				Error::<T>::UnknownAssetKind
			);
			ConversionRateToNative::<T>::remove(asset_kind.as_ref());

			Self::deposit_event(Event::AssetRateRemoved { asset_kind: *asset_kind });
			Ok(())
		}
```
