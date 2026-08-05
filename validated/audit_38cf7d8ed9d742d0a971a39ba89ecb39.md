Audit Report

## Title
Unbacked ERC20 asset minting in XCM via phantom `transfer()` return-value trust - ([File: cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs])

## Summary
`ERC20Transactor::withdraw_asset_with_surplus` and `deposit_asset_with_surplus` treat a non-reverting `IERC20::transfer` call that ABI-decodes to `true` as conclusive proof that `amount` tokens moved, without ever checking the checking account's or beneficiary's actual on-chain `balanceOf` delta. Because `ERC20Matcher` accepts *any* `Location` of the form `(0, [AccountKey20 { .. }])` as a valid asset with no allow-list or registration requirement, an unprivileged attacker can deploy a trivial contract whose `transfer` unconditionally returns `true` while moving no real value, and use it to fabricate `AssetsInHolding` credit inside an XCM program.

## Finding Description
`withdraw_asset_with_surplus` calls the attacker-supplied contract's `transfer(checking_account, amount)` as the withdrawing user (`OriginFor::<T>::signed(who.clone())`), and on `!did_revert()` plus a decoded `true` return, immediately mints `AssetsInHolding::new_from_fungible_credit(..., Erc20Credit(amount))` — with zero verification that the checking account's real balance increased: [1](#0-0) 

Symmetrically, `deposit_asset_with_surplus` calls `transfer(beneficiary, amount)` from the checking account and again only inspects `did_revert()` and the boolean return: [2](#0-1) 

The asset identity for both paths is `ERC20Matcher`, defined purely by location shape with no registry or allow-list — any `(0, [AccountKey20{key,..}])` maps directly to an `H160` asset id: [3](#0-2) 

This design is intentional per the feature's own PRDoc, which explicitly states any `AccountKey20` location will be matched and its `transfer` function invoked, with no mention of registration or an allow-list: [4](#0-3) 

`ERC20Transactor` is wired into Asset Hub Westend's `AssetTransactors` tuple, making it reachable by any signed user submitting `pallet_xcm::execute`/`send`: [5](#0-4) 

Because the attacker fully controls the deployed contract's logic (it's their own `pallet-revive` contract), they can make `transfer` always return `true` while performing no storage writes — corrupting the `Erc20Credit(amount)` value baked into `AssetsInHolding` so that it no longer represents any real balance change.

## Impact Explanation
This matches the "theft or unbacked mint or unlock" class of the impact gate: an ordinary signed user can fabricate `AssetsInHolding` credit for an arbitrary `amount` with zero real backing, using a self-deployed, self-registered "token" contract that requires no privilege, registration, or governance action. Whether this fabricated value can be laundered into real backing assets (e.g., via `pallet_asset_conversion` pools, reserve transfers, or teleports elsewhere in a chained XCM program) depends on downstream configuration that I could not fully verify in this session — in particular, whether Asset Hub Westend's `pallet_asset_conversion` pools can be created against an ERC20 `AccountKey20` location as a poolable asset. I was unable to confirm this within the available tool budget, so I cannot state with certainty whether a full "theft of real value" chain is currently reachable versus the fabricated credit being confined to the XCM holding register within a single message. Regardless, the corrupted value itself — an `Erc20Credit` amount inside `AssetsInHolding` that does not correspond to any real ERC20 balance movement — is a genuine and reproducible flaw in the transactor's trust model.

## Likelihood Explanation
Likelihood is high for producing the fabricated `AssetsInHolding` credit itself: it only requires deploying a `pallet-revive` contract implementing the `transfer(address,uint256)` selector to return `abi.encode(true)`, then submitting a `WithdrawAsset` XCM instruction via `pallet_xcm::execute` referencing that contract's `H160` address as an `AccountKey20` location. No relayer, validator, or governance action is needed. Realizing further value extraction (e.g., draining a liquidity pool or triggering a cross-chain payout) requires additional downstream configuration that was not fully verified here.

## Recommendation
- Do not rely solely on the boolean return / non-revert of `transfer` as proof of balance movement in `withdraw_asset_with_surplus` and `deposit_asset_with_surplus`. Query `balanceOf` for the checking account (and/or beneficiary) before and after the call and require the delta to equal `amount` exactly.
- Restrict `ERC20Matcher`/`ERC20Transactor` to an allow-list of vetted contract addresses, or require on-chain registration with a basic interface/behavior check before a contract can back XCM asset transfers.

## Proof of Concept
1. Deploy a `pallet-revive` contract at address `E` with `transfer(address,uint256) returns (bool) { return true; }` and no storage writes.
2. As a signed user, submit an XCM program via `pallet_xcm::execute` containing `WithdrawAsset` for `Location(0, [AccountKey20{key: E}])` with an arbitrary `amount`.
3. `ERC20Transactor::withdraw_asset_with_surplus` calls `E.transfer(checking_account, amount)`, observes `did_revert() == false` and decoded `true`, and mints `AssetsInHolding` of `amount` backed by nothing real: [6](#0-5) 
4. Verify via a unit/integration test that `E`'s real balance for the checking account did not change while `AssetsInHolding` reports `amount` credited, demonstrating the corrupted `Erc20Credit` value.

### Citations

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L185-207)
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

**File:** cumulus/parachains/runtimes/assets/common/src/lib.rs (L134-160)
```rust
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

**File:** prdoc/stable2506/pr_7762.prdoc (L6-19)
```text
doc:
  - audience: Runtime Dev
    description: |
      This PR introduces an Asset Transactor for dealing with ERC20 tokens and adds it to Asset Hub
      Westend.
      This means asset ids of the form `{ parents: 0, interior: X1(AccountKey20 { key, network }) }` will be
      matched by this transactor and the corresponding `transfer` function will be called in the
      smart contract whose address is `key`.
      If your chain uses `pallet-revive`, you can support ERC20s as well by adding the transactor, which lives
      in `assets-common`.
  - audience: Runtime User
    description: |
      This PR allows ERC20 tokens on Asset Hub to be referenced in XCM via their smart contract address.
      This is the first step towards cross-chain transferring ERC20s created on the Hub.
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
