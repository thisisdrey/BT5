Audit Report

## Title
`ERC20Transactor` trusts nominal `transfer()` amount instead of verifying actual balance delta, breaking XCM value conservation for fee-on-transfer/rebasing tokens - (File: `cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs`)

## Summary
`withdraw_asset_with_surplus` and `deposit_asset_with_surplus` in `ERC20Transactor` call the ERC20 contract's `transfer()` for the nominal XCM `amount` and, upon a decoded `true` boolean return, unconditionally credit/report that exact nominal `amount` into the XCM holding ledger without reading `balanceOf()` before/after to confirm the real balance delta. Any ERC20 contract matched via `AccountKey20` (fee-on-transfer, rebasing, or otherwise non-standard transfer semantics) can therefore cause the XCM ledger's belief about the shared `TransfersCheckingAccount` balance to diverge from its real on-chain balance.

## Finding Description
`Matcher::matches_fungibles(what)` extracts `(asset_id, amount)` from the XCM asset descriptor with no allowlist restricting eligible contract addresses [1](#0-0) , consistent with the PR description stating any `AccountKey20`-addressed contract is matched and its `transfer` invoked [2](#0-1) .

In `withdraw_asset_with_surplus`, the code calls `transfer(checking_address, amount)` via `pallet_revive::Pallet::<T>::bare_call`, and on decoded `true` return, credits `AssetsInHolding::new_from_fungible_credit` with the fixed nominal `Erc20Credit(amount)` — not an observed balance delta [3](#0-2) . Symmetrically, `deposit_asset_with_surplus` transfers `amount` from the checking account to the beneficiary and reports unconditional success (`Ok(surplus)`) on `Ok(true)`, again with no `balanceOf` verification [4](#0-3) . There is no `balanceOf()` call anywhere in this file; the entire trust chain rests on the boolean return value of `transfer()`.

The runtime wiring in `asset-hub-westend/src/xcm_config.rs` confirms this transactor is live for Asset Hub Westend, registered generically over `assets_common::ERC20Matcher` with no contract allowlist visible in the matcher wiring itself [5](#0-4) .

## Impact Explanation
This breaks the invariant that contract-held/XCM-tracked value must conserve and settle exactly to the rightful amount. Because `TransfersCheckingAccount` is a shared pool backing all XCM-tracked balances of a given ERC20 asset, a fee-on-transfer or rebasing token registered under this transactor causes the pooled checking account's real balance to diverge from the sum of nominal `Erc20Credit` amounts the XCM ledger believes it holds — creating either phantom backing (on withdraw) or under-delivery to beneficiaries (on deposit), and potentially causing later legitimate withdrawals against the shared pool to fail once it is drained below the aggregate claimed balance, i.e., a permanent fund lock for some users. This matches the "Balances/contract-held value must conserve value and settle exactly once" pivot and the "permanent user-fund or bridge-state lock" impact category.

## Likelihood Explanation
Likelihood depends entirely on an ERC20 contract with non-standard transfer semantics being matched by this transactor. Since matching is based purely on `AccountKey20` address encoding with no code/behavior allowlist enforced in the reviewed transactor or its wiring, and since `pallet-revive` contracts referenced here can be arbitrary user-deployed contracts (not curated mainnet Ethereum contracts), an unprivileged party can deploy such a token and trigger the flaw purely through standard XCM `WithdrawAsset`/`DepositAsset` execution — no privileged, validator, or off-chain-relayer action is required. Note: this is currently deployed only on Asset Hub Westend (a testnet) per the wiring found; whether an equivalent allowlist gate exists elsewhere in governance/registration flow outside the reviewed files was not fully verifiable within the scope explored.

## Recommendation
In both `withdraw_asset_with_surplus` and `deposit_asset_with_surplus`, read `balanceOf()` on the relevant account (checking account or beneficiary) before and after the `transfer()` call, and use the observed delta — not the requested `amount` — as the value credited into `AssetsInHolding` or reported as surplus/success. Alternatively, gate the `Matcher`/registration path behind a governance-curated allowlist of ERC20 contracts verified to implement standard, non-fee, non-rebasing transfer semantics, rejecting unknown contracts by default.

## Proof of Concept
1. Deploy an ERC20 contract on `pallet-revive` implementing a fee-on-transfer `transfer()` that returns `true` while delivering only `value * 99/100` to the recipient.
2. Encode its address as an `AccountKey20` XCM asset location; it is matched by `ERC20Transactor`'s `Matcher` with no additional validation [1](#0-0) .
3. Execute an XCM `WithdrawAsset` for `amount` of this token; `withdraw_asset_with_surplus` calls `transfer(checking_address, amount)`, which returns `true` but moves only `99%` of `amount` into `TransfersCheckingAccount`; the code still credits `Erc20Credit(amount)` (full nominal amount) into the XCM holding register [6](#0-5) .
4. Repeat withdraws/deposits to accumulate a shortfall between the checking account's real `balanceOf` and the aggregate nominal amounts XCM believes are backed, until a legitimate withdrawal by another holder of the same token fails with `"ERC20 contract transfer failed"` due to insufficient real balance, locking those funds.

### Citations

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L159-159)
```rust
		let (asset_id, amount) = Matcher::matches_fungibles(what)?;
```

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L168-207)
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

**File:** prdoc/stable2506/pr_7762.prdoc (L9-13)
```text
      This PR introduces an Asset Transactor for dealing with ERC20 tokens and adds it to Asset Hub
      Westend.
      This means asset ids of the form `{ parents: 0, interior: X1(AccountKey20 { key, network }) }` will be
      matched by this transactor and the corresponding `transfer` function will be called in the
      smart contract whose address is `key`.
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
