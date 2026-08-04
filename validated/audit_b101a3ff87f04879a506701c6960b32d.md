## Title
Hardcoded `WeightLimit`/`StorageDepositLimit` in `ERC20Transactor` can cause XCM deposits to fail and trap user funds - (File: `cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs`)

### Summary
`ERC20Transactor` is a `TransactAsset` implementation used by the XCM executor to move ERC20 balances by invoking `pallet_revive::Pallet::bare_call` on the underlying Solidity `transfer` function. Both `withdraw_asset_with_surplus` and `deposit_asset_with_surplus` call `bare_call` with a fixed, config-time constant `weight_limit = WeightLimit::get()` and `deposit_limit = StorageDepositLimit::get()`, exactly the class of bug described in the external report (hardcoded gas/weight limit instead of a per-call estimate). [1](#0-0) [2](#0-1) 

### Finding Description
`withdraw_asset_with_surplus` moves the token to a checking account by calling the ERC20 `transfer` function through `pallet_revive::Pallet::bare_call` with a statically configured `weight_limit`/`deposit_limit`: [3](#0-2) 

`deposit_asset_with_surplus` performs the mirrored transfer from the checking account to the beneficiary, again using the same hardcoded `WeightLimit::get()`: [2](#0-1) 

The code itself acknowledges the exact bug class from the external report — that a caller has no way to work around an insufficient hardcoded resource limit: [4](#0-3) [5](#0-4) 

Because `WeightLimit` and `StorageDepositLimit` are fixed `Get<Weight>`/`Get<Balance>` constants configured once in the runtime (via generic parameters on `ERC20Transactor` in `asset-hub-westend/src/xcm_config.rs`), they cannot adapt to:
- ERC20 contracts with more expensive `transfer` logic (fee-on-transfer tokens, hooks, proxies, upgradeable/delegatecall wrappers),
- increased per-byte/storage-deposit costs from runtime parameter changes,
- worst-case first-time storage writes on the beneficiary/checking account.

If the actual weight/storage-deposit required by the ERC20 `transfer` call exceeds the hardcoded limit, `bare_call` returns an execution error (out-of-resource), and both functions map this to `XcmError::FailedToTransactAsset`. In `withdraw_asset_with_surplus`, a failure simply aborts the XCM instruction (no funds moved yet). But in `deposit_asset_with_surplus`, a resource-exhaustion failure occurs **after** the corresponding withdraw has already succeeded and the asset was already recorded as `AssetsInHolding` for the beneficiary elsewhere in the message. When `deposit_asset` returns `Err((what, XcmError))`, the standard XCM-executor path either bounces to `deposit_asset` retry logic or ultimately traps the assets (`AssetTrap`) if no beneficiary account can receive them, since the transactor itself provides no fallback beyond returning the held assets to the executor.

Existing guards do not stop this path:
- There is no dynamic gas/weight estimation equivalent to `eth_estimateGas` before the `bare_call` (unlike `pallet_revive`'s own dry-run/binary-search estimator used for `eth_estimateGas`, seen at [6](#0-5) ).
- `TransactionLimits::WeightAndDeposit` is fed directly from the constant `Get` implementations with no headroom or fallback to a higher configured max.
- The `surplus` calculation (`weight_limit.saturating_sub(weight_consumed)`) only refunds unused weight on **success**; it provides no remedy when the call fails due to insufficient weight.

### Impact Explanation
An unprivileged actor (or simply normal usage of a non-trivial ERC20 token, e.g. with transfer hooks, blacklist checks, or fee logic) whose `transfer` call requires more weight/deposit than the hardcoded constants triggers `FailedToTransactAsset` in `deposit_asset_with_surplus` after the funds have already left the source side of the transfer. This can result in assets being trapped by the XCM executor (`AssetTrap`) instead of reaching the intended beneficiary — a form of "public underpriced work" causing failed delivery and fund lock/loss, aligned with the "permanent user-fund ... lock" and "message queues ... must only advance after decode, dispatch, execution, and settlement succeed atomically" impact criteria. It does not require a malicious peer, validator, or governance actor — only normal XCM usage with an ERC20 asset whose gas needs exceed the fixed configuration.

### Likelihood Explanation
Likelihood is moderate: it requires an ERC20 contract registered for XCM transfer whose `transfer()` implementation consumes more resources than the statically configured `WeightLimit`/`StorageDepositLimit` (e.g., due to non-trivial hooks, first-time storage writes triggering higher deposit needs, or a future increase in per-byte deposit costs via runtime upgrade). This is a realistic, non-adversarial trigger — no attacker collusion needed, and the comment in the code itself concedes that "there's nothing the user can change in the XCM that will make this work since there's a hardcoded gas limit."

### Recommendation
Replace the hardcoded `WeightLimit`/`StorageDepositLimit` constants with a dry-run/estimation step (analogous to `pallet_revive::Pallet::eth_estimate_gas`) before performing the real `bare_call`, or at minimum use `max(HARDCODED_LIMIT, estimated_requirement)` as recommended in the original report. Additionally, ensure that `deposit_asset_with_surplus` failures due to resource exhaustion are distinguished from genuine contract-logic failures so that retriable/adjustable-resource failures do not immediately fall through to asset trapping.

### Proof of Concept
1. Configure `ERC20Transactor<..., WeightLimit, StorageDepositLimit, ...>` in a runtime's XCM config with a `WeightLimit`/`StorageDepositLimit` sized for a simple ERC20 `transfer`.
2. Register (via `Matcher`) an ERC20 contract whose `transfer` implementation performs extra storage writes or hook calls (e.g., a fee-on-transfer or upgradeable proxy token), such that actual weight/storage-deposit needed exceeds the hardcoded limit only in some transfer paths (e.g., first transfer to a new address).
3. Submit a cross-chain XCM message that withdraws the asset from a source account (succeeds, since the checking-account transfer path is cheap) and deposits it to a fresh beneficiary address whose first-time storage write exceeds `StorageDepositLimit`.
4. Observe `bare_call` in `deposit_asset_with_surplus` returning an execution error; `TransactAsset::deposit_asset_with_surplus` returns `Err((what, XcmError::FailedToTransactAsset(...)))` after the withdraw side already completed, leading the XCM executor to trap the `AssetsInHolding` since the beneficiary never received the actual ERC20 balance. [5](#0-4)

### Citations

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L163-181)
```rust
		let checking_account_eth = T::AddressMapper::to_address(&TransfersCheckingAccount::get());
		let checking_address = Address::from(Into::<[u8; 20]>::into(checking_account_eth));
		let weight_limit = WeightLimit::get();
		// To withdraw, we actually transfer to the checking account.
		// We do this using the solidity ERC20 interface.
		let data =
			IERC20::transferCall { to: checking_address, value: EU256::from(amount) }.abi_encode();
		let ContractResult { result, weight_consumed, storage_deposit, .. } =
			pallet_revive::Pallet::<T>::bare_call(
				OriginFor::<T>::signed(who.clone()),
				asset_id,
				U256::zero(),
				TransactionLimits::WeightAndDeposit {
					weight_limit,
					deposit_limit: StorageDepositLimit::get(),
				},
				data,
				&ExecConfig::new_substrate_tx(),
			);
```

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L209-215)
```rust
		} else {
			tracing::debug!(target: "xcm::transactor::erc20::withdraw", ?result, "Error");
			// This error could've been duplicate smart contract, out of gas, etc.
			// If the issue is gas, there's nothing the user can change in the XCM
			// that will make this work since there's a hardcoded gas limit.
			Err(XcmError::FailedToTransactAsset("ERC20 contract execution errored"))
		}
```

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L253-266)
```rust
		let data = IERC20::transferCall { to: address, value: EU256::from(amount) }.abi_encode();
		let weight_limit = WeightLimit::get();
		let ContractResult { result, weight_consumed, storage_deposit, .. } =
			pallet_revive::Pallet::<T>::bare_call(
				OriginFor::<T>::signed(TransfersCheckingAccount::get()),
				asset_contract_id,
				U256::zero(),
				TransactionLimits::WeightAndDeposit {
					weight_limit,
					deposit_limit: StorageDepositLimit::get(),
				},
				data,
				&ExecConfig::new_substrate_tx(),
			);
```

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L299-305)
```rust
		} else {
			tracing::debug!(target: "xcm::transactor::erc20::deposit", ?result, "Error");
			// This error could've been duplicate smart contract, out of gas, etc.
			// If the issue is gas, there's nothing the user can change in the XCM
			// that will make this work since there's a hardcoded gas limit.
			Err((what, XcmError::FailedToTransactAsset("ERC20 contract execution errored")))
		}
```

**File:** substrate/frame/revive/src/lib.rs (L1936-1960)
```rust
	///
	/// This function estimates the gas of the transaction according to the same binary search
	/// algorithm that's implemented in Geth. It stops when with an acceptable error ratio of
	/// 1.5% so that the algorithm terminates early.
	///
	/// # Note
	///
	/// All calls to [`Self::dry_run_eth_transact`] need to happen inside of a [`with_transaction`]
	/// with state rollback to ensure that dry runs subsequent to the first one preserve the correct
	/// amount of storage deposits needed without any kind of caching from the previous dry runs.
	pub fn eth_estimate_gas(
		tx: GenericTransaction,
		timestamp_override: Option<MomentOf<T>>,
		state_overrides: Option<StateOverrideSet>,
	) -> Result<U256, EthTransactError>
	where
		T::Nonce: Into<U256> + TryFrom<U256>,
		CallOf<T>: SetWeightLimit,
	{
		log::debug!(target: LOG_TARGET, "eth_estimate_gas: {tx:?}");

		let mut low = U256::zero();
		let mut high = Self::evm_block_gas_limit();

		log::trace!(target: LOG_TARGET, "eth_estimate_gas starting with low={low}, high={high}");
```
