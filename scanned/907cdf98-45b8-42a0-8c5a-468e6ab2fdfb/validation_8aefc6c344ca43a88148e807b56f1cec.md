### Title
Storage deposit refunds in `pallet-revive` can permanently fail and lock user funds under hold with no fallback return path - ([File: substrate/frame/revive/src/lib.rs])

### Summary
`pallet-revive` charges a storage deposit from a contract-call origin and places it on `hold_reason` `HoldReason::StorageDepositReserve` against the contract account whenever storage grows. When storage shrinks (or a contract terminates), the deposit is supposed to be refunded back to the origin. If that refund fails, the pallet simply converts the failure into a `DispatchError` and gives up — there is no code path that returns the held funds to the depositor by any other means. This mirrors the reported bug class: funds are taken up-front (`charge_deposit`/`charge_and_hold`) but the corresponding "give the money back" step (`refund_deposit`/`refund_on_hold`) can permanently fail, and unlike the ERC-20 `Endpoint` fix (`tryReturnFunds`), there is no compensating mechanism here.

### Finding Description
Deposits are taken via `Pallet::<T>::charge_deposit`, which calls `T::Deposit::charge_and_hold` to place the amount on hold from the depositing origin onto the contract account: [1](#0-0) 

Refunds go through `Pallet::<T>::refund_deposit`, which calls `T::Deposit::refund_on_hold`. If that call fails, the error is mapped to either `Error::<T>::StorageRefundNotEnoughFunds` (the code’s own comment says this “would be a straight up bug in this pallet”) or `Error::<T>::StorageRefundLocked` (when other locks, e.g. staking/governance conviction voting, prevent moving the held balance): [2](#0-1) 

The underlying `refund_on_hold` implementation in `deposit_payment.rs` performs a plain `transfer_on_hold`/`release` + `withdraw`, with no retry, no partial-refund fallback, and no alternate beneficiary or "trapped funds" mechanism if the transfer fails: [3](#0-2) [4](#0-3) 

Because the whole dispatchable (the `call`/`instantiate`/terminate extrinsic that triggers the refund) is transactional, a failed refund rolls back the entire extrinsic — including the storage shrink or contract termination that was supposed to free the deposit. The practical effect: the origin can never shrink storage or terminate the contract while the hold cannot be moved (e.g. because the contract account's balance is locked by another pallet, such as staking or conviction-voting, exactly as the warning log in `refund_deposit` describes), and the deposit stays reserved under `HoldReason::StorageDepositReserve` indefinitely. There is no `tryReturnFunds`-style compensating transfer, no queued/deferred refund, and no way for the depositor to reclaim the value by any other path in this pallet.

### Impact Explanation
This falls under the "permanent user-fund lock" category: value that was legitimately charged from a signed origin becomes unrecoverable through any exposed extrinsic once the refund path hits a lock or accounting mismatch. Unlike overweight/failed message handling in `pallet-message-queue` (which is explicitly transactional and retried, see `pr_5198.prdoc`) or the Snowbridge inbound queue's XCM trap-and-claim fallback (`AssetsTrapped` + `claim_assets`), `pallet-revive`'s deposit refund has no such recovery mechanism — the funds are simply stuck.

### Likelihood Explanation
Triggering `StorageRefundLocked` does not require a malicious peer, validator, or governance actor; an ordinary account can lock its own balance (e.g., by using the contract-holding account, or an account it controls, in staking or conviction-voting) while a storage-deposit hold from `pallet-revive` is outstanding, then attempt to shrink storage or terminate the contract. Reaching `StorageRefundNotEnoughFunds` requires an accounting desync, which the code's own comment flags as a pallet bug rather than expected operator error, indicating this path is reachable through normal (non-adversarial) execution once holds and locks interact.

### Recommendation
Add a fallback for failed refunds analogous to the referenced patch's `tryReturnFunds`: if `refund_on_hold` fails, do not silently error out of the extrinsic and leave the hold intact with no exit — either (a) defer the refund into a claimable/queued state tied to the origin (similar to Snowbridge's trapped-asset claim flow), or (b) make the failure non-fatal for the associated storage mutation so the depositor is not blocked from future release, while emitting an event that lets governance or the account itself reconcile/release the lock and retry the refund later.

### Proof of Concept
1. Instantiate a contract and perform storage writes so that a storage deposit is charged and held against the contract account under `HoldReason::StorageDepositReserve` (see `charge_deposit`).
2. Cause the contract account (or the depositing origin's held funds path) to become locked by another pallet that competes for the same balance (e.g., staking bonding or conviction-voting lock) so that `T::Currency::transfer_on_hold`/`release` cannot move the reserved amount.
3. Perform a call that shrinks storage or terminates the contract, triggering `refund_deposit` → `T::Deposit::refund_on_hold`.
4. Observe that `refund_on_hold` fails, `refund_deposit` maps this to `Error::<T>::StorageRefundLocked`, and the whole extrinsic (including the storage shrink/termination) is rolled back — the deposit remains held indefinitely with no path in the pallet to return it to the depositor. [5](#0-4)

### Citations

**File:** substrate/frame/revive/src/lib.rs (L2774-2788)
```rust
	fn charge_deposit(
		hold_reason: HoldReason,
		from: &T::AccountId,
		to: &T::AccountId,
		amount: BalanceOf<T>,
		exec_config: &ExecConfig<T>,
	) -> DispatchResult {
		if amount.is_zero() {
			return Ok(());
		}

		T::Deposit::charge_and_hold(hold_reason, exec_config.funds(from), to, amount)
			.map_err(|_| Error::<T>::StorageDepositNotEnoughFunds)?;
		Ok(())
	}
```

**File:** substrate/frame/revive/src/lib.rs (L2790-2831)
```rust
	/// Refund a deposit.
	///
	/// `dst` is usually the transaction origin and `from` a contract or
	/// the pallets own account.
	fn refund_deposit(
		hold_reason: HoldReason,
		from: &T::AccountId,
		dst: deposit_payment::Funds<T::AccountId>,
		amount: BalanceOf<T>,
	) -> Result<(), DispatchError> {
		if amount.is_zero() {
			return Ok(());
		}

		let to = match &dst {
			deposit_payment::Funds::Balance(to) | deposit_payment::Funds::TxFee(to) => *to,
		};
		let result = T::Deposit::refund_on_hold(hold_reason, from, dst, amount);

		result.defensive_map_err(|err| {
			let available = T::Deposit::total_on_hold(hold_reason, from);
			if available < amount {
				// The storage deposit accounting got out of sync with the balance: This would be a
				// straight up bug in this pallet.
				log::error!(
					target: LOG_TARGET,
					"Failed to refund storage deposit {amount:?} from contract {from:?} to origin {to:?}. Not enough deposit: {available:?}. This is a bug.",
				);
				Error::<T>::StorageRefundNotEnoughFunds.into()
			} else {
				// There are some locks preventing the refund. This could be the case if the
				// contract participates in government. The consequence is that if a contract votes
				// with its storage deposit it would no longer be possible to remove storage without first
				// reducing the lock.
				log::warn!(
					target: LOG_TARGET,
					"Failed to refund storage deposit {amount:?} from contract {from:?} to origin {to:?}: {err:?}. First remove locks (staking, governance) from the contracts account.",
				);
				Error::<T>::StorageRefundLocked.into()
			}
		})
	}
```

**File:** substrate/frame/revive/src/deposit_payment.rs (L212-244)
```rust
	fn refund_on_hold(
		reason: HoldReason,
		from: &T::AccountId,
		dst: Funds<T::AccountId>,
		amount: BalanceOf<T>,
	) -> DispatchResult {
		match dst {
			Funds::Balance(to) => {
				T::Currency::transfer_on_hold(
					&reason.into(),
					from,
					to,
					amount,
					Precision::Exact,
					Restriction::Free,
					Fortitude::Polite,
				)?;
			},
			Funds::TxFee(_) => {
				let released =
					T::Currency::release(&reason.into(), from, amount, Precision::Exact)?;
				let credit = T::Currency::withdraw(
					from,
					released,
					Precision::Exact,
					Preservation::Preserve,
					Fortitude::Polite,
				)?;
				T::FeeInfo::deposit_txfee(credit);
			},
		}
		Ok(())
	}
```

**File:** substrate/frame/revive/src/deposit_payment.rs (L377-412)
```rust
	/// Refunds native currency first (capped by [`NativeDepositOf`]); any shortfall is taken from
	/// PGAS with `RefundPercent` refunded and the rest burned. When `dst` is [`Funds::TxFee`],
	/// the native portion is routed into the tx fee pool instead of the embedded account's
	/// free balance. The PGAS portion (if any) is always settled to the account embedded in
	/// `dst`.
	///
	/// Note: callers must run inside a storage layer so partial state rolls back on error.
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
