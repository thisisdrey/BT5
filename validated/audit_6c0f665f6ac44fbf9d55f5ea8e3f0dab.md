### Title
`NativeDepositOf` refund cap is not cleared on contract termination, letting a redeployed contract at the same address inherit a stale native-refund entitlement - ([File: substrate/frame/revive/src/deposit_payment.rs])

### Summary
`pallet-revive`'s PGAS storage-deposit backend caps how much of a partial storage-deposit refund is paid out in native currency (vs. PGAS) using the per-contributor map `NativeDepositOf<T>: (holder, user) -> BalanceOf<T>`. This mirrors the audited perps bug exactly: a "reserved" amount (`NativeDepositOf`) is used to gate a refund, but one of the refund paths (`refund_all`, used on contract termination) pays out the native hold in full **without decrementing/clearing** `NativeDepositOf`. If the terminated contract's account is later reused by a new contract instance (address reuse after `selfdestruct`/CREATE2-style redeploy, which `pallet-revive` supports), the stale `NativeDepositOf` entry silently carries over and inflates the native-refund cap for the *new* contract's deposits — letting a user extract more native currency on refund than they actually contributed to the new instance.

### Finding Description
`Deposit::refund_on_hold` for the PGAS backend caps the native portion of a refund by the tracked contribution and decrements it: [1](#0-0) 

This is the intended safety invariant: never refund natively more than `NativeDepositOf[contract][user]` records as actually contributed by that user in native currency.

However, `refund_all` — the path used at contract termination (`do_terminate` calls `T::Deposit::refund_all`) — releases the entire native hold on the contract to the beneficiary, bypassing the per-contributor cap entirely, and never touches `NativeDepositOf`: [2](#0-1) 

Note the doc comment explicitly says "The native cap only makes sense for partial refunds on a live contract; at termination there is one recipient and the contract is gone" — this reasoning assumes the contract account is gone for good. It does not clear `NativeDepositOf` because it assumes there is no future refund that will consult it again for this `(contract, user)` key.

That assumption breaks if the same account address is reused by a new contract instance later (deterministic address derivation from deployer+salt/nonce/code_hash allows a `selfdestruct` followed by redeploy at the same address, a standard EVM/CREATE2 pattern that `pallet-revive`'s termination and instantiation flow does not prevent by clearing this map). When the new contract charges a storage deposit and the payer falls back to native funding, `record_native_deposit` only **adds** to whatever is already stored under that key: [3](#0-2) 

Because the old entry from the terminated contract was never cleared (it was left dangling by `refund_all`), the new contract inherits a residual credit balance in `NativeDepositOf`. Subsequently, when the user requests a partial refund (e.g. via `clear_storage` shrinking the new contract's footprint), `refund_on_hold`'s cap computation (`contribution = NativeDepositOf::get(from, to)`) is inflated by the stale leftover, and the user is refunded natively beyond what they contributed to the current contract instance — the exact "refund computed from stale/unconsumed reserved state, not capped to what remains actually owed for the current position" pattern from the audited report, where `rebalanceClose`'s `finalMarginDelta` used a stale `marginDelta` unconstrained by what had already been withdrawn.

### Impact Explanation
This allows unbacked extraction of native currency (DOT) from the storage-deposit hold of an unrelated (new) contract, funded ultimately by whoever pays that contract's deposits going forward — a direct "theft or unbacked mint/unlock" class impact per the scope: value is settled to the wrong amount because a stale credit key was never invalidated across an account-address's lifecycle boundary (termination → reuse). It also violates the invariant that "deposit/refund state must only advance after decode, dispatch, execution, and settlement succeed atomically" for the *current* deposit lifecycle — the credit from a fully-settled prior lifecycle leaks into the new one.

### Likelihood Explanation
Reachable by any unprivileged user through normal public entry points: deploy a contract, pay a native-fallback storage deposit, `terminate` it (self-destruct, a normal contract operation), redeploy a contract at the same address (deterministic addressing makes this attacker-controlled, not requiring any privileged/admin action), and then trigger a partial refund via ordinary storage-shrinking calls. No malicious validator, relayer, or governance action is needed — this is a pure user-triggerable public-dispatch path (`instantiate`/`call`/`terminate`), matching the gate's "unprivileged attacker … fund loss" criterion. This is a newly introduced PGAS deposit mechanism (per `prdoc/stable2606/pr_11847.prdoc`), so it has not undergone the same scrutiny as the long-standing native-only path.

### Recommendation
Clear (or explicitly zero-out) all `NativeDepositOf` entries keyed by a terminated contract's account in `refund_all`/`destroy_contract`, e.g. `NativeDepositOf::<T>::remove_prefix(contract, None)` (or equivalent bounded removal) before/while releasing the native hold at termination, so a subsequently reused address starts with a clean ledger. Alternatively, prevent address reuse across termination/instantiation, or re-derive/salt addresses to make collisions with a previously-terminated contract impossible.

### Proof of Concept
1. Deploy contract `C` at deterministic address `A` (e.g., via CREATE2-equivalent salt) under the PGAS-backed deposit config.
2. User `U` writes storage to `C`; `U`'s PGAS balance is insufficient, so `charge_and_hold` falls back to native and calls `record_native_deposit(U, A, amount)`, setting `NativeDepositOf[A][U] = amount`.
3. `C` self-destructs (`terminate`). `do_terminate` → `T::Deposit::refund_all(A, beneficiary)` refunds the full native hold to the beneficiary but leaves `NativeDepositOf[A][U] = amount` untouched in storage.
4. A new contract `C'` is instantiated at the same address `A` (same deployer/salt/code_hash after `remove_code`/redeploy, or via any address-reuse mechanism available in `pallet-revive`).
5. User `U'` (could be the same or a different account routed through `U`'s stored credit if `to`/`from` keys collide) funds a native storage deposit to `C'`; `record_native_deposit` adds to the pre-existing (stale) entry rather than starting from zero.
6. `U` (or whoever the stale key belongs to) triggers a partial refund (`clear_storage`) on `C'`; `refund_on_hold`'s `contribution = NativeDepositOf::get(A, U)` includes the stale leftover from step 2, so the native refund cap — and hence the actual amount paid out natively — exceeds what was contributed to `C'` in this lifecycle, extracting excess native currency.

### Citations

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

**File:** substrate/frame/revive/src/deposit_payment.rs (L421-440)
```rust
	/// Refunds the full native hold to `dst` ignoring the per-contributor cap, then settles the
	/// PGAS hold via [`Self::settle_pgas_refund`] (refunding `RefundPercent` to `dst` and burning
	/// the rest). The native cap only makes sense for partial refunds on a live contract; at
	/// termination there is one recipient and the contract is gone.
	///
	/// Note: callers must run inside a storage layer so partial state rolls back on error.
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
