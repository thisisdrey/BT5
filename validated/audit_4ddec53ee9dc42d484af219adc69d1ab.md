Audit Report

## Title
Fee-on-transfer / non-conforming ERC20 tokens let `ERC20Transactor` credit XCM holding with more value than the checking account actually receives, breaking reserve-backing invariants - ([File: cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs])

## Summary
`ERC20Transactor::withdraw_asset_with_surplus` and `deposit_asset_with_surplus` invoke the ERC20 contract's `transfer` and only inspect `did_revert()` plus the ABI-decoded boolean return value, then unconditionally credit/require the requested `amount` rather than the actual balance delta observed by the `ERC20TransfersCheckingAccount`. Because `ERC20Matcher` (`IsLocalAccountKey20`/`AccountKey20ToH160`) accepts *any* `(0, [AccountKey20{..}])` location as a valid reserve asset with no allow-list or conformance check, any user can permissionlessly deploy a fee-on-transfer/deflationary contract via `pallet-revive` and cause the credited XCM holding value to exceed what the checking account truly received.

## Finding Description
`ERC20Matcher` is defined as an unconditional matcher over any local `AccountKey20` location: [1](#0-0) . There is no registry, allow-list, or verification step tying a contract address to "verified balance-conserving ERC20" before it can be used as a reserve asset in XCM.

In `withdraw_asset_with_surplus`, the code calls `Matcher::matches_fungibles(what)` to get `(asset_id, amount)`, invokes `transfer(checking_account, amount)` on the arbitrary contract, and — provided the call didn't revert and the ABI-decoded return is `true` — credits the XCM holding register with `Erc20Credit(amount)` regardless of what the checking account's real balance became: [2](#0-1) .

The symmetric `deposit_asset_with_surplus` path has the same trust assumption: it transfers `amount` out of the shared `TransfersCheckingAccount` to the beneficiary and treats a decoded `true` return as full success without confirming the beneficiary actually received `amount`: [3](#0-2) .

`ERC20TransfersCheckingAccount` is a single, chain-wide shared account used for all cross-chain ERC20 movement of every ERC20 asset routed through this transactor: [4](#0-3) . Existing tests in the repository only cover a contract that isn't ERC20-shaped at all, or one that returns a non-boolean type — both hard failures — but there is no test/guard for a contract that fully conforms to the `transfer(address,uint256) returns (bool)` ABI, returns `true`, yet silently delivers less than `amount` (a fee-on-transfer/deflationary/rebasing token), confirming this class of behavior is unhandled by the current logic.

## Impact Explanation
Since `Erc20Credit(amount)` feeds the XCM `AssetsInHolding` register used for reserve-based transfers (`DepositReserveAsset`, `InitiateReserveWithdraw`, etc.), a fee-on-transfer contract lets an attacker cause Asset Hub to assert to other chains in the network that `ERC20TransfersCheckingAccount` backs more of the token than it actually holds. This directly corrupts the amount value underpinning reserve-backed cross-chain accounting — an unbacked-mint/under-collateralization condition — and subsequent legitimate `deposit_asset_with_surplus` calls for the same asset can fail once the checking account's real balance falls short of what has been claimed as backed, producing denial of service and broken settlement for other users of the asset.

## Likelihood Explanation
No privileged actor is required. `pallet-revive` contract deployment is permissionless, and `ERC20Matcher` accepts any `AccountKey20` address as a valid reserve-asset location with no integration checklist or allow-list, so any unprivileged user can deploy a fee-on-transfer ERC20 and immediately submit an XCM program (`WithdrawAsset`/`DepositAsset`) referencing it. Fee-on-transfer/deflationary token patterns are common and easy to implement, making this readily reachable and repeatable.

## Recommendation
Before crediting/requiring `amount` in `withdraw_asset_with_surplus` and `deposit_asset_with_surplus`, read the checking/beneficiary account's actual ERC20 balance (via `balanceOf`) immediately before and after the `transfer` call, and credit/require exactly the observed delta rather than trusting the requested `amount` and the boolean return value. Alternatively, gate which ERC20 contract addresses `ERC20Matcher` will accept behind a governance-controlled allow-list of contracts verified to be balance-conserving.

## Proof of Concept
1. Deploy an ERC20-ABI-conforming `pallet-revive` contract whose `transfer(to, amount)` moves `amount * 95 / 100` to `to`, burns the remainder, and returns `true`.
2. Submit an XCM program: `WithdrawAsset` for `100` units referencing that contract via `AccountKey20`, followed by `DepositReserveAsset`/`InitiateReserveWithdraw` to a remote chain.
3. `ERC20Transactor::withdraw_asset_with_surplus` calls `transfer(checking_account, 100)`; only `95` reach `ERC20TransfersCheckingAccount` but the call returns `true`, so `Erc20Credit(100)` is credited and forwarded as a reserve message claiming `100` units are backed.
4. Repeat across multiple withdrawals to accumulate a shortfall between real checking-account balance and the sum of amounts claimed as backed.
5. A subsequent legitimate `deposit_asset_with_surplus` for another user fails once the checking account's actual balance is insufficient, demonstrating the broken conservation-of-value invariant.

### Citations

**File:** cumulus/parachains/runtimes/assets/common/src/lib.rs (L132-160)
```rust
/// `Contains<Location>` implementation that matches locations with no parents,
/// a `PalletInstance` and an `AccountKey20` junction.
pub struct IsLocalAccountKey20;
impl Contains<Location> for IsLocalAccountKey20 {
	fn contains(location: &Location) -> bool {
		matches!(location.unpack(), (0, [AccountKey20 { .. }]))
	}
}

/// Fallible converter from a location to a `H160` that matches any location ending with
/// an `AccountKey20` junction.
pub struct AccountKey20ToH160;
impl MaybeEquivalence<Location, H160> for AccountKey20ToH160 {
	fn convert(location: &Location) -> Option<H160> {
		match location.unpack() {
			(0, [AccountKey20 { key, .. }]) => Some((*key).into()),
			_ => None,
		}
	}

	fn convert_back(key: &H160) -> Option<Location> {
		Some(Location::new(0, [AccountKey20 { key: (*key).into(), network: None }]))
	}
}

/// [`xcm_executor::traits::MatchesFungibles`] implementation that matches
/// ERC20 tokens.
pub type ERC20Matcher =
	MatchedConvertedConcreteId<H160, u128, IsLocalAccountKey20, AccountKey20ToH160, TryConvertInto>;
```

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L159-208)
```rust
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
```

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L253-299)
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
		} else {
```

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/src/xcm_config.rs (L213-237)
```rust
parameter_types! {
	/// Taken from the real gas and deposits of a standard ERC20 transfer call.
	pub const ERC20TransferGasLimit: Weight = Weight::from_parts(500_000_000_000, 10 * 1024 * 1024);
	pub const ERC20TransferStorageDepositLimit: Balance = 10_200_000_000;
	pub ERC20TransfersCheckingAccount: AccountId = PalletId(*b"py/revch").into_account_truncating();
	pub DapBufferAccount: AccountId = pallet_dap::Pallet::<Runtime>::buffer_account();
}

/// Transactor for ERC20 tokens.
pub type ERC20Transactor = assets_common::ERC20Transactor<
	// We need this for accessing pallet-revive.
	Runtime,
	// The matcher for smart contracts.
	assets_common::ERC20Matcher,
	// How to convert from a location to an account id.
	LocationToAccountId,
	// The maximum gas that can be used by a standard ERC20 transfer.
	ERC20TransferGasLimit,
	// The maximum storage deposit that can be used by a standard ERC20 transfer.
	ERC20TransferStorageDepositLimit,
	// We're generic over this so we can't escape specifying it.
	AccountId,
	// Checking account for ERC20 transfers.
	ERC20TransfersCheckingAccount,
>;
```
