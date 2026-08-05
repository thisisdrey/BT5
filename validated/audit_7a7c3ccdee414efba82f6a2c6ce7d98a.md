Audit Report

## Title
`ERC20Transactor::deposit_asset_with_surplus` silently drops and permanently loses non-first fungible assets when `AssetsInHolding` contains more than one asset - (File: `cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs`)

## Summary
`ERC20Transactor::deposit_asset_with_surplus` only inspects the first fungible asset via `what.fungible_assets_iter().next()`, performs a single ERC20 transfer for that asset, and unconditionally returns `Ok(surplus)` on success, regardless of how many assets were actually in `what`. Since the `TransactAsset` trait contract treats `Ok` as "all of `what` was consumed," any additional fungible assets in the batch are destroyed when `what` goes out of scope, with no error and no trap for later recovery.

## Finding Description
The function signature accepts `what: AssetsInHolding`, a collection that legitimately can contain multiple assets when XCM instructions like `DepositAsset { assets: Wild(All) | Wild(AllCounted(n)), .. }` collapse multiple holding-register assets into a single call to the configured `TransactAsset`. [1](#0-0) 

The docstring explicitly acknowledges the flaw: "If multiple assets are present, only the first fungible asset will be deposited and the rest will be silently ignored." The only mitigation is a `defensive_assert!(what.len() == 1, ...)`, which in production builds is a no-op/log-only check and does not alter control flow or prevent the deposit from proceeding. [2](#0-1) 

After the single-asset ERC20 transfer succeeds, the function returns `Ok(surplus)` unconditionally — it does not check whether `what.len() == 1` before declaring success: [3](#0-2) 

Per the `TransactAsset` trait's default `deposit_asset_with_surplus` and the tuple-combinator semantics used by the XCM executor, an `Ok` result is treated as "the entirety of `what` has been consumed"; any un-consumed remainder is not separately returned or tracked: [4](#0-3) [5](#0-4) 

`ERC20Transactor` is wired into the production Asset Hub Westend runtime's XCM configuration, confirming this is live, in-scope code rather than test-only or dead code. [6](#0-5) 

## Impact Explanation
This matches the "permanent user-fund lock/loss" impact category. Any XCM program that causes the holding register to contain more than one ERC20 asset at the point `DepositAsset` (or an internal deposit call from a reserve-transfer/teleport/exchange sequence) is executed against `ERC20Transactor` results in all but the first matched ERC20 asset being irrecoverably destroyed. The executor believes the deposit fully succeeded (`Ok`), so no trap-asset recovery mechanism is triggered — the funds are gone with no error, no event, and no bookkeeping trail.

## Likelihood Explanation
Requires a chain to configure `ERC20Transactor` as part of its `AssetTransactor` (confirmed present in `asset-hub-westend`'s `xcm_config.rs`) and for an XCM program to accumulate more than one ERC20-matched fungible asset in holding before a deposit (e.g., via `Wild(All)`/`Wild(AllCounted(n))` filters, or sequences involving `ExchangeAsset` plus reserve deposits before a catch-all `DepositAsset`). No privileged actor is needed — an ordinary XCM sender constructing a normal multi-asset transfer can trigger this against their own or a beneficiary's funds.

## Recommendation
Either (1) modify `deposit_asset_with_surplus`/`deposit_asset` to iterate over all fungible assets in `what` and transfer each one, only returning `Ok` once every asset is moved, or (2) if only single-asset deposits are supported, return `Err((what, XcmError::FailedToTransactAsset(...)))` returning the full unspent `what` whenever `what.len() != 1`, allowing the XCM executor to trap the assets for later claim. Replace the debug-only `defensive_assert!` with a hard runtime check that rejects the deposit rather than merely logging.

## Proof of Concept
1. Configure a runtime with `ERC20Transactor` in its XCM `AssetTransactor` (as done in `asset-hub-westend`).
2. Construct an XCM program with two prior `WithdrawAsset`/`ReserveAssetDeposited` instructions for two distinct ERC20-backed tokens (`TokenA`, `TokenB`), both matched by `Matcher`.
3. Execute `DepositAsset { assets: Wild(AllCounted(2)), beneficiary }`.
4. `deposit_asset_with_surplus` receives `AssetsInHolding` containing both tokens; only `TokenA` is transferred via ERC20 `transfer`, and the function returns `Ok(surplus)`. `TokenB` is dropped silently when `what` goes out of scope, with no error, trap, or event — the beneficiary receives only `TokenA`, and `TokenB` is permanently lost.

### Citations

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L218-243)
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
```

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L270-280)
```rust
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

**File:** polkadot/xcm/xcm-executor/src/traits/transact_asset.rs (L79-100)
```rust
	/// Deposit the `what` asset in holding into the account of `who`.
	///
	/// Implementations should return `XcmError::FailedToTransactAsset` if deposit failed.
	fn deposit_asset(
		what: AssetsInHolding,
		_who: &Location,
		_context: Option<&XcmContext>,
	) -> Result<(), (AssetsInHolding, XcmError)> {
		Err((what, XcmError::Unimplemented))
	}

	/// Identical to `deposit_asset` but returning the surplus, if any.
	///
	/// Return the difference between the worst-case weight and the actual weight consumed.
	/// This can be zero most of the time unless there's some metering involved.
	fn deposit_asset_with_surplus(
		what: AssetsInHolding,
		who: &Location,
		context: Option<&XcmContext>,
	) -> Result<Weight, (AssetsInHolding, XcmError)> {
		Self::deposit_asset(what, who, context).map(|()| Weight::zero())
	}
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
