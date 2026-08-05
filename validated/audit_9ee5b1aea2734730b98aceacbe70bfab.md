Based on the code I reviewed, the claim is well-substantiated by the actual code in the repository.

Audit Report

## Title
Non-compliant ERC20 `transfer` return data causes `ERC20Transactor` to desynchronize real token balances from XCM asset accounting - ([File: cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs])

## Summary
`ERC20Transactor::withdraw_asset_with_surplus` and `deposit_asset_with_surplus` invoke a `pallet-revive` ERC20 contract's `transfer(address,uint256)` via `bare_call` and then strictly ABI-decode the return data with `IERC20::transferCall::abi_decode_returns_validate`, treating any decode failure as a transaction failure even when the on-chain token transfer actually succeeded. [1](#0-0) [2](#0-1)  This causes real token balance movements to desynchronize from the XCM executor's internal `AssetsInHolding` accounting whenever the underlying ERC20 contract is non-compliant (e.g., returns no data on success, like USDT-style tokens).

## Finding Description
In `withdraw_asset_with_surplus`, the contract call moves tokens from `who` to `TransfersCheckingAccount` via `IERC20::transferCall`; if `did_revert()` is false but the return data fails strict decode validation, the function returns `Err(XcmError::FailedToTransactAsset("ERC20 contract result couldn't decode"))` without ever calling `AssetsInHolding::new_from_fungible_credit`, so no credit is created for the tokens that already physically moved. [3](#0-2) 

In `deposit_asset_with_surplus`, tokens are transferred from `TransfersCheckingAccount` to the beneficiary via the same `transferCall`; on decode failure the function returns `Err((what, XcmError::FailedToTransactAsset("ERC20 contract result couldn't decode")))`, handing back the original `what: AssetsInHolding` as if the deposit never happened, even though the beneficiary already received the tokens on-chain. [4](#0-3) 

There is no fallback path treating empty return data as success (the pattern used by `SafeERC20`-style wrappers), no re-verification via `balanceOf`, and no reconciliation between the ERC20 contract's actual state change and the XCM engine's belief about the outcome — the strict `abi_decode_returns_validate` call is the sole success signal once `did_revert()` is false.

## Impact Explanation
This breaks the invariant that settlement state must only advance atomically with actual execution: real balance movement occurs, but XCM-level accounting says otherwise. This can result in a **permanent user-fund lock** (withdraw case: user funds moved into `TransfersCheckingAccount` with no corresponding `AssetsInHolding` credit, so the XCM program has nothing to route further) or **duplicate/erroneous settlement** (deposit case: `what` is returned to the executor as still-unspent value even though tokens were already delivered to the beneficiary, risking a trap/refund/reuse of value not backed by the actual checking-account balance).

## Likelihood Explanation
This requires no privileged action — any account executing an XCM asset transfer through `ERC20Transactor` for an asset backed by a non-standard ERC20 contract (one that returns no data on a successful `transfer`, a documented real-world pattern) will deterministically hit this desync on every such transfer. The trigger is purely a property of the token contract's return-data behavior, not of attacker privilege.

## Recommendation
Do not rely solely on strict ABI-decode of the return value to determine success. Follow the `SafeERC20` approach: treat `!did_revert()` combined with either empty return data or a decodable `true` as success, and only treat a decodable `false` (or a revert) as failure — replacing the unconditional `abi_decode_returns_validate(...)?` calls in `withdraw_asset_with_surplus` (around line 191) and `deposit_asset_with_surplus` (around line 276).

## Proof of Concept
1. Deploy a `pallet-revive` contract implementing `IERC20::transfer` that updates balances but returns no data on success (mirroring USDT-style non-compliance).
2. Register/configure this contract's address as the matched asset for `ERC20Transactor`'s `Matcher` in `cumulus/parachains/runtimes/assets/asset-hub-westend/src/xcm_config.rs`.
3. Execute an XCM `WithdrawAsset`: the `bare_call` succeeds (`did_revert() == false`), tokens move into `TransfersCheckingAccount`, but `abi_decode_returns_validate` errors on empty data, causing `withdraw_asset_with_surplus` to return `Err(...)` — funds are now stranded in the checking account with no `AssetsInHolding` credit.
4. Execute an XCM `DepositAsset` for the same asset: the `bare_call` succeeds and delivers tokens to the beneficiary, but the same decode failure causes `deposit_asset_with_surplus` to return `Err((what, ...))`, leaving `what` as unspent value even though the beneficiary already received the tokens on-chain.
5. Compare the ERC20 contract's `balanceOf` state (moved) against the XCM executor's holding/trap events to confirm the desync.

### Citations

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L150-216)
```rust
	fn withdraw_asset_with_surplus(
		what: &Asset,
		who: &Location,
		_context: Option<&XcmContext>,
	) -> Result<(AssetsInHolding, Weight), XcmError> {
		tracing::trace!(
			target: "xcm::transactor::erc20::withdraw",
			?what, ?who,
		);
		let (asset_id, amount) = Matcher::matches_fungibles(what)?;
		let who = AccountIdConverter::convert_location(who)
			.ok_or(MatchError::AccountIdConversionFailed)?;
		// We need to map the 32 byte checking account to a 20 byte account.
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

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L225-298)
```rust
	fn deposit_asset_with_surplus(
		what: AssetsInHolding,
		who: &Location,
		_context: Option<&XcmContext>,
	) -> Result<Weight, (AssetsInHolding, XcmError)> {
		tracing::trace!(
			target: "xcm::transactor::erc20::deposit",
			?what, ?who,
		);
		defensive_assert!(what.len() == 1, "Trying to deposit more than one asset!");
		// Check we handle this asset.
		let maybe = what
			.fungible_assets_iter()
			.next()
			.and_then(|asset| Matcher::matches_fungibles(&asset).ok());
		let (asset_contract_id, amount) = match maybe {
			Some(inner) => inner,
			None => return Err((what, MatchError::AssetNotHandled.into())),
		};
		let who = match AccountIdConverter::convert_location(who) {
			Some(inner) => inner,
			None => return Err((what, MatchError::AccountIdConversionFailed.into())),
		};
		// We need to map the 32 byte beneficiary account to a 20 byte account.
		let eth_address = T::AddressMapper::to_address(&who);
		let address = Address::from(Into::<[u8; 20]>::into(eth_address));
		// To deposit, we actually transfer from the checking account to the beneficiary.
		// We do this using the solidity ERC20 interface.
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
