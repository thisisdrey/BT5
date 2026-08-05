All citations verified against the actual source code and match exactly, with line numbers and content confirmed. The chain of logic holds: `BalanceSwapAction::claim` at `substrate/frame/atomic-swap/src/lib.rs:153-155` discards the shortfall from `repatriate_reserved` and reports success on `.is_ok()`; `claim_swap` at lines 311-319 unconditionally removes `PendingSwaps` and emits `success: true` regardless of actual transferred amount; `cancel_swap` at lines 332-352 requires the entry to still exist, so once removed there's no recovery path; `do_transfer_reserved` at `substrate/frame/balances/src/lib.rs:1258-1282` caps `actual` at `reducible_total_balance_on_hold` under `BestEffort`; and `reducible_total_balance_on_hold` at `substrate/frame/balances/src/impl_fungible.rs:262-273` subtracts `frozen.saturating_sub(free)` from `reserved`, confirming freeze/lock enforcement is respected (rebutting the theft hypothesis) while exposing the genuine partial-claim fund-lock defect. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) 

Based on this verification, the claim's analysis is accurate and its conclusion (rejecting the theft/bypass hypothesis but identifying the distinct partial-claim fund-lock defect) is well-supported by the code.

Audit Report

## Title
Partial BestEffort claim reported as full success permanently strands leftover reserved swap funds - (File: substrate/frame/atomic-swap/src/lib.rs)

## Summary
`claim_swap` treats any `Ok` result from `repatriate_reserved` (which uses `Precision::BestEffort`) as full success, deletes the `PendingSwaps` entry, and never re-checks that the full swap value was transferred. If a lock/freeze on the source reduces the transferable amount below the swap's committed value, the shortfall remains reserved with no remaining path to recover it.

## Finding Description
`BalanceSwapAction::claim` calls `C::repatriate_reserved(source, target, self.value, BalanceStatus::Free).is_ok()` at `substrate/frame/atomic-swap/src/lib.rs:153-155`. `repatriate_reserved` performs a `BestEffort`, `Polite` transfer capped by `reducible_total_balance_on_hold`, and returns `Ok(shortfall)` even when less than the requested value was moved (`substrate/frame/balances/src/impl_currency.rs:725-734`, `substrate/frame/balances/src/lib.rs:1258-1282`). `claim_swap` unconditionally removes the `PendingSwaps` storage entry after this call, regardless of the transferred amount (`substrate/frame/atomic-swap/src/lib.rs:311-319`). Once removed, `cancel_swap` can no longer act on it (`Error::NotExist`, `substrate/frame/atomic-swap/src/lib.rs:339`), and there is no other dispatchable call to unreserve the residual reserved balance created by `create_swap`'s `reserve()`.

Freeze enforcement itself is correctly respected: `reducible_total_balance_on_hold` at `substrate/frame/balances/src/impl_fungible.rs:262-273` subtracts `frozen.saturating_sub(free)` from `reserved`, meaning locked/frozen value cannot be siphoned out via `claim_swap`. No bypass or theft path exists.

## Impact Explanation
A portion of a user's balance can become permanently locked in the `reserved` state with no on-chain path to free it, matching the "permanent user-fund lock" impact category.

## Likelihood Explanation
Requires that a lock/freeze reduce the source's spendable-under-freeze reserved balance between `create_swap` and `claim_swap` — achievable via ordinary public extrinsics (e.g., vesting, staking bonding, governance locks) that a source account can trigger on itself, or that occur incidentally in normal chain usage.

## Recommendation
In `BalanceSwapAction::claim`, use `Precision::Exact` (or otherwise verify the full `value` was moved, e.g., check the returned shortfall is zero) before reporting success, and if not fully claimable, avoid deleting the `PendingSwaps` entry (or provide a recovery path) so the source can still `cancel_swap` for the unclaimed remainder.

## Proof of Concept
1. `create_swap(target, hashed_proof, action=Balance(value=100), duration)` — reserves 100 from source's free balance.
2. Source (or some other actor) applies a public lock/freeze on the source account such that `reducible_total_balance_on_hold(source, Polite)` becomes less than 100 (e.g., via `set_lock`/vesting/staking flows).
3. Target calls `claim_swap(proof, action)`. `repatriate_reserved` moves only the reduced `actual` amount, returns `Ok(shortfall>0)`, `claim()` returns `true`.
4. `claim_swap` deletes the `PendingSwaps` entry and emits `SwapClaimed { success: true }` even though target received less than 100.
5. Source can no longer call `cancel_swap` (entry gone); the shortfall remains reserved indefinitely with no available extrinsic to release it.

