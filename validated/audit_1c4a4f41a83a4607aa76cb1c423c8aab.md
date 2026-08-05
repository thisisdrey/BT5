Confirmed: `ERC20Transactor` is used directly in `AssetTransactors` with `assets_common::ERC20Matcher` and no additional allow-list wrapper is layered on top [1](#0-0) . Contrast this with `ForeignAssetsConvertedConcreteId`, which explicitly excludes local/relay locations via `EverythingBut<...>` filters [2](#0-1) , whereas `IsLocalAccountKey20` unconditionally matches any `AccountKey20` junction with no registry check [3](#0-2) .

The `withdraw_asset_with_surplus` and `deposit_asset_with_surplus` implementations both call `pallet_revive::Pallet::<T>::bare_call` on the attacker-supplied `asset_id`/`asset_contract_id` and trust the ABI-decoded boolean return value as the sole proof of a successful transfer, crediting `AssetsInHolding` with `Erc20Credit(amount)` for an arbitrary `amount` with no independent balance verification [4](#0-3) [5](#0-4) . `Erc20Credit` is explicitly documented as not enforcing runtime-level balance checks, relying entirely on the smart contract itself [6](#0-5) .

This matches the claim precisely as described, with all cited code present and verified in the repository.

Audit Report

## Title
Unbacked ERC20 asset credit via unvalidated attacker-controlled contract address in `ERC20Transactor` - (File: `cumulus/parachains/runtimes/assets/common/src/lib.rs`, `cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs`)

## Summary
`ERC20Matcher` (built on `IsLocalAccountKey20`) accepts any `AccountKey20` Location as a valid ERC20 asset identifier without checking it against any registry or allow-list. `ERC20Transactor::withdraw_asset_with_surplus` and `deposit_asset_with_surplus` then invoke `transfer()` on that attacker-chosen H160 contract via `pallet_revive::Pallet::<T>::bare_call` and treat the contract's self-reported ABI-decoded boolean return value as sole proof that a real, backed transfer occurred, crediting `AssetsInHolding` with an arbitrary `Erc20Credit(amount)`.

## Finding Description
`IsLocalAccountKey20::contains` matches any `(0, [AccountKey20 { .. }])` Location unconditionally, and `ERC20Matcher` is defined directly from it with no additional filter, unlike `ForeignAssetsConvertedConcreteId` which explicitly excludes disallowed locations via `EverythingBut`. This matcher is wired directly into `ERC20Transactor`, which is part of the live `AssetTransactors` tuple for Asset Hub Westend's XCM executor.

In `withdraw_asset_with_surplus`, the matched H160 (`asset_id`) — fully attacker-controlled since it is just an address extracted from the XCM `AssetId` Location — becomes the target contract of a `bare_call` invoking `IERC20::transferCall`. The result is judged solely by `return_value.did_revert()` being false and the ABI-decoded return value being `true`; if so, `AssetsInHolding` is credited with `Erc20Credit(amount)` for an `amount` fully chosen by the caller as part of the XCM `Asset`. `Erc20Credit` is explicitly documented as performing no runtime-level balance enforcement, deferring entirely to the smart contract. `deposit_asset_with_surplus` follows the identical trust pattern on the release side.

Because an attacker can deploy an ordinary, permissionless `pallet-revive` contract whose `transfer()` always returns `true` without moving any real value, and then reference that contract's address as the `AssetId` in an XCM program, the transactor will credit `AssetsInHolding` with a wholly fabricated balance. There is no check that the H160 corresponds to a genuine, previously registered ERC20 token (no analog of `pallet_assets`/foreign-asset registration), so nothing blocks this from being exercised by any signed account capable of deploying a contract and submitting an XCM program (e.g., via `pallet_xcm::execute`).

## Impact Explanation
This breaks the value-conservation invariant for asset crediting in the XCM executor: an unprivileged attacker can mint an unbacked "ERC20" credit inside `AssetsInHolding` backed by nothing, which can then be moved via further XCM instructions (e.g., `DepositAsset`) or supplied as a leg into `pallet_asset_conversion` pools to attempt extraction of real value from other participants. This matches the "theft or unbacked mint" impact category for the Polkadot SDK program, since the exact corrupted value is the `Erc20Credit(amount)` field inside `AssetsInHolding`, created without any real backing check.

## Likelihood Explanation
The attack requires only two unprivileged, permissionless actions available to any funded account: deploying a `pallet-revive` contract and submitting an XCM program (e.g. via `pallet_xcm::execute`) referencing that contract's address as an `AssetId`. No validator, governance, relayer, or leaked-key assumption is required, and the flaw is directly reachable through public extrinsics.

## Recommendation
Restrict `ERC20Matcher`/`IsLocalAccountKey20` (or layer an additional filter into the `MatchedConvertedConcreteId`) so that only H160 addresses registered in an explicit, governance-controlled allow-list/registry are accepted as valid ERC20 asset ids for `ERC20Transactor`, mirroring how `ForeignAssetsConvertedConcreteId` restricts eligible Locations.

## Proof of Concept
1. Attacker deploys a `pallet-revive` contract `Fake` implementing `IERC20` whose `transfer(address,uint256)` always returns `true` without altering real balances.
2. Attacker submits `pallet_xcm::execute` with `WithdrawAsset(Asset { id: AssetId(Location::new(0, [AccountKey20{ key: Fake_address, network: None }])), fun: Fungible(LARGE_AMOUNT) }, ...)`.
3. `ERC20Matcher::matches_fungibles` accepts `key` unconditionally; `withdraw_asset_with_surplus` calls `bare_call` invoking `Fake.transfer(checking_address, LARGE_AMOUNT)`, which returns `true`; `AssetsInHolding` is credited with `Erc20Credit(LARGE_AMOUNT)`.
4. Follow-up `DepositAsset` to the attacker's own account, or feeding this holding into an asset-conversion pool leg, lets the attacker realize the fabricated balance without any real ERC20 tokens moving.

### Citations

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

**File:** cumulus/parachains/runtimes/assets/common/src/lib.rs (L108-130)
```rust
pub type ForeignAssetsConvertedConcreteId<
	AdditionalLocationExclusionFilter,
	Balance,
	AssetId,
	LocationToAssetIdConverter = WithLatestLocationConverter<AssetId>,
	BalanceConverter = TryConvertInto,
> = MatchedConvertedConcreteId<
	AssetId,
	Balance,
	EverythingBut<(
		// Excludes relay/parent chain currency
		Equals<ParentLocation>,
		// Here we rely on fact that something like this works:
		// assert!(Location::new(1,
		// [Parachain(100)]).starts_with(&Location::parent()));
		// assert!([Parachain(100)].into().starts_with(&Here));
		StartsWith<LocalLocationPattern>,
		// Here we can exclude more stuff or leave it as `()`
		AdditionalLocationExclusionFilter,
	)>,
	LocationToAssetIdConverter,
	BalanceConverter,
>;
```

**File:** cumulus/parachains/runtimes/assets/common/src/lib.rs (L132-139)
```rust
/// `Contains<Location>` implementation that matches locations with no parents,
/// a `PalletInstance` and an `AccountKey20` junction.
pub struct IsLocalAccountKey20;
impl Contains<Location> for IsLocalAccountKey20 {
	fn contains(location: &Location) -> bool {
		matches!(location.unpack(), (0, [AccountKey20 { .. }]))
	}
}
```

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L73-107)
```rust
/// A minimal imbalance tracking type that holds an ERC20 token amount.
///
/// This type implements the necessary imbalance accounting traits but does not perform
/// runtime-level balance enforcement. It's used to track ERC20 token amounts within XCM
/// asset holdings, where the actual balance constraints are enforced by the ERC20 smart
/// contract itself rather than the runtime.
struct Erc20Credit(u128);
impl UnsafeConstructorDestructor<u128> for Erc20Credit {
	fn unsafe_clone(&self) -> Box<dyn ImbalanceAccounting<u128>> {
		Box::new(Erc20Credit(self.0))
	}
	fn forget_imbalance(&mut self) -> u128 {
		let amount = self.0;
		self.0 = 0;
		amount
	}
}

impl UnsafeManualAccounting<u128> for Erc20Credit {
	fn saturating_subsume(&mut self, mut other: Box<dyn ImbalanceAccounting<u128>>) {
		let amount = other.forget_imbalance();
		self.0 = self.0.saturating_add(amount);
	}
}

impl ImbalanceAccounting<u128> for Erc20Credit {
	fn amount(&self) -> u128 {
		self.0
	}
	fn saturating_take(&mut self, amount: u128) -> Box<dyn ImbalanceAccounting<u128>> {
		let new = self.0.min(amount);
		self.0 = self.0 - new;
		Box::new(Erc20Credit(new))
	}
}
```

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L185-216)
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
		} else {
			tracing::debug!(target: "xcm::transactor::erc20::withdraw", ?result, "Error");
			// This error could've been duplicate smart contract, out of gas, etc.
			// If the issue is gas, there's nothing the user can change in the XCM
			// that will make this work since there's a hardcoded gas limit.
			Err(XcmError::FailedToTransactAsset("ERC20 contract execution errored"))
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
