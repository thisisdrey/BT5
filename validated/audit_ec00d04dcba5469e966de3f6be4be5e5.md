Audit Report

## Title
`ERC20Transactor` credits the full requested `amount` on withdrawal/deposit without verifying the checking account's actual ERC20 balance delta, allowing fee-on-transfer/deflationary tokens to insolvent the shared escrow - (File: `cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs`)

## Summary
`ERC20Transactor::withdraw_asset_with_surplus` calls `IERC20::transfer` to move tokens from a user into the shared `TransfersCheckingAccount`, and unconditionally credits `AssetsInHolding` with the requested `amount` if the ABI-decoded return value is `true`, regardless of how many tokens actually arrived. `deposit_asset_with_surplus` mirrors this on the payout side, releasing `amount` from `AssetsInHolding` and calling `transfer(beneficiary, amount)` from the checking account, again trusting the boolean return without checking the real balance decrease. Because `ERC20Matcher` (used to instantiate this transactor on Asset Hub) matches any location of the form `AccountKey20`, any permissionlessly deployed `pallet_revive` contract can be treated as a fungible ERC20 asset through this transactor.

## Finding Description
In `withdraw_asset_with_surplus`, after `Matcher::matches_fungibles(what)` resolves `(asset_id, amount)`, the code builds `IERC20::transferCall { to: checking_address, value: amount }`, executes it via `pallet_revive::Pallet::<T>::bare_call`, and on `Ok(true)` immediately mints `AssetsInHolding::new_from_fungible_credit(what.id.clone(), Box::new(Erc20Credit(amount)))` [1](#0-0) [2](#0-1) . No `balanceOf` check on `TransfersCheckingAccount` before/after the transfer is performed; the credited amount is derived solely from the requested `amount`, not from an observed balance delta.

Symmetrically, `deposit_asset_with_surplus` calls `transfer(beneficiary, amount)` from `TransfersCheckingAccount` and treats `Ok(true)` as full settlement without verifying the checking account's balance actually decreased by `amount` [3](#0-2) .

The `Erc20Credit` imbalance type explicitly documents that it performs no runtime-level balance enforcement, delegating all correctness to the ERC20 contract itself [4](#0-3) .

Critically, the asset being ERC20-compliant/well-behaved is not enforced by any permissioned registration step: `ERC20Matcher` matches on `IsLocalAccountKey20`, i.e. any `Location` of the shape `(0, [AccountKey20 { .. }])`, converting it directly to an `H160` contract address with no allowlist or governance gate [5](#0-4) . Combined with `pallet_revive`'s permissionless contract deployment, any user can deploy a fee-on-transfer/deflationary ERC20 that returns `true` on `transfer`/`transferFrom` while moving less than `value`, and have it processed by this transactor via `AssetTransactors` on Asset Hub Westend [6](#0-5) .

## Impact Explanation
Each withdrawal of a fee-on-transfer ERC20 through this transactor credits `Erc20Credit(amount)` into `AssetsInHolding` while `TransfersCheckingAccount` only actually receives `amount - fee` real tokens. This breaks the invariant that escrowed balances must conserve value and settle exactly once to the rightful beneficiary and amount. Repeated withdraw/deposit cycles accumulate a shortfall between the checking account's real on-chain ERC20 balance and the aggregate value the runtime believes is escrowed, eventually causing deposit/payout failures or under-delivery for other, unrelated users of the same asset — a permanent fund-lock/insolvency condition triggered entirely by unprivileged, ordinary use of a common ERC20 variant.

## Likelihood Explanation
Deploying arbitrary contracts via `pallet_revive` requires no privilege, and `ERC20Matcher`'s unconditional `AccountKey20`-based matching means no governance/registration gate stands between a user-deployed fee-on-transfer contract and this transactor's escrow logic. Triggering the underpayment only requires an ordinary XCM withdraw/deposit of such a token, making this reliably repeatable by any external, unprivileged actor.

## Recommendation
Before crediting `Erc20Credit(amount)` in `withdraw_asset_with_surplus`, read `TransfersCheckingAccount`'s ERC20 balance before and after the `transfer` call (via `balanceOf`) and credit only the observed delta. Symmetrically, in `deposit_asset_with_surplus`, verify the checking account's balance decreased by exactly the intended amount before treating the deposit as fully settled, and fail/roll back otherwise instead of trusting the boolean return value alone.

## Proof of Concept
1. Deploy a `pallet_revive` contract implementing `IERC20` whose `transfer`/`transferFrom` burns a fee (e.g. 5%) before crediting `to`, but still returns `true`.
2. Since `ERC20Matcher` matches any `AccountKey20` location with no allowlist [7](#0-6) , an XCM program can reference this contract address directly as the asset for `ERC20Transactor`.
3. User A executes an XCM `WithdrawAsset` for `amount = 1000` of this token; `withdraw_asset_with_surplus` calls `transfer(checking_address, 1000)`, the contract moves only `950` and returns `true`, and the code credits `Erc20Credit(1000)` regardless [8](#0-7) .
4. Repeat across multiple withdraw/deposit cycles; the checking account's real balance falls persistently below the sum of `Erc20Credit` amounts tracked by the runtime, eventually causing a legitimate beneficiary's `deposit_asset_with_surplus` transfer to fail or under-deliver [9](#0-8) .

### Citations

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L73-89)
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
```

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L159-169)
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
