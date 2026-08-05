Audit Report

## Title
Unverified ERC20 transfer amount accounting allows unbacked credit/mint via fee-on-transfer or non-conserving tokens - (File: `cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs`)

## Summary
`ERC20Transactor::withdraw_asset_with_surplus` and `deposit_asset_with_surplus` credit/debit the XCM `AssetsInHolding` register with the nominal requested `amount` as soon as the `IERC20::transfer` call returns `true`, without ever checking `balanceOf` deltas on `TransfersCheckingAccount` or the beneficiary. Any ERC20-shaped contract that is fee-on-transfer, deflationary, or rebasing can return `true` while moving less than `amount`, producing a mismatch between the nominal amount credited into XCM holding and the real tokens actually held in escrow.

## Finding Description
In `withdraw_asset_with_surplus`, the transactor issues `IERC20::transferCall { to: checking_address, value: amount }` via `pallet_revive::Pallet::<T>::bare_call`, and solely inspects `return_value.did_revert()` and the ABI-decoded boolean return value before minting `AssetsInHolding::new_from_fungible_credit(what.id.clone(), Box::new(Erc20Credit(amount)))` for the full nominal `amount`. [1](#0-0) 

Symmetrically, `deposit_asset_with_surplus` transfers `amount` from `TransfersCheckingAccount` to the beneficiary and, again, only checks the boolean return value (`Ok(true)` → success) with no post-transfer balance verification. [2](#0-1) 

Neither function reads `balanceOf` of the checking/beneficiary account before and after the `transfer` call to confirm the actual amount moved matches the requested `amount`. This is a real gap: `Erc20Credit`, the `ImbalanceAccounting` implementation backing this credit, performs no runtime-enforced balance check either — its own doc comment states "the actual balance constraints are enforced by the ERC20 smart contract itself rather than the runtime." [3](#0-2) 

This is wired into a live runtime as `ERC20Transactor` in `AssetTransactors` on Asset Hub Westend, meaning any contract matched by `ERC20Matcher` can participate in real XCM programs. [4](#0-3) 

## Impact Explanation
This falls under theft/unbacked mint: an attacker deploying a fee-on-transfer, deflationary, or rebasing ERC20-shaped contract via `pallet-revive` and matched as an XCM asset can `WithdrawAsset` a nominal `amount`, have the executor credit the full `amount` into `AssetsInHolding` even though `TransfersCheckingAccount`'s real balance increased by less, and then forward that nominal credit via reserve-transfer XCM instructions to mint a wrapped asset of equal nominal value on a remote chain — permanently unbacking the reserve custody in `TransfersCheckingAccount` by the shortfall amount. This is a public, unprivileged-attacker path reachable through ordinary XCM `execute`/message processing with no reliance on malicious validators, relayers, or governance.

## Likelihood Explanation
The vulnerability requires only that a non-value-conserving contract satisfying the `IERC20` ABI shape be deployable and matchable by `ERC20Matcher`, and that `ERC20Transactor` be wired into a live `AssetTransactors` tuple — both conditions are confirmed true for `asset-hub-westend`. Deploying arbitrary contracts via `pallet-revive` and constructing an XCM program with `WithdrawAsset`/reserve-transfer instructions is an unprivileged, public-entrypoint action, making this readily exploitable and repeatable.

## Recommendation
In both `withdraw_asset_with_surplus` and `deposit_asset_with_surplus`, read `balanceOf` of `TransfersCheckingAccount`/beneficiary before and after the `transfer` call and credit/debit `AssetsInHolding` based on the observed balance delta, rejecting the instruction (returning `XcmError::FailedToTransactAsset`) if the delta does not exactly equal the requested `amount`, so fee-on-transfer, deflationary, or rebasing tokens cannot create a mismatch between real token custody and virtual XCM-accounted value.

## Proof of Concept
1. Deploy a `pallet-revive` contract implementing `IERC20` whose `transfer(to, value)` burns 50% of `value`, forwards only 50% to `to`, but still returns `true`.
2. Have `ERC20Matcher` match this contract's address as an asset id, and construct an XCM program via `pallet_xcm::execute` containing `WithdrawAsset(<this token>, amount)` followed by `DepositReserveAsset`/`InitiateReserveWithdraw` to a remote chain treating this Asset Hub as reserve.
3. Observe `withdraw_asset_with_surplus` credits `AssetsInHolding` with the full `amount` (lines 195-203 of `erc20_transactor.rs`) even though `TransfersCheckingAccount`'s real ERC20 balance only increased by `amount/2`.
4. The remote chain mints a wrapped asset worth `amount`, while only `amount/2` in real tokens back it in `TransfersCheckingAccount` — an unbacked mint of `amount/2`, reproducible as a unit test asserting `balanceOf(checking_account)` delta ≠ credited `AssetsInHolding` amount.

### Citations

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

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L166-203)
```rust
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
		// We need to return this surplus for the executor to allow refunding it.
		let surplus = weight_limit.saturating_sub(weight_consumed);
		tracing::trace!(target: "xcm::transactor::erc20::withdraw", ?weight_consumed, ?surplus, ?storage_deposit);
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
