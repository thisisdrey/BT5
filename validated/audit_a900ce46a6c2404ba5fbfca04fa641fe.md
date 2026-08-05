Audit Report

## Title
Unverified ERC20 `transfer` boolean return lets `ERC20Transactor::withdraw_asset_with_surplus`/`deposit_asset_with_surplus` mint and pay out unbacked XCM asset credit - (File: `cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs`)

## Summary
`ERC20Transactor` bridges arbitrary `pallet-revive` ERC20 contracts into the XCM `TransactAsset` interface, and `ERC20Matcher` accepts **any** local `AccountKey20` location as a valid asset identifier with no allowlist or governance gate. `withdraw_asset_with_surplus` mints `AssetsInHolding` credit purely from a contract's self-reported boolean `transfer` return value, and `deposit_asset_with_surplus` pays that credit out via a real ERC20 transfer, without ever checking `balanceOf` of the checking account before/after either call.

## Finding Description
`ERC20Matcher` is defined as `MatchedConvertedConcreteId<H160, u128, IsLocalAccountKey20, AccountKey20ToH160, TryConvertInto>` [1](#0-0) , and `IsLocalAccountKey20` matches any location of the shape `(0, [AccountKey20 { .. }])` with no registry, allowlist, or governance check [2](#0-1) . This means any deployed contract address (contract deployment via `pallet-revive` is a public, unprivileged operation) is automatically treated by the `ERC20Transactor` as a valid XCM fungible asset, confirming the claim's open question about permissionless registration — it is indeed permissionless.

In `withdraw_asset_with_surplus`, the transactor calls `IERC20::transferCall` to the checking account and, upon `is_success == true`, unconditionally mints `AssetsInHolding::new_from_fungible_credit(what.id.clone(), Box::new(Erc20Credit(amount)))` for the full claimed `amount` [3](#0-2) . There is no read of `balanceOf(checking_address)` before or after the call to confirm the reported amount actually moved. `deposit_asset_with_surplus` mirrors this: it performs a real `transfer` from the checking account to the beneficiary and trusts the boolean return alone to determine success, again without any balance verification [4](#0-3) .

Because any contract address qualifies as a matched asset, an attacker can deploy a non-compliant or intentionally malicious contract whose `transfer` returns `true` without moving the claimed value, then reference that contract's address as the XCM asset in a `WithdrawAsset`/`TransferAsset` program. This directly parallels the external report's `_mint`-without-verification defect: no atomic, single-source-of-truth ledger mutation (as in `pallet-assets::do_mint`/`do_burn` or `pallet-balances`'s `Balanced`/`Unbalanced` imbalance types) backs the `AssetsInHolding` credit — it is derived solely from an externally-controlled contract's self-reported boolean.

## Impact Explanation
The credited `AssetsInHolding` value is a first-class XCM fungible asset once minted, usable anywhere XCM value can be used on Asset Hub — including deposit to arbitrary beneficiaries via `deposit_asset_with_surplus`, and potentially via `PoolAssetsExchanger`/`AssetConversion` swaps against real assets, since `ERC20Matcher`-recognized assets are not excluded from pool participation in the XCM config [5](#0-4) . This satisfies the "theft or unbacked mint" impact category: an attacker-controlled contract can cause the runtime to believe value is backed in the checking account when it is not, and that unbacked value can subsequently be moved to real beneficiaries or exchanged for genuinely-backed assets.

## Likelihood Explanation
Exploitability requires only deploying an arbitrary contract via `pallet-revive` (an unprivileged, public operation) and submitting an XCM program (locally executed via `pallet_xcm::execute` or via message) referencing that contract's address as the asset. `ERC20Matcher`'s unconditional `IsLocalAccountKey20` match confirms no privileged registration step gates this path, making the withdraw/mint half of the bug directly reachable by any unprivileged actor. Realizing further real-world "theft" (e.g., extracting value from a shared liquidity pool or from other legitimate holders of the same contract) requires that asset to also be paired with real value elsewhere (e.g., in an AMM pool), which is a plausible but separate configuration step, not gated by the transactor itself.

## Recommendation
Do not trust the boolean `transfer` return value as the sole basis for `AssetsInHolding` accounting. Read `balanceOf(checking_address)` before and after the `transfer` call in both `withdraw_asset_with_surplus` and `deposit_asset_with_surplus`, and derive the credited/debited amount strictly from the observed balance delta, capping it at the requested `amount`. Additionally, consider restricting `ERC20Matcher` (or a wrapping `Contains` filter) to a governance-curated allowlist of ERC20 contracts rather than any arbitrary `AccountKey20` address.

## Proof of Concept
1. Deploy a malicious ERC20 contract via `pallet-revive` whose `transfer(to, value)` always ABI-returns `true` without updating internal balances for large `value`.
2. Since `ERC20Matcher`/`IsLocalAccountKey20` accepts any `AccountKey20` location unconditionally [6](#0-5) , submit an XCM program with `WithdrawAsset` referencing `Location::new(0, [AccountKey20 { key: <malicious_contract_address> }])` and a large `amount`.
3. `withdraw_asset_with_surplus` invokes `transfer`, receives `true`, and mints `Erc20Credit(amount)` into `AssetsInHolding` regardless of the contract's real state [7](#0-6) .
4. Chain a `DepositAsset` instruction targeting a beneficiary; `deposit_asset_with_surplus` performs a real `transfer` from the checking account for `amount`, completing the unbacked payout [8](#0-7) .
5. Verify via unit test that no `balanceOf(checking_address)` check occurred and that the minted/paid `amount` is independent of the contract's actual internal accounting.

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

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L191-207)
```rust
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

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/src/xcm_config.rs (L410-426)
```rust
pub type PoolAssetsExchanger = SingleAssetExchangeAdapter<
	crate::AssetConversion,
	crate::NativeAndNonPoolAssets,
	(
		TrustBackedAssetsAsLocation<TrustBackedAssetsPalletLocation, Balance, xcm::v5::Location>,
		ForeignAssetsConvertedConcreteId,
		// `ForeignAssetsConvertedConcreteId` excludes the relay token, so we add it back here.
		MatchedConvertedConcreteId<
			xcm::v5::Location,
			Balance,
			Equals<ParentLocation>,
			WithLatestLocationConverter<xcm::v5::Location>,
			TryConvertInto,
		>,
	),
	AccountId,
>;
```
