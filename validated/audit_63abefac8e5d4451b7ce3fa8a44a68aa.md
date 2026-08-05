Audit Report

## Title
ERC20 XCM Asset Transactor credits `AssetsInHolding` from an unchecked ERC-20 boolean return instead of a measured balance delta - (File: cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs)

## Summary
`ERC20Transactor::withdraw_asset_with_surplus` and `deposit_asset_with_surplus` derive the XCM `AssetsInHolding` credit/success purely from decoding a `bool` return value of a `bare_call` to `IERC20::transfer` on an arbitrary, attacker-controlled contract address, never verifying that the checking/beneficiary account's real balance changed by `amount`. Because the matcher (`ERC20Matcher` = `MatchedConvertedConcreteId<..., IsLocalAccountKey20, AccountKey20ToH160, ...>`) accepts any local `AccountKey20` location and contract deployment via `pallet_revive` is permissionless (`UploadOrigin: EnsureSigned`, `InstantiateOrigin: EnsureSigned`), an attacker can deploy a contract whose `transfer()` always returns `true` while moving no real value, letting them fabricate arbitrary XCM holding credit for that asset.

## Finding Description
`withdraw_asset_with_surplus` extracts `(asset_id, amount)` via `Matcher::matches_fungibles(what)` [1](#0-0) , then calls `pallet_revive::Pallet::<T>::bare_call` on `asset_id` with a `transfer(checking_address, amount)` payload [2](#0-1) . If the call doesn't revert and decodes to `true`, it unconditionally constructs `AssetsInHolding::new_from_fungible_credit` for the full requested `amount`, with no check of the checking account's actual balance change [3](#0-2) . `deposit_asset_with_surplus` mirrors this pattern on the deposit side [4](#0-3) .

The matcher `ERC20Matcher` accepts any location matching `(0, [AccountKey20{..}])` via `IsLocalAccountKey20` with no allow-list [5](#0-4) , and Asset Hub Westend's `pallet_revive::Config` sets both `UploadOrigin` and `InstantiateOrigin` to `EnsureSigned` [6](#0-5) , confirming any signed account can deploy an arbitrary ERC20-shaped contract that this transactor will process, since `ERC20Transactor` is wired into `AssetTransactors` [7](#0-6) .

## Impact Explanation
On its own, an attacker fabricating holding credit for their own worthless contract only manipulates bookkeeping for an asset nobody else trusts. However, this token can be paired in an `AssetConversion` liquidity pool against a real, valuable asset (native/foreign token) — the attacker deposits genuine liquidity, then uses this bug to conjure holding credit for their fake ERC20 far beyond what was actually transferred into the checking account, and swaps that fabricated credit for the real paired asset. This constitutes an unbacked-mint-style drain of real backing/value from other participants' liquidity, matching the "theft or unbacked mint" impact category, since the exact corrupted value is the `AssetsInHolding` credit amount in `withdraw_asset_with_surplus`/`deposit_asset_with_surplus`, which is not tied to any measured balance delta.

## Likelihood Explanation
High feasibility for any deployment (such as Asset Hub Westend) that wires `ERC20Transactor` with an unrestricted matcher over attacker-deployable contract addresses. The attacker needs only: (1) permissionless contract deployment (confirmed `EnsureSigned`), (2) a minimal `transfer()` that always returns `true` without honoring real balances, and (3) submission of one XCM program invoking withdraw/deposit against this contract. No validator, governance, or leaked-key assumptions are required.

## Recommendation
Read the checking/beneficiary account's real ERC20 balance immediately before and after the `bare_call` and credit/report success only for the actually observed delta, rather than trusting the boolean return value and caller-supplied `amount`.

## Proof of Concept
1. Attacker deploys a minimal contract via `pallet_revive::Pallet::<T>::instantiate` exposing `IERC20::transfer` that always returns `true` without adjusting real balances.
2. Attacker seeds an `AssetConversion` pool pairing this fake ERC20 (matched via `ERC20Matcher`/`AccountKey20`) with a real asset, providing genuine liquidity of both.
3. Attacker submits an XCM program (`WithdrawAsset` referencing the fake ERC20 with an inflated `amount`, then `ExchangeAsset`/swap through the pool) — `withdraw_asset_with_surplus` credits `AssetsInHolding` with the full fabricated `amount` per lines 195-203, without any real transfer to `TransfersCheckingAccount`.
4. The fabricated credit is swapped through the pool for the real paired asset, draining real value with no corresponding real inflow, completing an unbacked mint/drain.

### Citations

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L159-159)
```rust
		let (asset_id, amount) = Matcher::matches_fungibles(what)?;
```

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L168-181)
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
```

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L195-203)
```rust
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

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/src/lib.rs (L1395-1400)
```rust
	type AddressMapper = pallet_revive::AccountId32Mapper<Self>;
	type RuntimeMemory = ConstU32<{ 128 * 1024 * 1024 }>;
	type PVFMemory = ConstU32<{ 512 * 1024 * 1024 }>;
	type AllowEVMBytecode = ConstBool<true>;
	type UploadOrigin = EnsureSigned<Self::AccountId>;
	type InstantiateOrigin = EnsureSigned<Self::AccountId>;
```

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/src/xcm_config.rs (L239-246)
```rust
/// Means for transacting assets on this chain.
pub type AssetTransactors = (
	FungibleTransactor,
	FungiblesTransactor,
	ForeignFungiblesTransactor,
	UniquesTransactor,
	ERC20Transactor,
);
```
