## Analysis

I reviewed `claim_swap` in `substrate/frame/atomic-swap/src/lib.rs` together with the underlying `ReservableCurrency`/`fungible` hold-and-freeze machinery in `pallet-balances` to check whether the hypothesis in Q1442 (claim bypasses locks/holds/freezes to enable theft) actually holds.

**Freeze/lock enforcement is actually respected at claim time.** `BalanceSwapAction::claim` calls `C::repatriate_reserved(source, target, self.value, BalanceStatus::Free)` [1](#0-0) , which routes to `Pallet::repatriate_reserved` → `do_transfer_reserved(..., BestEffort, Polite, status)` [2](#0-1) . `do_transfer_reserved` caps the transferable amount at `reducible_total_balance_on_hold(slashed, Polite)`, which subtracts `frozen.saturating_sub(free)` from `reserved` [3](#0-2) [4](#0-3) . This means a lock/freeze placed on the source's total balance genuinely constrains how much of the reserved swap value can leave the account — locked/frozen value cannot be siphoned out through `claim_swap` beyond what freeze semantics allow. There is no bypass or theft path here.

**However, the "BestEffort" partial-transfer result is mishandled as full success.** `repatriate_reserved` returns `Ok(shortfall)` even when it only moved part of the requested amount (`Precision::BestEffort`) [2](#0-1) . `BalanceSwapAction::claim` only checks `.is_ok()` and discards the shortfall, so it reports `true` ("succeeded") even on a partial move [1](#0-0) . `claim_swap` then unconditionally deletes the `PendingSwaps` entry and emits `SwapClaimed { success: true }` regardless of whether the full `value` actually moved [5](#0-4) .

Because the `PendingSwaps` entry is gone, `cancel_swap` — the only path that calls `action.cancel()` (`unreserve`) on the source's reserved balance — is no longer available (`Error::NotExist`) [6](#0-5) . If a lock/freeze (set via ordinary public extrinsics such as vesting, staking bonding, or a governance-style lock) reduces `reducible_total_balance_on_hold` below the swap's promised `value` between `create_swap` and `claim_swap`, the unmoved remainder stays in `reserved` with no dispatchable path to release it — a genuine **permanent fund lock**, not a theft/bypass.

## Conclusion

This does not confirm the hypothesized "bypass hold/lock/freeze to steal funds" scenario — freeze enforcement in `do_transfer_reserved` correctly limits withdrawable amount. It does surface a distinct, real defect: `claim_swap`'s reliance on `BestEffort`+`.is_ok()` semantics can report success and irreversibly delete the swap record on a partial transfer, permanently stranding the unmoved reserved remainder since no unreserve/cancel path remains.

### Title
Partial BestEffort claim reported as full success permanently strands leftover reserved swap funds - (File: substrate/frame/atomic-swap/src/lib.rs)

### Summary
`claim_swap` treats any `Ok` result from `repatriate_reserved` (which uses `Precision::BestEffort`) as full success, deletes the `PendingSwaps` entry, and never re-checks that the full swap value was transferred. If a lock/freeze on the source reduces the transferable amount below the swap's committed value, the shortfall remains reserved with no remaining path to recover it.

### Finding Description
`BalanceSwapAction::claim` calls `C::repatriate_reserved(source, target, self.value, BalanceStatus::Free).is_ok()` [1](#0-0) . `repatriate_reserved` performs a `BestEffort`, `Polite` transfer capped by `reducible_total_balance_on_hold`, and returns `Ok(shortfall)` even when less than the requested value was moved [2](#0-1) [7](#0-6) . `claim_swap` unconditionally removes the `PendingSwaps` storage entry after this call, regardless of the transferred amount [5](#0-4) . Once removed, `cancel_swap` can no longer act on it (`Error::NotExist`), and there is no other dispatchable call to unreserve the residual generic reserved balance created by `create_swap`'s `reserve()` [8](#0-7) .

### Impact Explanation
A portion of a user's balance can become permanently locked in the `reserved` state with no on-chain path to free it, matching the "permanent user-fund lock" impact category.

### Likelihood Explanation
Requires that a lock/freeze reduce the source's spendable-under-freeze reserved balance between `create_swap` and `claim_swap` — achievable via ordinary public extrinsics (e.g., vesting, staking bonding, governance locks) that a source account can trigger on itself, or that occur incidentally in normal chain usage.

### Recommendation
In `BalanceSwapAction::claim`, use `Precision::Exact` (or otherwise verify the full `value` was moved, e.g., check the returned shortfall is zero) before reporting success, and if not fully claimable, avoid deleting the `PendingSwaps` entry (or provide a recovery path) so the source can still `cancel_swap` for the unclaimed remainder.

### Proof of Concept
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

**File:** substrate/frame/balances/src/impl_currency.rs (L725-734)
```rust
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