### Citations

**File:** substrate/frame/atomic-swap/src/lib.rs (L153-155)
```rust
	fn claim(&self, source: &AccountId, target: &AccountId) -> bool {
		C::repatriate_reserved(source, target, self.value, BalanceStatus::Free).is_ok()
	}
```

**File:** substrate/frame/atomic-swap/src/lib.rs (L311-319)
```rust
			let succeeded = swap.action.claim(&swap.source, &target);

			PendingSwaps::<T>::remove(target.clone(), hashed_proof);

			Self::deposit_event(Event::SwapClaimed {
				account: target,
				proof: hashed_proof,
				success: succeeded,
			});
```

**File:** substrate/frame/atomic-swap/src/lib.rs (L332-352)
```rust
		pub fn cancel_swap(
			origin: OriginFor<T>,
			target: T::AccountId,
			hashed_proof: HashedProof,
		) -> DispatchResult {
			let source = ensure_signed(origin)?;

			let swap = PendingSwaps::<T>::get(&target, hashed_proof).ok_or(Error::<T>::NotExist)?;
			ensure!(swap.source == source, Error::<T>::SourceMismatch);
			ensure!(
				frame_system::Pallet::<T>::block_number() >= swap.end_block,
				Error::<T>::DurationNotPassed,
			);

			swap.action.cancel(&swap.source);
			PendingSwaps::<T>::remove(&target, hashed_proof);

			Self::deposit_event(Event::SwapCancelled { account: target, proof: hashed_proof });

			Ok(())
		}
```

**File:** substrate/frame/balances/src/impl_currency.rs (L716-734)
```rust
	/// Move the reserved balance of one account into the balance of another, according to `status`.
	///
	/// Is a no-op if:
	/// - the value to be moved is zero; or
	/// - the `slashed` id equal to `beneficiary` and the `status` is `Reserved`.
	///
	/// This is `Polite` and thus will not repatriate any funds which would lead the total balance
	/// to be less than the frozen amount. Returns `Ok` with the actual amount of funds moved,
	/// which may be less than `value` since the operation is done on a `BestEffort` basis.
	fn repatriate_reserved(
		slashed: &T::AccountId,
		beneficiary: &T::AccountId,
		value: Self::Balance,
		status: Status,
	) -> Result<Self::Balance, DispatchError> {
		let actual =
			Self::do_transfer_reserved(slashed, beneficiary, value, BestEffort, Polite, status)?;
		Ok(value.saturating_sub(actual))
	}
```

**File:** substrate/frame/balances/src/lib.rs (L1258-1282)
```rust
		pub(crate) fn do_transfer_reserved(
			slashed: &T::AccountId,
			beneficiary: &T::AccountId,
			value: T::Balance,
			precision: Precision,
			fortitude: Fortitude,
			status: Status,
		) -> Result<T::Balance, DispatchError> {
			if value.is_zero() {
				return Ok(Zero::zero());
			}

			let max = <Self as fungible::InspectHold<_>>::reducible_total_balance_on_hold(
				slashed, fortitude,
			);
			let actual = match precision {
				Precision::BestEffort => value.min(max),
				Precision::Exact => value,
			};
			ensure!(actual <= max, TokenError::FundsUnavailable);
			if slashed == beneficiary {
				return match status {
					Status::Free => Ok(actual.saturating_sub(Self::unreserve(slashed, actual))),
					Status::Reserved => Ok(actual),
				};
```

**File:** substrate/frame/balances/src/impl_fungible.rs (L262-273)
```rust
	fn reducible_total_balance_on_hold(who: &T::AccountId, force: Fortitude) -> Self::Balance {
		// The total balance must never drop below the freeze requirements if we're not forcing:
		let a = Self::account(who);
		let unavailable = if force == Force {
			Self::Balance::zero()
		} else {
			// The freeze lock applies to the total balance, so we can discount the free balance
			// from the amount which the total reserved balance must provide to satisfy it.
			a.frozen.saturating_sub(a.free)
		};
		a.reserved.saturating_sub(unavailable)
	}
```
