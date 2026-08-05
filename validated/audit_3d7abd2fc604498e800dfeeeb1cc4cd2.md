Audit Report

## Title
Unbacked XCM asset credit can be minted via ERC20 contracts that lie about `transfer` success - (File: `cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs`)

## Summary
`ERC20Transactor::withdraw_asset_with_surplus` and `::deposit_asset_with_surplus` treat any `pallet-revive` contract matched by `ERC20Matcher` as a valid ERC20 fungible asset for XCM, and accept the operation as successful purely based on `!did_revert()` plus a decoded `true` boolean return value from `transfer()`, never verifying that the `TransfersCheckingAccount`'s or beneficiary's actual token balance changed by `amount`.

## Finding Description
`withdraw_asset_with_surplus` performs a `bare_call` to the asset contract's `IERC20::transfer(checking_address, amount)` and, if the call doesn't revert and `abi_decode_returns_validate` yields `true`, unconditionally mints an `Erc20Credit(amount)` into the XCM holding register: [1](#0-0) . The symmetric `deposit_asset_with_surplus` calls `transfer()` from the checking account to the beneficiary and again only inspects the decoded boolean, on line 276-280: [2](#0-1) . `Erc20Credit` is explicitly documented as not enforcing runtime-level balance constraints, deferring entirely to the contract itself: [3](#0-2) .

Critically, the `Matcher` used in the deployed configuration, `ERC20Matcher`, imposes no allow-list — it matches *any* location of the form `(0, [AccountKey20 { .. }])`, meaning any `pallet-revive` contract address can be treated as a valid ERC20 asset for this transactor: [4](#0-3) . This matcher is wired directly into the live `asset-hub-westend` runtime's `ERC20Transactor` type: [5](#0-4) . Since deploying a `pallet-revive` contract is an unprivileged, permissionless action, any user can deploy a fake ERC20 whose `transfer()` always returns `abi_encode(true)` without any storage mutation, then reference that contract's address via `AccountKey20` in an XCM `WithdrawAsset` instruction to fabricate `Erc20Credit` in the XCM holding register with no real balance movement.

## Impact Explanation
This breaks value-conservation for the ERC20-as-XCM-asset abstraction: an unprivileged attacker can mint arbitrary `Erc20Credit(amount)` XCM holding credit that has no backing balance change in `TransfersCheckingAccount` or the target contract. If subsequently deposited to a beneficiary, or forwarded via reserve-transfer/teleport instructions to chains that trust this asset location as 1:1 backed, this produces unbacked value creation — matching the "theft or unbacked mint or unlock" impact category.

## Likelihood Explanation
High likelihood: deploying a `pallet-revive` contract and submitting an XCM program referencing it via `AccountKey20` (e.g., through `pallet_xcm::execute`) requires only ordinary account permissions, no governance, no compromised keys, no privileged access.

## Recommendation
Query the target account's (checking account for withdraw, beneficiary for deposit) ERC20 balance via `IERC20::balanceOf` before and after the `bare_call`, and require the observed delta to equal exactly `amount` before treating the transfer as successful and before minting/releasing `Erc20Credit`.

## Proof of Concept
1. Deploy a `pallet-revive` contract `FakeERC20` whose `transfer(address,uint256)` always returns `abi_encode(true)` without any balance state change.
2. Submit an XCM program via `pallet_xcm::execute` containing `WithdrawAsset` for `Asset { id: AccountKey20 { key: FakeERC20_address, network: None }, fun: Fungible(amount) }` from the attacker's account.
3. `ERC20Matcher::matches_fungibles` accepts `FakeERC20_address` unconditionally (no allow-list) at [6](#0-5) ; `withdraw_asset_with_surplus` calls `transfer()`, receives `true`, and mints `Erc20Credit(amount)` into holding at [7](#0-6) , while `ERC20TransfersCheckingAccount`'s real `FakeERC20` balance never increased.
4. Follow with `DepositAsset` to a beneficiary or forward via reserve-transfer to another chain, demonstrating unbacked credit creation.

### Citations

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L73-79)
```rust
/// A minimal imbalance tracking type that holds an ERC20 token amount.
///
/// This type implements the necessary imbalance accounting traits but does not perform
/// runtime-level balance enforcement. It's used to track ERC20 token amounts within XCM
/// asset holdings, where the actual balance constraints are enforced by the ERC20 smart
/// contract itself rather than the runtime.
struct Erc20Credit(u128);
```

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L185-203)
```rust
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

**File:** cumulus/parachains/runtimes/assets/common/src/lib.rs (L132-161)
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

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/src/xcm_config.rs (L221-245)
```rust
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

/// Means for transacting assets on this chain.
pub type AssetTransactors = (
	FungibleTransactor,
	FungiblesTransactor,
	ForeignFungiblesTransactor,
	UniquesTransactor,
	ERC20Transactor,
```
