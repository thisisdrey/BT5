Audit Report

## Title
Silent fund loss when `ERC20Transactor::deposit_asset_with_surplus` receives multiple matched fungible ERC20 assets - (File: cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs)

## Summary
`ERC20Transactor::deposit_asset_with_surplus` extracts only the first fungible asset from the `AssetsInHolding` value it receives via `what.fungible_assets_iter().next()`, issues a single ERC20 `transfer` for that asset, and returns `Ok(surplus)` on success — silently dropping any additional matched assets present in `what` rather than crediting or returning them. [1](#0-0)  The function's own doc comment and a `defensive_assert!` acknowledge this limitation, but `defensive_assert!` is a no-op in production builds, so the assumption is not enforced at runtime. [2](#0-1) 

## Finding Description
The `TransactAsset` trait's tuple implementation calls each transactor's `deposit_asset`/`deposit_asset_with_surplus` with the *entire* `AssetsInHolding` value passed by the caller, only splitting to the next tuple member on `Err((unspent, AssetNotFound | Unimplemented))`. [3](#0-2)  The XCM executor's `DepositAsset` instruction handling takes a filtered/matched subset of the holding register (which can legitimately contain multiple distinct fungible asset classes, e.g. several ERC20 contracts matched by a wildcard filter) and hands that whole `AssetsInHolding` to `Config::AssetTransactor::deposit_asset`. When `ERC20Transactor` is a member of that transactor tuple, it receives this full multi-asset value in `deposit_asset_with_surplus`.

Inside `deposit_asset_with_surplus`, only `what.fungible_assets_iter().next()` is matched and transferred via `IERC20::transferCall`; on success the function returns `Ok(surplus)`, discarding the rest of `what` without crediting the beneficiary or returning it to the caller for further processing (refund, trapping, or handoff to another transactor). [4](#0-3)  Since the function signature is `Result<Weight, (AssetsInHolding, XcmError)>`, there is no way to return a partial "unspent" remainder alongside an `Ok` result — the `Ok` branch has no `AssetsInHolding` component at all, so the untransferred assets are irrecoverably dropped once the local `what` goes out of scope.

The `defensive_assert!(what.len() == 1, ...)` at line 234 only panics when compiled with the defensive-checks feature (`std`/`try-runtime`); it is a no-op in production runtimes, so this is not an enforced invariant. `ERC20Transactor` is wired into asset-hub-westend's `xcm_config.rs`, confirming it is live scoped code. [5](#0-4) 

## Impact Explanation
This is a real fund-loss vector: any XCM `DepositAsset` instruction whose filter matches more than one ERC20-class asset in a single call to this transactor will result in every asset beyond the first being permanently lost — already debited from the executor's holding register but never credited to the beneficiary's ERC20 balance and never returned for trapping/refund. This directly violates the invariant that assets must conserve value and settle exactly once to the rightful beneficiary and amount, matching the required "permanent user-fund lock" / value non-conservation impact category.

## Likelihood Explanation
The tuple-based `TransactAsset::deposit_asset` dispatch mechanism passes the entire matched `AssetsInHolding` (potentially multi-asset) to each transactor in one call, and only re-splits on `AssetNotFound`/`Unimplemented` errors — confirmed directly in `polkadot/xcm/xcm-executor/src/traits/transact_asset.rs`. Any unprivileged XCM sender able to construct a message that withdraws multiple distinct ERC20-class assets into holding and issues a single `DepositAsset` with a filter matching more than one of them (e.g. `Wild(All)` or a multi-asset explicit filter) to a beneficiary reachable via `ERC20Transactor` can trigger this. The bug requires no privileged access — only standard XCM message construction — making it reachable by any unprivileged party routing multi-asset ERC20 transfers through Asset Hub.

## Recommendation
Do not hand-roll best-effort/partial settlement in `deposit_asset_with_surplus` (and the symmetric `withdraw_asset_with_surplus`). Either:
1. Loop over `what.fungible_assets_iter()`, attempt an ERC20 transfer for each asset, and accumulate any assets that fail or cannot be matched into a returned `AssetsInHolding` via the `Err` path rather than silently dropping them, or
2. Change the function to fail closed (return `Err((what, ...))`, restoring the entire input) whenever `what.len() != 1`, instead of relying on a no-op `defensive_assert!` in production.

## Proof of Concept
1. Register two distinct ERC20 contracts as fungible assets matched by the `Matcher: MatchesFungibles` configured for `ERC20Transactor` on asset-hub-westend.
2. Construct an XCM program that withdraws both ERC20 amounts into the holding register, then issues a single `DepositAsset { assets: Wild(All), beneficiary }` targeting a beneficiary.
3. Trace execution: `xcm-executor`'s `DepositAsset` handling takes the matched multi-asset holding and calls `Config::AssetTransactor::deposit_asset`, which reaches `ERC20Transactor::deposit_asset_with_surplus` with `what.len() == 2`.
4. Observe only `what.fungible_assets_iter().next()` is transferred via `IERC20::transferCall`; `Ok(surplus)` is returned, and the second asset's amount is never credited to the beneficiary nor returned/trapped — confirmable via unit test on `deposit_asset_with_surplus` with a two-asset `AssetsInHolding` input, asserting the beneficiary's second-token balance remains zero and no `AssetsTrapped` event fires.

### Citations

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L218-280)
```rust
	/// Deposits assets from holding to a beneficiary account via ERC20 transfer.
	///
	/// Note: This implementation only handles a single fungible asset at a time. The
	/// `AssetsInHolding` parameter is required by the `TransactAsset` trait, but callers
	/// should ensure only one asset is passed. If multiple assets are present, only the
	/// first fungible asset will be deposited and the rest will be silently ignored.
	/// The `defensive_assert!` helps catch misuse during development.
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
```

**File:** polkadot/xcm/xcm-executor/src/traits/transact_asset.rs (L272-294)
```rust
	fn deposit_asset(
		mut what: AssetsInHolding,
		who: &Location,
		context: Option<&XcmContext>,
	) -> Result<(), (AssetsInHolding, XcmError)> {
		for_tuples!( #(
			match Tuple::deposit_asset(what, who, context) {
				Err((unspent, XcmError::AssetNotFound)) | Err((unspent, XcmError::Unimplemented)) => {
					what = unspent;
					// continue
				},
				r => return r,
			}
		)* );
		tracing::trace!(
			target: "xcm::TransactAsset::deposit_asset",
			?what,
			?who,
			?context,
			"did not deposit asset",
		);
		Err((what, XcmError::AssetNotFound))
	}
```

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/src/xcm_config.rs (L1-1)
```rust
// Copyright (C) Parity Technologies (UK) Ltd.
```
