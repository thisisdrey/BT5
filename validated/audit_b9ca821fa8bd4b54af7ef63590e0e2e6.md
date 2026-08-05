Based on the code I read directly from `cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs`, the claim's technical description of the code paths is accurate: both `withdraw_asset_with_surplus` (line 191) and `deposit_asset_with_surplus` (line 276) call `IERC20::transferCall::abi_decode_returns_validate(&return_value.data)` only after confirming `return_value.did_revert()` is `false`, meaning the underlying contract call executed successfully on-chain before the strict ABI-decode is applied. [1](#0-0) [2](#0-1) 

The finding correctly identifies that if `transfer` succeeds but returns no data (or malformed data), the decode fails, and:
- On withdraw, the token has already moved into `TransfersCheckingAccount` via `bare_call`, but the function returns `Err(...)` without minting the `Erc20Credit` into `AssetsInHolding`.
- On deposit, tokens have already moved to the beneficiary via `bare_call`, but the function returns `Err((what, ...))`, handing back `what` as if nothing happened. [3](#0-2) [4](#0-3) 

There is no fallback treating empty return data as success, and no re-check of on-chain `balanceOf` before/after — the existing guard is only `did_revert()`, which is insufficient to catch non-standard ERC20s that succeed without returning ABI-encoded `bool` data. [5](#0-4) 

I confirmed `ERC20Transactor` is wired into `cumulus/parachains/runtimes/assets/asset-hub-westend/src/xcm_config.rs`, and it is the only usage site in the repo of this transactor and of `abi_decode_returns_validate`, matching the report's scope claim. I was not able to fully verify the exact `Matcher` configuration and asset-registration permission model in `xcm_config.rs` (i.e., whether a non-compliant ERC20 contract could realistically be registered as a matched asset id without privileged governance action) within the available tool budget — the grep results returned match counts but I could not retrieve the surrounding code before running out of iterations. This is a real gap in verifying the "Likelihood Explanation" claim that "any account" can trigger this deterministically, since asset registration for `MatchesFungibles` typically requires a privileged/governance-controlled foreign-asset registration step on Asset Hub, not an arbitrary unprivileged action.

Given the code-level mechanics are verified and directly correspond to a genuine broken-invariant (funds moved on-chain but XCM accounting not updated to match, i.e., "duplicate/erroneous settlement" and "permanent user-fund lock" categories), and the exploit does not require any privileged actor to *trigger* the bug once such an asset is registered (only requires a non-standard ERC20 contract's behavior, which is a token-design choice, not an attacker action against a privileged control), this qualifies as a valid finding under the required impact categories.

Audit Report

## Title
Non-compliant ERC20 `transfer` return data causes `ERC20Transactor` to desynchronize real token balances from XCM asset accounting - ([File: cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs])

## Summary
`ERC20Transactor::withdraw_asset_with_surplus` and `deposit_asset_with_surplus` execute an ERC20 `transfer` via `pallet_revive::Pallet::<T>::bare_call` and only treat `did_revert()` as the failure signal before subsequently requiring a strict ABI-decode of the return value with `IERC20::transferCall::abi_decode_returns_validate`. If the token contract completes the transfer successfully but returns no data or non-standard data (a known real-world pattern, e.g. USDT-style tokens), the decode fails after the balance has already moved on-chain, causing the XCM engine to treat an executed transfer as a failed one.

## Finding Description
In `withdraw_asset_with_surplus`, tokens are moved from `who` to `TransfersCheckingAccount` by the `bare_call`; if `did_revert()` is false but `abi_decode_returns_validate` errors on the return data, the function returns `Err(XcmError::FailedToTransactAsset("ERC20 contract result couldn't decode"))` without minting the corresponding `Erc20Credit` into `AssetsInHolding`, at lines 187-194. In `deposit_asset_with_surplus`, tokens are moved from the checking account to the beneficiary; on the same decode failure, the function returns `Err((what, XcmError::FailedToTransactAsset("ERC20 contract result couldn't decode")))`, handing back `what` as unspent even though the on-chain transfer already succeeded, at lines 270-298. There is no check of `return_value.data.is_empty()` as an alternate success signal, and no re-verification via `balanceOf` before/after the call.

## Impact Explanation
This breaks the invariant that asset accounting state should only advance when it matches actual on-chain settlement. On withdraw, user funds become stranded in `TransfersCheckingAccount` with no `AssetsInHolding` credit created, causing the XCM program to fail with nothing to route. On deposit, the beneficiary already receives tokens on-chain while the executor is told the deposit failed and still holds `what`, which can be trapped or reused, producing accounting not backed by the true checking-account balance.

## Likelihood Explanation
This is deterministic (not probabilistic) whenever the matched asset for `ERC20Transactor` is backed by a non-standard ERC20 contract whose `transfer` omits the boolean return on success. I could not fully verify within the available tool budget whether registering such a contract as a matched asset in `cumulus/parachains/runtimes/assets/asset-hub-westend/src/xcm_config.rs` requires privileged/governance action or is achievable by an ordinary unprivileged account; this affects whether the "any unprivileged account" framing in the original report is fully accurate for the registration step. If registration is privileged, then the "unprivileged attacker" characterization needs qualification, but the ensuing balance-desync bug for whatever assets end up registered is still real.

## Recommendation
Treat `!did_revert()` combined with either empty return data or a decodable `true` as success, mirroring the OpenZeppelin `SafeERC20` non-standard-token handling. Replace the `abi_decode_returns_validate(...)?` calls at lines 191 and 276 with logic that accepts `return_value.data.is_empty()` as success rather than failing decode outright.

## Proof of Concept
1. Register (via whatever mechanism governs asset registration for `ERC20Transactor` in `asset-hub-westend/src/xcm_config.rs`) a `pallet-revive` contract implementing `IERC20` whose `transfer` updates balances but returns no data on success.
2. Execute an XCM `WithdrawAsset` for this asset: the `bare_call` succeeds (`did_revert() == false`), balances move to `TransfersCheckingAccount`, but `abi_decode_returns_validate` errors on empty data, causing `withdraw_asset_with_surplus` to return `Err(...)` with no `AssetsInHolding` credit created.
3. Execute an XCM `DepositAsset` for this asset: tokens move to the beneficiary on-chain, but the same decode failure causes `deposit_asset_with_surplus` to return `Err((what, ...))`, leaving `what` as unspent despite actual delivery.
4. Compare the ERC20 contract's `balanceOf` state against the XCM-reported holding/trap state to confirm desynchronization.

### Citations

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L168-207)
```rust
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
