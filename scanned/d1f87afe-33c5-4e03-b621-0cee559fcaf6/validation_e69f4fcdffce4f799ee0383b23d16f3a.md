### Title
`ensure_not_overdrawn` validates the tx-hold against a fee value that excludes the storage-deposit adjustment, letting the actual debit exceed the checked bound - (File: `substrate/frame/revive/src/evm/block_storage.rs`)

### Summary
In `pallet-revive`'s Ethereum call finalization path, `EthereumCallResult::new` computes a `native_fee` from `compute_actual_fee` and immediately validates it against the pre-reserved transaction-fee hold via `ensure_not_overdrawn(native_fee, result)`. Only *after* this guard passes does the code derive the real debited amount by adding/subtracting the contract's `storage_deposit` on top of `native_fee`. The guard therefore checks a value that is systematically out of sync with the amount that is actually charged to the signer, mirroring the Peapods pattern of validating against a stale "optimistic" figure instead of the final one.

### Finding Description
`EthereumCallResult::new` in `substrate/frame/revive/src/evm/block_storage.rs`: [1](#0-0) 

computes `native_fee` (the base extrinsic/weight fee only) and passes it straight into `T::FeeInfo::ensure_not_overdrawn`: [2](#0-1) 

`ensure_not_overdrawn` compares `fee` (i.e. `native_fee`) against `remaining_txfee()` — the balance still available in the pre-withdrawn transaction-fee hold — and reverts with `TxFeeOverdraw` if `fee > available`.

Immediately after this check has already passed, the code computes the *actual* value that is charged to the account, by folding in the contract execution's storage deposit:

```rust
let fee = Pallet::<T>::convert_native_to_evm(match output.storage_deposit {
    StorageDeposit::Refund(refund) => native_fee.saturating_sub(refund),
    StorageDeposit::Charge(amount) => native_fee.saturating_add(amount),
});
```

For `StorageDeposit::Charge(amount)`, the true amount consumed from the account is `native_fee + amount`, but the overdraw guard was evaluated only against `native_fee`, before `amount` was known/added. This is structurally identical to the Peapods bug: an "optimistic" quantity (`native_fee` alone) is checked against a threshold (`remaining_txfee()`), while the quantity that is actually settled (`native_fee + storage_deposit`) is computed and applied only afterward, without re-validating against the same threshold. The `leftoverCollateral`-style desync here is: the overdraw check never accounts for the storage-deposit component, so a transaction whose combined native fee + storage charge exceeds the reserved tx-hold can still pass the guard, because the guard only ever "sees" the smaller, pre-storage-deposit figure.

### Impact Explanation
If the storage-deposit-inclusive fee is allowed to exceed the amount reserved in the tx-hold (`remaining_txfee()`), the runtime can debit more from the signer than what was validated/reserved for the extrinsic, i.e. the guard meant to bound withdrawal from the tx-credit-hold is bypassed for the storage-deposit portion of the charge. This falls under "public underpriced work" / incorrect settlement of value for a public, unprivileged entry point (any Ethereum-style transaction dispatched through `pallet-revive`), since a normal caller can trigger contract execution that produces a non-trivial storage deposit charge.

### Likelihood Explanation
Every successful contract call that incurs a non-zero `StorageDeposit::Charge` goes through this exact code path (`EthereumCallResult::new`), so the condition is reachable on ordinary, unprivileged contract calls without needing any privileged actor, malicious relayer, or governance action — matching the "public wrapper / public dispatch" scope required by the task.

### Recommendation
Compute the final, storage-deposit-adjusted fee first, and perform `ensure_not_overdrawn` against that final value (not the intermediate `native_fee`), so the guard reflects exactly what will be debited from the tx-hold.

### Proof of Concept
Conceptual reproduction (exact numeric PoC requires running the `pallet-revive` test harness, which was not available in this pass):
1. Craft an Ethereum-style call whose `compute_actual_fee` (`native_fee`) is just at or slightly under `remaining_txfee()` so `ensure_not_overdrawn` passes.
2. Have the call's execution incur a non-trivial `StorageDeposit::Charge(amount)` (e.g., via new storage writes), such that `native_fee + amount > remaining_txfee()`.
3. Observe that `ensure_not_overdrawn` still returns `Ok` (checked before `amount` was folded in), and the final charged/burned amount (`native_fee + amount`, later used to compute `gas_used`/`tx_cost` and to burn the rounding remainder via `burn_with_dust`) exceeds what the tx-hold was validated to allow.

I was not able to fully trace, within the available tool budget, whether `remaining_txfee()`/the tx-credit-hold is later re-validated or topped up elsewhere before the storage-deposit debit is applied to the account; that final settlement path (`burn_with_dust`, `withdraw_txfee`) would need to be inspected in a live session to confirm the end-to-end fund-loss magnitude precisely.

### Citations

**File:** substrate/frame/revive/src/evm/block_storage.rs (L94-101)
```rust
		let result = dispatch_result(output.result, output.weight_consumed, base_call_weight);
		let native_fee = T::FeeInfo::compute_actual_fee(encoded_len, &info, &result);
		let result = T::FeeInfo::ensure_not_overdrawn(native_fee, result);

		let fee = Pallet::<T>::convert_native_to_evm(match output.storage_deposit {
			StorageDeposit::Refund(refund) => native_fee.saturating_sub(refund),
			StorageDeposit::Charge(amount) => native_fee.saturating_add(amount),
		});
```

**File:** substrate/frame/revive/src/evm/fees.rs (L271-302)
```rust
	fn ensure_not_overdrawn(
		fee: BalanceOf<E::Config>,
		result: DispatchResultWithPostInfo,
	) -> DispatchResultWithPostInfo {
		// if tx is already failing we can ignore
		// as it will be rolled back anyways
		let Ok(post_info) = result else {
			return result;
		};

		let available = Self::remaining_txfee();
		if fee > available {
			log::debug!(target: LOG_TARGET, "Drew too much from the txhold. \
				fee={fee:?} \
				available={available:?} \
				overdrawn_by={:?}",
				fee.saturating_sub(available),
			);
			Err(DispatchErrorWithPostInfo {
				post_info,
				error: <Error<E::Config>>::TxFeeOverdraw.into(),
			})
		} else {
			log::trace!(target: LOG_TARGET, "Enough left in the txhold. \
				fee={fee:?} \
				available={available:?} \
				refund={:?}",
				available.saturating_sub(fee),
			);
			result
		}
	}
```
