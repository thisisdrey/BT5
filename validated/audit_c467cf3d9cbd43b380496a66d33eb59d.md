Confirmed: `drop_history` is a permissionless call (`_origin: OriginFor<T>`, any kind of origin) [1](#0-0) , and `do_drop_history` takes the `InstaPoolHistory` record unconditionally, discards the `Result` of `Self::charge` via `let _ = ...`, then emits `Event::HistoryDropped` with the claimed revenue amount regardless of whether the withdrawal succeeded [2](#0-1) . `Self::charge` performs a fallible `T::Currency::withdraw` before calling `T::OnRevenue::on_unbalanced`, so a withdrawal failure (e.g., insufficient free balance under `Exact`/`Expendable`/`Polite` constraints) is silently swallowed [3](#0-2) . This matches the claim's description precisely.

Audit Report

## Title
`do_drop_history` silently discards the `charge` error while still emitting `HistoryDropped` and consuming the payout record - ([File: substrate/frame/broker/src/dispatchable_impls.rs])

## Summary
`Pallet::<T>::do_drop_history` unconditionally removes the `InstaPoolHistory` record via `take`, attempts to pay out `record.maybe_payout` through `Self::charge`, but discards any error from that fallible call with `let _ = ...`. It then emits `Event::HistoryDropped { when, revenue }` announcing the payout regardless of whether `charge` actually succeeded, and returns `Ok(())`.

## Finding Description
`do_drop_history` calls `InstaPoolHistory::<T>::take(when)`, which unconditionally and destructively removes the stored record. If `record.maybe_payout` is `Some(payout)`, it calls `Self::charge(&Self::account_id(), payout)` but throws away the result with `let _ = ...` [4](#0-3) . `Self::charge` performs `T::Currency::withdraw(&who, amount, Exact, Expendable, Polite)?` before forwarding the credit to `T::OnRevenue::on_unbalanced` [3](#0-2) ; the `?` means a withdraw failure short-circuits `charge` with an `Err`, which the caller then ignores. The function proceeds to compute `revenue` from the very same `maybe_payout` and emits `Event::HistoryDropped { when, revenue }` unconditionally, then returns `Ok(())` [5](#0-4) . Since `drop_history` is dispatched with `_origin: OriginFor<T>` — documented as "Can be any kind of origin" — it is callable by any unprivileged, unsigned-or-signed account [1](#0-0) , and is gated only by the `contribution_timeout` check, a routine, permissionless cleanup condition rather than a rare edge case [6](#0-5) .

## Impact Explanation
If `T::Currency::withdraw` fails (e.g., the broker pallet account's free/withdrawable balance is insufficient at the time of the call under `Exact`/`Expendable`/`Polite` constraints), `T::OnRevenue::on_unbalanced` is never invoked, meaning the revenue is never actually forwarded to its beneficiary (typically a treasury/broker revenue recipient). Yet the `InstaPoolHistory` record has already been irreversibly removed via `take`, and `Event::HistoryDropped` asserts the payout of `revenue` occurred. This is a duplicate/incorrect settlement bug: on-chain state and the event log claim payout succeeded while the underlying value transfer silently failed, permanently losing track of that revenue with no possibility of retry, since the source record no longer exists.

## Likelihood Explanation
`drop_history` requires no privileged origin and is reachable by any caller once `status.last_timeslice > when + contribution_timeout`, which occurs under normal, expected chain operation. The failure mode of `charge` (pallet account's free balance being transiently insufficient relative to the recorded payout, due to normal broker pallet spending elsewhere) does not require a malicious validator, governance action, or compromised infrastructure — only ordinary pallet-account balance fluctuations.

## Recommendation
Propagate the error from `Self::charge` instead of discarding it, e.g. replace `let _ = Self::charge(...)` with `Self::charge(...)?`, and avoid destructively removing the `InstaPoolHistory` record before the charge succeeds (or restore/re-insert it on charge failure), so that state advancement and the `HistoryDropped` event are only emitted once settlement has actually succeeded.

## Proof of Concept
1. Set up an `InstaPoolHistory` entry for timeslice `when` with `maybe_payout = Some(payout)`.
2. Drain the broker pallet account (`Pallet::<T>::account_id()`) of free balance below `payout` through normal pallet operations before `contribution_timeout` elapses.
3. Once `status.last_timeslice > when + contribution_timeout`, call `drop_history(when)` from any origin.
4. `InstaPoolHistory::<T>::take(when)` removes the record; `Self::charge` fails internally in `T::Currency::withdraw`, but the error is discarded.
5. `Event::HistoryDropped { when, revenue: payout }` is emitted and the extrinsic succeeds, even though `T::OnRevenue::on_unbalanced` was never called — the payout amount is lost with no way to recover it, since the source record is gone. This can be verified as a Rust unit test in `substrate/frame/broker/src/tests.rs` mirroring the existing `drop_history` tests, but arranging the pallet account's balance to be insufficient at call time and asserting that `HistoryDropped` is still emitted despite the missing transfer.

### Citations

**File:** substrate/frame/broker/src/lib.rs (L893-901)
```rust
		/// Drop an expired Instantaneous Pool History record from the chain.
		///
		/// - `origin`: Can be any kind of origin.
		/// - `region_id`: The time of the Pool History record which has expired.
		#[pallet::call_index(16)]
		pub fn drop_history(_origin: OriginFor<T>, when: Timeslice) -> DispatchResultWithPostInfo {
			Self::do_drop_history(when)?;
			Ok(Pays::No.into())
		}
```

**File:** substrate/frame/broker/src/dispatchable_impls.rs (L511-525)
```rust
	pub(crate) fn do_drop_history(when: Timeslice) -> DispatchResult {
		let config = Configuration::<T>::get().ok_or(Error::<T>::Uninitialized)?;
		let status = Status::<T>::get().ok_or(Error::<T>::Uninitialized)?;
		ensure!(
			status.last_timeslice > when.saturating_add(config.contribution_timeout),
			Error::<T>::StillValid
		);
		let record = InstaPoolHistory::<T>::take(when).ok_or(Error::<T>::NoHistory)?;
		if let Some(payout) = record.maybe_payout {
			let _ = Self::charge(&Self::account_id(), payout);
		}
		let revenue = record.maybe_payout.unwrap_or_default();
		Self::deposit_event(Event::HistoryDropped { when, revenue });
		Ok(())
	}
```

**File:** substrate/frame/broker/src/utility_impls.rs (L68-72)
```rust
	pub(crate) fn charge(who: &T::AccountId, amount: BalanceOf<T>) -> DispatchResult {
		let credit = T::Currency::withdraw(&who, amount, Exact, Expendable, Polite)?;
		T::OnRevenue::on_unbalanced(credit);
		Ok(())
	}
```
