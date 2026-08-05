## Title
`LocalPay::pay` returns a constant, non-unique payment `Id` (`QueryId::MAX`), breaking the "distinct-identifier" invariant relied on by `Config::Paymaster` consumers - ([File: cumulus/parachains/common/src/pay.rs])

## Summary
The external report's core broken invariant is: a function that is supposed to take **two distinct identifiers** (`oldProposalID`, `newProposalID`) silently accepts them being equal, and downstream logic that assumes distinctness (the "already approved" check) is trivially satisfied, letting an attacker collapse the two-identifier check into a no-op that bypasses access control.

The local analog is `LocalPay::pay` / `LocalPay::pay` (via `PayWithSource`) in `cumulus/parachains/common/src/pay.rs`, which is explicitly documented as always returning the **same constant `Id`** (`QueryId::MAX`) for every payment, rather than a fresh, unique identifier per payment: [1](#0-0) 
This is the same bug class: code that is contractually expected to produce/compare **distinct** identifiers instead always collapses to one value, and consumers of the `Pay`/`PayWithSource` trait (e.g. `pallet-treasury`, `pallet-multi-asset-bounties`) rely on that `Id` being unique per in-flight payment to correctly track and settle it.

## Finding Description
`frame_support::traits::tokens::Pay` (and `PayWithSource`) define an associated `Id` type that is meant to uniquely identify one in-flight payment so a pallet can later call `check_payment(id)` to learn whether *that specific* payment succeeded, failed, or is still pending. Pallets such as `pallet-treasury` store this `Id` per spend index (`SpendStatus::status = PaymentState::Attempted { id }`) precisely so multiple concurrent payments can be tracked independently: [2](#0-1)  and later resolved independently via `check_status`: [3](#0-2) . `pallet-multi-asset-bounties` follows the identical pattern, storing a `payment_id` per bounty/child-bounty and looking it up with `get_payment_id(parent_bounty_id, child_bounty_id)` in `check_status`: [4](#0-3) .

`LocalPay` violates this contract on purpose (per its own comment) by always returning `Self::Id::MAX` for both `pay` (used by `pallet-treasury`) and the `PayWithSource::pay` variant (used by `pallet-multi-asset-bounties`): [5](#0-4) [6](#0-5) 
and `check_payment` ignores the `id` argument entirely, always returning `Success`: [7](#0-6) 

This exactly mirrors the DAO report's flaw: a value that is supposed to distinguish one operation from another (`newProposalID` vs `oldProposalID`; here, payment-`Id` A vs payment-`Id` B) is allowed to be identical across unrelated operations, and the code that is supposed to gate on that distinction (`check_payment(id)`/minority-check) instead always resolves the same way regardless of which specific operation is being checked.

## Impact Explanation
Under the current implementation, `check_payment` always returns `Success` regardless of the `id` passed in, so no *direct* fund duplication occurs today because the transfer in `LocalPay::pay` happens synchronously before the `Id` is even returned. However, this breaks the safety invariant that call sites (`pallet-treasury::check_status`, `pallet-multi-asset-bounties::check_status`) rely on: they assume `id` uniquely correlates to *one* spend/bounty's payment attempt. Because the `Id` is a compile-time constant with no relation to the specific spend/bounty, any future change to `check_payment` (e.g. adding real async status tracking, retries, or a lookup table keyed by `Id`) — or any other `Paymaster`/`Pay` consumer that keys state by `Id` rather than by its own index — will silently conflate distinct payments under the single `QueryId::MAX` key. This is explicitly flagged by the code author as a known defect ("breaks the expectation that payment IDs should be unique. See Issue #10450"), i.e., an implementation bug that compromises the intended behavior of the payment-tracking abstraction that treasury/bounty payout settlement is built on.

## Likelihood Explanation
The bug is unconditionally triggered on every single call to `LocalPay::pay` — there is no attacker action required beyond normal use of the treasury `spend`/`payout` or multi-asset-bounties funding/payout flow, both of which are triggerable by ordinary signed accounts (`payout`, `check_status` are signed-origin calls). The defect is deterministic and already acknowledged in-repo via the `// See Issue #10450` comment, meaning the maintainers are aware the identifier is not unique, confirming this is a real, not speculative, deviation from the expected contract.

## Recommendation
Make `LocalPay::pay` (and the `PayWithSource` variant) generate a fresh, unique `Id` per call — e.g., derive it from an incrementing nonce/storage counter or from a hash of `(who, asset, amount, block_number)` — instead of the constant `QueryId::MAX`, and have `check_payment` validate against the specific payment record rather than unconditionally returning `Success`. This restores the invariant that `Id` uniquely identifies one payment, consistent with what `pallet-treasury` and `pallet-multi-asset-bounties` assume when they store and later look up payments by `Id`.

## Proof of Concept
1. Configure a runtime's `pallet-treasury::Config::Paymaster` (or `pallet-multi-asset-bounties`'s payout mechanism) to use `LocalPay`.
2. Call `Treasury::spend(...)` twice to create spend index `0` and spend index `1` with different beneficiaries/amounts.
3. Call `Treasury::payout(origin, 0)` — internally `LocalPay::pay` transfers funds and returns `id = QueryId::MAX`; spend `0`'s status becomes `Attempted { id: QueryId::MAX }` (see `substrate/frame/treasury/src/lib.rs:750`).
4. Call `Treasury::payout(origin, 1)` — `LocalPay::pay` again returns the **same** `id = QueryId::MAX`; spend `1`'s status also becomes `Attempted { id: QueryId::MAX }`.
5. Both spend records now reference the identical `Id`, demonstrating that `Id` carries no distinguishing information between the two independent payments — exactly as documented in the source comment (`cumulus/parachains/common/src/pay.rs:116-121`). Any code path that keys logic off `Id` (rather than off the spend index/bounty index) cannot distinguish these two payments, reproducing the "two supposedly-distinct identifiers collapse to one" flaw described in the external report.

### Citations

**File:** cumulus/parachains/common/src/pay.rs (L102-121)
```rust
	fn pay(
		who: &Self::Beneficiary,
		asset: Self::AssetKind,
		amount: Self::Balance,
	) -> Result<Self::Id, Self::Error> {
		let who = Self::match_location::<A::Type>(who).map_err(|_| DispatchError::Unavailable)?;
		let asset = Self::match_asset(&asset).map_err(|_| DispatchError::Unavailable)?;
		<F as fungibles::Mutate<_>>::transfer(
			asset,
			&A::get(),
			&who,
			amount,
			Preservation::Expendable,
		)?;
		// We use `QueryId::MAX` as a constant identifier for these payments since they are always
		// processed immediately and successfully on the local chain. The `QueryId` type is used to
		// maintain compatibility with XCM payment implementations.
		Ok(Self::Id::MAX) // Always returns the same ID, breaks the expectation that payment IDs should be
		            // unique. See Issue #10450.
	}
```

**File:** cumulus/parachains/common/src/pay.rs (L122-124)
```rust
	fn check_payment(_: Self::Id) -> PaymentStatus {
		PaymentStatus::Success
	}
```

**File:** cumulus/parachains/common/src/pay.rs (L186-206)
```rust
	fn pay(
		source: &Self::Source,
		who: &Self::Beneficiary,
		asset: Self::AssetKind,
		amount: Self::Balance,
	) -> Result<Self::Id, Self::Error> {
		let source = Self::match_location::<A>(source).map_err(|_| DispatchError::Unavailable)?;
		let who = Self::match_location::<A>(who).map_err(|_| DispatchError::Unavailable)?;
		let asset = Self::match_asset(&asset).map_err(|_| DispatchError::Unavailable)?;
		<F as fungibles::Mutate<_>>::transfer(
			asset,
			&source,
			&who,
			amount,
			Preservation::Expendable,
		)?;
		// We use `QueryId::MAX` as a constant identifier for these payments since they are always
		// processed immediately and successfully on the local chain. The `QueryId` type is used to
		// maintain compatibility with XCM payment implementations.
		Ok(Self::Id::MAX)
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

**File:** substrate/frame/multi-asset-bounties/src/lib.rs (L1176-1213)
```rust
		pub fn check_status(
			origin: OriginFor<T>,
			#[pallet::compact] parent_bounty_id: BountyIndex,
			child_bounty_id: Option<BountyIndex>,
		) -> DispatchResultWithPostInfo {
			use BountyStatus::*;

			ensure_signed(origin)?;
			let (asset_kind, value, metadata, status, parent_curator) =
				Self::get_bounty_details(parent_bounty_id, child_bounty_id)?;

			let (new_status, weight) = match status {
				FundingAttempted { ref payment_status, curator } => {
					let new_payment_status = Self::do_check_funding_payment_status(
						parent_bounty_id,
						child_bounty_id,
						payment_status.clone(),
					)?;

					let new_status = match new_payment_status {
						PaymentState::Succeeded => match (child_bounty_id, parent_curator) {
							(Some(_), Some(parent_curator)) if curator == parent_curator => {
								BountyStatus::Active { curator }
							},
							_ => BountyStatus::Funded { curator },
						},
						PaymentState::Pending |
						PaymentState::Failed |
						PaymentState::Attempted { .. } => BountyStatus::FundingAttempted {
							payment_status: new_payment_status,
							curator,
						},
					};

					let weight = <T as Config<I>>::WeightInfo::check_status_funding();

					(new_status, weight)
				},
```
