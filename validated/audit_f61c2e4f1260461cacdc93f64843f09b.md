This confirms the claim's technical accuracy fully. `ERC20Matcher` in `cumulus/parachains/runtimes/assets/common/src/lib.rs` matches any location of `(0, [AccountKey20 { key, .. }])` unconditionally via `IsLocalAccountKey20` with no allowlist, and converts to `H160` via `AccountKey20ToH160` — meaning any deployed contract address is matchable. This is wired into Asset Hub Westend's `AssetTransactors` tuple.

Audit Report

## Title
XCM ERC20 asset transactor credits nominal `amount` into holding instead of actual balance received, enabling unbacked value creation with fee-on-transfer/non-standard ERC20 contracts - (File: cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs)

## Summary
`ERC20Transactor::withdraw_asset_with_surplus` invokes an arbitrary user-deployed `pallet-revive` contract's `transfer(to, amount)` via `bare_call`, and credits `AssetsInHolding` with the full nominal `amount` whenever the call doesn't revert and decodes to boolean `true`, without verifying the `TransfersCheckingAccount`'s actual balance delta. `deposit_asset_with_surplus` has the symmetric flaw on withdrawal to a beneficiary. Since `ERC20Matcher` (`cumulus/parachains/runtimes/assets/common/src/lib.rs`, `IsLocalAccountKey20`/`AccountKey20ToH160`) matches any local `AccountKey20` location without an allowlist, and is wired into Asset Hub Westend's `AssetTransactors`, any user can deploy a non-standard/fee-on-transfer/malicious ERC20 contract and reference it in XCM to mint XCM holding value that is not backed by an equivalent real token transfer.

## Finding Description
`withdraw_asset_with_surplus` builds an `IERC20::transferCall` for the requested `amount`, executes it via `pallet_revive::Pallet::<T>::bare_call`, and on non-revert + `true` decode, unconditionally constructs `AssetsInHolding::new_from_fungible_credit(what.id.clone(), Box::new(Erc20Credit(amount)))` using the nominal `amount`, never checking the real balance change of `TransfersCheckingAccount`: [1](#0-0) 

The comment on `Erc20Credit` explicitly documents that "the actual balance constraints are enforced by the ERC20 smart contract itself rather than the runtime": [2](#0-1) 

The symmetric `deposit_asset_with_surplus` releases the holding credit based only on the boolean return value of the `transfer` call from `TransfersCheckingAccount` to the beneficiary: [3](#0-2) 

Critically, `ERC20Matcher` matches *any* local `AccountKey20` location with no allowlist or registration requirement: [4](#0-3) 

This is wired into `AssetTransactors` on Asset Hub Westend: [5](#0-4) 

Existing guards (`return_value.did_revert()`, ABI-decoded boolean check) only catch outright reverts or contracts that explicitly return `false`; they cannot detect a contract that returns `true` while moving less (or more) than `amount`, since there is no `balanceOf` pre/post comparison anywhere in this code path.

## Impact Explanation
Any unprivileged user can deploy a `pallet-revive` contract implementing a non-conforming `transfer` (e.g., always returns `true` but moves fewer tokens, or moves zero tokens) and reference it via XCM using its `AccountKey20` location — no registration or governance action is needed since `ERC20Matcher` accepts any such location. Executing `WithdrawAsset` against this contract causes `AssetsInHolding` to be credited with the full nominal `amount` even though the checking account received less. This holding value is fully fungible within the XCM executor and can be deposited to a beneficiary, reserve-transferred cross-chain, or exchanged in a pool, constituting unbacked mint of XCM-recognized asset value — matching the "theft or unbacked mint" impact category.

## Likelihood Explanation
High: deploying a `pallet-revive` contract and submitting an XCM program referencing it by address is a fully public, unprivileged action requiring only the ability to submit extrinsics on Asset Hub Westend. No relayer, validator, or governance collusion is required, and the exploit is repeatable at will.

## Recommendation
Instead of trusting the boolean return value alone, read `balanceOf` of the relevant account before and after the `transfer`/`transferFrom` call and credit/debit `AssetsInHolding` with the actual observed balance delta. If the delta does not equal the requested `amount`, return an `XcmError` rather than crediting/releasing the nominal amount.

## Proof of Concept
1. Deploy a `pallet-revive` contract implementing `IERC20` whose `transfer(to, value)` always returns `true` but moves `value / 2` (or `0`) tokens to `to`.
2. Fund an account with this token; its location resolves to `{parents: 0, interior: X1(AccountKey20 { key: <contract address>, network: None })}`, which `ERC20Matcher` accepts unconditionally.
3. Submit an XCM program: `WithdrawAsset(asset, amount)` followed by `DepositAsset` to a different beneficiary or `InitiateReserveWithdraw` to another chain.
4. `withdraw_asset_with_surplus` calls `transfer(checking_account, amount)`; the malicious contract returns `true` while transferring less than `amount`; `ERC20Transactor` still constructs `AssetsInHolding::new_from_fungible_credit` for the full nominal `amount` (`cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs`, lines 195-203).
5. The XCM executor deposits/forwards the inflated holding amount to the beneficiary/destination chain despite the real balance increase of `TransfersCheckingAccount` being smaller — demonstrating unbacked value creation.

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

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L185-208)
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
				} else {
					tracing::debug!(target: "xcm::transactor::erc20::withdraw", "contract transfer failed");
					Err(XcmError::FailedToTransactAsset("ERC20 contract transfer failed"))
				}
			}
```

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L270-298)
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

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/src/xcm_config.rs (L221-246)
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
);
```
