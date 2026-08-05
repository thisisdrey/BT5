Audit Report

## Title
Permanent lock of XCM-bridged assets when the underlying ERC20 contract's `transfer()` callback reverts or exceeds the hardcoded weight budget - (File: `cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs`)

## Summary
`ERC20Transactor` implements XCM's `TransactAsset` for fungible assets whose `AssetId` resolves to an arbitrary `H160` address via `assets_common::ERC20Matcher`, and settles both `withdraw_asset_with_surplus` and `deposit_asset_with_surplus` by making a mandatory `pallet_revive::Pallet::<T>::bare_call` into that address's contract code, invoking `IERC20::transferCall`, under a fixed `WeightLimit`/`StorageDepositLimit`. [1](#0-0) [2](#0-1)  There is no non-contract-dependent accounting fallback, so a contract that always reverts (or exhausts the weight budget) for the checking account or a specific beneficiary permanently blocks settlement for that asset/account pair.

## Finding Description
`ERC20Matcher` is defined as `MatchedConvertedConcreteId<H160, u128, IsLocalAccountKey20, AccountKey20ToH160, TryConvertInto>` [3](#0-2) , and `IsLocalAccountKey20` matches **any** `Location` of the shape `(0, [AccountKey20 { .. }])` with no allow-list, registry, or governance gate [4](#0-3) . This confirms the claim's caveat: matching/"registration" of an ERC20 contract as a valid XCM asset is fully permissionless — any 20-byte address, including one an attacker freshly deploys a Solidity contract to, is treated as a matchable ERC20 asset id by `Matcher::matches_fungibles`. `ERC20Transactor` is wired into `asset-hub-westend`'s `AssetTransactors` tuple with a fixed `ERC20TransferGasLimit` and `ERC20TransfersCheckingAccount` [5](#0-4) .

Both settlement paths are unconditionally dependent on the external call succeeding: on revert, decode failure, `Ok(false)` return, or `bare_call` execution error (e.g. out-of-gas against the fixed `weight_limit`), the transactor returns `XcmError::FailedToTransactAsset` with no alternate ledger-based settlement [6](#0-5) [7](#0-6) . The code's own comment acknowledges this is a dead end when the failure is gas-related, since "there's nothing the user can change in the XCM that will make this work since there's a hardcoded gas limit" [8](#0-7) .

An attacker can deploy an ERC20 contract whose `transfer()` reverts specifically when `msg.sender == TransfersCheckingAccount` (blocking `deposit_asset_with_surplus`, called from the checking account at line 257) or when `msg.sender` is a particular withdrawing user (blocking `withdraw_asset_with_surplus`). If `deposit_asset_with_surplus` fails after the asset has already left holding via a prior withdraw/reserve step, the XCM executor's generic asset-trap mechanism (`DropAssets`/`ClaimAssets`) would be invoked [9](#0-8) ; because any claim replays the same deposit path against the same adversarial contract, the trapped value can never be redeemed.

## Impact Explanation
This matches the "permanent user-fund or bridge-state lock" impact category: settlement (crediting the checking account or beneficiary) can advance only if arbitrary, unprivileged, attacker-authored contract code cooperates within a fixed weight/gas budget it does not control. Because matching is fully permissionless (confirmed via `IsLocalAccountKey20`), there is no governance gate standing between an attacker and this state — the earlier caveat in the original claim about possible governance-gating is resolved: registration/matching requires no privileged action at all.

## Likelihood Explanation
Any unprivileged account can deploy a Solidity contract under `pallet-revive` and immediately use its address as a valid XCM `AssetId` for `ERC20Transactor`, since `ERC20Matcher`'s `MatchAssetId` filter (`IsLocalAccountKey20`) accepts any local `AccountKey20` location without any registry check [10](#0-9) . The attacker fully controls the contract's `transfer()` logic and can make it revert selectively for the well-known `TransfersCheckingAccount` address or a chosen victim beneficiary, and can also make it exceed the hardcoded `ERC20TransferGasLimit` [11](#0-10) . This requires no relayer, validator, collator, or governance privilege — only the ability to submit a normal XCM transfer/reserve-transfer instruction and to deploy a contract, both of which are public, permissionless actions.

## Recommendation
Do not gate settlement of the fixed checking-account leg on an external, attacker-controllable contract call with no fallback. Options: (a) restrict `ERC20Matcher`/`ERC20Transactor` to a governance-curated allow-list of vetted ERC20 contract addresses instead of matching any `AccountKey20` location; (b) decouple holding/trap bookkeeping so a failed `deposit_asset_with_surplus` does not simply re-trap the same non-redeemable asset — e.g., support partial/alternative settlement or refund-to-origin-chain semantics; (c) make the weight/gas budget auditable and per-asset configurable so legitimate contracts with heavier logic are not starved, while still bounding worst-case cost.

## Proof of Concept
1. Deploy a `pallet-revive` contract `Evil` implementing `IERC20` whose `transfer()` does `require(msg.sender != CHECKING_ACCOUNT_ETH_ADDR)`.
2. Since `ERC20Matcher` (`IsLocalAccountKey20` + `AccountKey20ToH160`) accepts any local `AccountKey20` location as a valid asset id with no allow-list [10](#0-9) , construct an XCM `Asset { id: AccountKey20(Evil's address), fun: Fungible(amount) }`.
3. Trigger a flow ending in `ERC20Transactor::deposit_asset_with_surplus` for a beneficiary with this asset in holding; the `bare_call` from `TransfersCheckingAccount` into `Evil::transfer` always reverts [12](#0-11) , returning `Err((what, XcmError::FailedToTransactAsset("ERC20 contract reverted")))`.
4. The asset is trapped by the executor's `DropAssets`; any subsequent `ClaimAssets` attempt re-invokes the identical failing deposit path against `Evil`, permanently locking the value.

### Citations

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L159-181)
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
```

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L185-215)
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
```

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L251-305)
```rust
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
		} else {
			tracing::debug!(target: "xcm::transactor::erc20::deposit", ?result, "Error");
			// This error could've been duplicate smart contract, out of gas, etc.
			// If the issue is gas, there's nothing the user can change in the XCM
			// that will make this work since there's a hardcoded gas limit.
			Err((what, XcmError::FailedToTransactAsset("ERC20 contract execution errored")))
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

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/src/xcm_config.rs (L213-246)
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

/// Means for transacting assets on this chain.
pub type AssetTransactors = (
	FungibleTransactor,
	FungiblesTransactor,
	ForeignFungiblesTransactor,
	UniquesTransactor,
	ERC20Transactor,
);
```

**File:** polkadot/xcm/xcm-executor/src/traits/drop_assets.rs (L27-99)
```rust
pub trait DropAssets {
	/// Handler for receiving dropped assets. Returns the weight consumed by this operation.
	fn drop_assets(origin: &Location, assets: AssetsInHolding, context: &XcmContext) -> Weight;
}
impl DropAssets for () {
	fn drop_assets(_origin: &Location, _assets: AssetsInHolding, _context: &XcmContext) -> Weight {
		Weight::zero()
	}
}

/// Morph a given `DropAssets` implementation into one which can filter based on assets. This can
/// be used to ensure that `AssetsInHolding` values which hold no value are ignored.
#[allow(dead_code)]
pub struct FilterAssets<D, A>(PhantomData<(D, A)>);

impl<D: DropAssets, A: Contains<AssetsInHolding>> DropAssets for FilterAssets<D, A> {
	fn drop_assets(origin: &Location, assets: AssetsInHolding, context: &XcmContext) -> Weight {
		if A::contains(&assets) {
			D::drop_assets(origin, assets, context)
		} else {
			Weight::zero()
		}
	}
}

/// Morph a given `DropAssets` implementation into one which can filter based on origin. This can
/// be used to ban origins which don't have proper protections/policies against misuse of the
/// asset trap facility don't get to use it.
#[allow(dead_code)]
pub struct FilterOrigin<D, O>(PhantomData<(D, O)>);

impl<D: DropAssets, O: Contains<Location>> DropAssets for FilterOrigin<D, O> {
	fn drop_assets(origin: &Location, assets: AssetsInHolding, context: &XcmContext) -> Weight {
		if O::contains(origin) {
			D::drop_assets(origin, assets, context)
		} else {
			Weight::zero()
		}
	}
}

/// Define any handlers for the `AssetClaim` instruction.
///
/// Types implementing this trait should make sure to properly handle imbalances held within the
/// trap and pass them over to `AssetsInHolding`. Generally should have a mirror `DropAssets`
/// implementation that originally moved the imbalance from holding to this trap.
pub trait ClaimAssets {
	/// Claim any assets available to `origin` and return them in a single `AssetsInHolding` value,
	/// together with the weight used by this operation.
	fn claim_assets(
		origin: &Location,
		ticket: &Location,
		what: &Assets,
		context: &XcmContext,
	) -> Option<AssetsInHolding>;
}

#[impl_trait_for_tuples::impl_for_tuples(30)]
impl ClaimAssets for Tuple {
	fn claim_assets(
		origin: &Location,
		ticket: &Location,
		what: &Assets,
		context: &XcmContext,
	) -> Option<AssetsInHolding> {
		for_tuples!( #(
			if let Some(a) = Tuple::claim_assets(origin, ticket, what, context) {
				return Some(a);
			}
		)* );
		None
	}
}
```
