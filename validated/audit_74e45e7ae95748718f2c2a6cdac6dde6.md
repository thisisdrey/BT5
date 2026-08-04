### Title
`ERC20Transactor` treats non-boolean-returning ERC-20 `transfer` as an XCM failure after the underlying token transfer has already succeeded, permanently stranding user funds in the checking account - ([File: cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs])

### Summary
`ERC20Transactor::withdraw_asset_with_surplus` and `deposit_asset_with_surplus` invoke an arbitrary contract at `asset_id`/`asset_contract_id` (resolved from an XCM `Asset` via `Matcher::matches_fungibles`) using a hard-coded `IERC20::transferCall` ABI call, and then strictly require the call to abi-decode a `bool` return value via `abi_decode_returns_validate` to consider the transfer successful. [1](#0-0) 

This is the same class of bug as the external report: code assumes every ERC-20-like contract exactly implements the OpenZeppelin reference semantics (specifically, that `transfer()` always returns an ABI-encoded `bool`), even though the contract address is attacker/asset-issuer supplied and not guaranteed to conform. Non-conforming but widely used tokens (e.g. USDT-style tokens that return no data from `transfer`) will pass token-level execution (balances actually move) but fail the strict decode check here.

### Finding Description
`withdraw_asset_with_surplus` calls `pallet_revive::Pallet::<T>::bare_call` against the registered ERC20 contract address to move `amount` from `who` to `TransfersCheckingAccount`. [2](#0-1) 

If the call does not revert, the code unconditionally attempts to `abi_decode_returns_validate` the return data as `IERC20::transferCall`'s `bool` return type: [3](#0-2) 

Any ERC-20 implementation that does not return exactly a 32-byte encoded boolean from `transfer` (empty return data, a different type, or any other non-EIP20-strict encoding) will make `abi_decode_returns_validate` return `Err`, which is mapped to `XcmError::FailedToTransactAsset("ERC20 contract result couldn't decode")`. Crucially, this happens **after** the underlying contract call already executed and (for a functioning, non-reverting token) already transferred the real balance from `who` to `TransfersCheckingAccount`. Because the function returns an `Err` instead of an `AssetsInHolding` credit, the XCM executor believes the withdrawal never happened — no `AssetsInHolding` is produced, so no corresponding `deposit_asset_with_surplus` (or any refund path) will ever be triggered for those tokens. The tokens are physically now owned by `TransfersCheckingAccount` (a pallet-controlled account) but the XCM/asset-accounting state has zero record of holding them, and there is no compensating mechanism in this code path to detect or recover this class of stuck balance.

The identical failure mode applies symmetrically in `deposit_asset_with_surplus`, which transfers from `TransfersCheckingAccount` to the beneficiary and requires the same strict boolean decode; a non-conforming token can leave funds it already delivered to the checking account, with the deposit reported as failed and the `AssetsInHolding` handed back to the caller as if nothing was transferred — while the real ERC-20 balance was already moved on-chain in `withdraw_asset_with_surplus`'s corresponding leg. [4](#0-3) 

This is structurally the same broken invariant as the reported Endaoment bug: relying on a specific concrete ERC-20 implementation's exact return-value semantics for an address that is not guaranteed to match it, instead of tolerating standard-compliant variance, resulting in funds becoming unrecoverable.

### Impact Explanation
This breaks the "message queues... and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically" invariant: on-chain token custody state (real ERC-20 balances) and XCM-level `AssetsInHolding` accounting can diverge irrecoverably. Any parachain registering a foreign/local ERC20 asset backed by a non-strictly-EIP20-compliant token (which is common in the wild) can have user funds permanently locked in `TransfersCheckingAccount` with no code path to reclaim them, matching the "permanent user-fund or bridge-state lock" impact category in the gate.

### Likelihood Explanation
This is triggerable by any unprivileged user performing an ordinary asset transfer/reserve-transfer through XCM once the asset (an arbitrary ERC-20 contract address) is registered via the normal `Matcher::matches_fungibles` configuration for the chain (this transactor is wired up in asset-hub-westend's `xcm_config.rs` alongside the pallet-revive ERC20 precompiles). No malicious peer, validator, relayer, or governance action is required — only the existence of a widely-used non-strict ERC-20 token (e.g. `transfer` returning no data) registered as an asset, which is a realistic and common real-world condition, not a contrived edge case. [5](#0-4) 

### Recommendation
Do not require a strict ABI-decoded `bool` return for ERC-20 `transfer`/interactions in `ERC20Transactor`. Follow the well-established safe-ERC20 pattern: treat a non-reverting call with empty return data as success, and only fail on an explicit `false` return or a revert. Additionally, ensure that if the call result cannot be safely interpreted, the function does not silently drop custody of tokens that have already moved — e.g., verify balances before/after the call rather than trusting only the return-value decode, so state accounting always matches actual token custody.

### Proof of Concept
1. Deploy (or register as a foreign asset) an ERC-20 contract whose `transfer(address,uint256)` function performs the balance update but returns no data (a common non-EIP20-strict but widely deployed pattern).
2. Register this contract's address as the backing asset for some `Asset` location recognized by `Matcher::matches_fungibles` on a chain using `ERC20Transactor` (as wired in `asset-hub-westend`'s XCM config).
3. Submit an ordinary XCM instruction (e.g. `TransferAsset`/`WithdrawAsset`) that causes `withdraw_asset_with_surplus` to be invoked for this asset from an account `who` holding a balance.
4. `bare_call` executes the token's `transfer`, moving `amount` from `who` to `TransfersCheckingAccount` on-chain (token-level success, no revert).
5. `IERC20::transferCall::abi_decode_returns_validate(&return_value.data)` fails because there is no return data to decode `bool` from.
6. The function returns `XcmError::FailedToTransactAsset("ERC20 contract result couldn't decode")`; no `AssetsInHolding` is created, and the XCM executor treats the withdrawal as never having occurred.
7. `who`'s tokens are now permanently held by `TransfersCheckingAccount` with no corresponding XCM state or user-facing recovery path — funds are stuck.

### Citations

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L109-130)
```rust
impl<
		AccountId: Eq + Clone,
		T: pallet_revive::Config<AccountId = AccountId>,
		AccountIdConverter: ConvertLocation<AccountId>,
		Matcher: MatchesFungibles<H160, u128>,
		WeightLimit: Get<Weight>,
		StorageDepositLimit: Get<BalanceOf<T>>,
		TransfersCheckingAccount: Get<AccountId>,
	> TransactAsset
	for ERC20Transactor<
		T,
		Matcher,
		AccountIdConverter,
		WeightLimit,
		StorageDepositLimit,
		AccountId,
		TransfersCheckingAccount,
	>
where
	BalanceOf<T>: Into<U256> + TryFrom<U256>,
	MomentOf<T>: Into<U256>,
	T::Hash: frame_support::traits::IsType<H256>,
```

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L166-208)
```rust
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
		// We need to return this surplus for the executor to allow refunding it.
		let surplus = weight_limit.saturating_sub(weight_consumed);
		tracing::trace!(target: "xcm::transactor::erc20::withdraw", ?weight_consumed, ?surplus, ?storage_deposit);
		if let Ok(return_value) = result {
			tracing::trace!(target: "xcm::transactor::erc20::withdraw", ?return_value, "Return value by withdraw_asset");
			if return_value.did_revert() {
				tracing::debug!(target: "xcm::transactor::erc20::withdraw", "ERC20 contract reverted");
				Err(XcmError::FailedToTransactAsset("ERC20 contract reverted"))
			} else {
				let is_success = IERC20::transferCall::abi_decode_returns_validate(&return_value.data).map_err(|error| {
					tracing::debug!(target: "xcm::transactor::erc20::withdraw", ?error, "ERC20 contract result couldn't decode");
					XcmError::FailedToTransactAsset("ERC20 contract result couldn't decode")
				})?;
				if is_success {
					tracing::trace!(target: "xcm::transactor::erc20::withdraw", "ERC20 contract was successful");
					Ok((
						AssetsInHolding::new_from_fungible_credit(
							what.id.clone(),
							Box::new(Erc20Credit(amount)),
						),
						surplus,
					))
				} else {
					tracing::debug!(target: "xcm::transactor::erc20::withdraw", "contract transfer failed");
					Err(XcmError::FailedToTransactAsset("ERC20 contract transfer failed"))
				}
			}
```

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L253-298)
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
		// We need to return this surplus for the executor to allow refunding it.
		let surplus = weight_limit.saturating_sub(weight_consumed);
		tracing::trace!(target: "xcm::transactor::erc20::deposit", ?weight_consumed, ?surplus, ?storage_deposit);
		if let Ok(return_value) = result {
			tracing::trace!(target: "xcm::transactor::erc20::deposit", ?return_value, "Return value");
			if return_value.did_revert() {
				tracing::debug!(target: "xcm::transactor::erc20::deposit", "Contract reverted");
				Err((what, XcmError::FailedToTransactAsset("ERC20 contract reverted")))
			} else {
				match IERC20::transferCall::abi_decode_returns_validate(&return_value.data) {
					Ok(true) => {
						tracing::trace!(target: "xcm::transactor::erc20::deposit", "ERC20 contract was successful");
						Ok(surplus)
					},
					Ok(false) => {
						tracing::debug!(target: "xcm::transactor::erc20::deposit", "contract transfer failed");
						Err((
							what,
							XcmError::FailedToTransactAsset("ERC20 contract transfer failed"),
						))
					},
					Err(error) => {
						tracing::debug!(target: "xcm::transactor::erc20::deposit", ?error, "ERC20 contract result couldn't decode");
						Err((
							what,
							XcmError::FailedToTransactAsset(
								"ERC20 contract result couldn't decode",
							),
						))
					},
				}
			}
```
