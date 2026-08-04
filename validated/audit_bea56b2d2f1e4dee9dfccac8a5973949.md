## Local Analog Found

The external report's core broken invariant — *"code assumes an ERC20 `transfer` always returns a strictly-encoded `bool`, so non-standard/non-compliant tokens (no return data on success) cause the call to be misclassified"* — has a direct structural analog in `ERC20Transactor`, the XCM `TransactAsset` implementation used to move ERC20-denominated assets on Asset Hub Westend through `pallet-revive`.

### Title
Non-compliant ERC20 `transfer` return data causes `ERC20Transactor` to desynchronize real token balances from XCM asset accounting — ([File: cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs])

### Summary
`ERC20Transactor::withdraw_asset_with_surplus` and `deposit_asset_with_surplus` invoke an ERC20 contract's `transfer(address,uint256)` via `pallet_revive::Pallet::<T>::bare_call` and then strictly ABI-decode the return data with `IERC20::transferCall::abi_decode_returns_validate`, exactly mirroring the Solidity-level `RETURNDATASIZE` strictness described in the external report. [1](#0-0) [2](#0-1) 

### Finding Description
The contract call itself does not revert (`return_value.did_revert()` is `false`), meaning the underlying token contract executed the transfer and moved real balances. But if that ERC20 contract does not return exactly one ABI-encoded `bool` word (e.g. it returns no data on success — the same non-compliance class as USDT-style tokens cited in the report), `abi_decode_returns_validate` fails and the code treats the already-executed transfer as a failure:

- In `withdraw_asset_with_surplus`, the token has already moved from the user to `TransfersCheckingAccount`, yet the function returns `Err(XcmError::FailedToTransactAsset("ERC20 contract result couldn't decode"))` instead of minting the corresponding `Erc20Credit` into `AssetsInHolding`. [3](#0-2) 
- In `deposit_asset_with_surplus`, the token has already moved from the checking account to the beneficiary on-chain, yet the function returns `Err((what, XcmError::FailedToTransactAsset("ERC20 contract result couldn't decode")))`, handing the original `what: AssetsInHolding` back to the XCM executor as if the deposit never happened. [4](#0-3) 

The pallet has no other guard: no re-check of on-chain `balanceOf` before/after, no fallback for empty-return-data success, and no reconciliation between what the ERC20 contract actually did and what the XCM engine believes happened.

### Impact Explanation
This breaks the "message queues... and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically" invariant. Two concrete failure modes:
- **Fund lock on withdraw**: user tokens are physically moved into `TransfersCheckingAccount`, but no offsetting `AssetsInHolding` credit is created, so the XCM program aborts/traps with nothing to route — user funds become stranded in the checking account with no on-chain accounting path to reclaim them.
- **Duplicate/erroneous settlement on deposit**: tokens are physically delivered to the beneficiary's real ERC20 balance, but the XCM executor is told the deposit failed and still holds `what` as unspent value, which can be trapped, refunded to origin, or reused later in the same message — producing value that is not backed 1:1 by the actual checking-account balance.

This lands squarely in "Balances... and contract-held value must conserve value and settle exactly once" and "permanent user-fund lock" impact categories, and requires only an unprivileged interaction: any account performing an XCM asset transfer where the underlying asset is backed by a non-standard ERC20 contract on the revive side of Asset Hub Westend.

### Likelihood Explanation
This is exploitable purely by economic/token-choice conditions, not by any privileged actor: whenever the asset represented in `Matcher: MatchesFungibles<H160, u128>` resolves to a `pallet-revive` contract address whose `transfer` implementation omits the bool return value on success (a known and common pattern among real-world tokens, per the cited USDT precedent), every withdraw/deposit through `ERC20Transactor` for that asset will hit this path deterministically, not probabilistically.

### Recommendation
Do not rely solely on strict ABI-decode of the return value to determine success. Mirror the OpenZeppelin `SafeERC20` approach: treat `!did_revert()` combined with either (a) empty return data, or (b) a decodable `true`, as success; only treat a decodable `false` (or revert) as failure. Concretely, replace the `abi_decode_returns_validate(...)?` calls in `withdraw_asset_with_surplus` (line 191) and `deposit_asset_with_surplus` (line 276) with logic that accepts `return_value.data.is_empty()` as success, consistent with widely-adopted non-standard-ERC20 handling.

### Proof of Concept
1. Deploy (or register as a foreign asset) a `pallet-revive` contract implementing `IERC20` whose `transfer` function performs the balance update but returns no data (omits `return true;`), analogous to non-compliant ERC20s like USDT.
2. Configure this contract's address as the matched asset id for `ERC20Transactor` via the `Matcher` used in `cumulus/parachains/runtimes/assets/asset-hub-westend/src/xcm_config.rs`.
3. Execute an XCM `WithdrawAsset`/`DepositAsset` sequence moving this asset:
   - On withdraw: the `bare_call` to `transferCall` succeeds (`did_revert() == false`), balances move into `TransfersCheckingAccount`, but `IERC20::transferCall::abi_decode_returns_validate(&return_value.data)` errors on the empty data, so `withdraw_asset_with_surplus` returns `Err(...)` — user funds are now in the checking account with no `AssetsInHolding` credit created.
   - On deposit: the `bare_call` succeeds and moves tokens to the beneficiary, but the same decode failure causes `deposit_asset_with_surplus` to return `Err((what, ...))`, leaving `what` intact for further downstream handling (trap/refund) even though the beneficiary already received the tokens on-chain.
4. Compare the ERC20 contract's real `balanceOf` state (moved) against emitted XCM events (`AssetsTrapped` / failed transact / no credit created) to confirm the desync.

### Citations

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L168-216)
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
			}
		} else {
			tracing::debug!(target: "xcm::transactor::erc20::withdraw", ?result, "Error");
			// This error could've been duplicate smart contract, out of gas, etc.
			// If the issue is gas, there's nothing the user can change in the XCM
			// that will make this work since there's a hardcoded gas limit.
			Err(XcmError::FailedToTransactAsset("ERC20 contract execution errored"))
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
