Confirmed: the `assets_common::ERC20Transactor` is wired into Asset Hub Westend's `AssetTransactors` and matches any asset identified by `{parents: 0, interior: X1(AccountKey20 { key, network })}`, meaning **any smart contract deployed via `pallet-revive`** can be referenced as an ERC20 asset in XCM — this is permissionless, matching the "public underpriced work" / unbacked-mint requirement without needing a privileged actor.

### Title
XCM ERC20 asset transactor credits nominal `amount` into holding instead of actual balance received, enabling unbacked value creation with fee-on-transfer/non-standard ERC20 contracts - (File: cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs)

### Summary
`ERC20Transactor::withdraw_asset_with_surplus` calls an arbitrary user-deployed ERC20 contract's `transfer(to, amount)` via `pallet_revive::Pallet::bare_call`, and if the boolean return value is `true` (no revert), it unconditionally credits `AssetsInHolding` with the nominal `amount` requested by the XCM instruction — never checking the actual balance change of the `TransfersCheckingAccount`. This is the same bug class as the reported `ACOToken::_transferFromERC20`: trusting the nominal transferred amount instead of the real balance delta, with the added risk that a boolean-success ERC20 is not guaranteed to move exactly `amount` tokens (fee-on-transfer, rebasing, or non-compliant "returns true always" tokens).

### Finding Description
`withdraw_asset_with_surplus` [1](#0-0)  is registered as an `AssetTransactor` on Asset Hub Westend for any asset location of the form `AccountKey20 { key, network }` [2](#0-1) . Per the PR description, this matches *any* smart contract address deployed via `pallet-revive`, and the corresponding `transfer` function is invoked on that arbitrary contract [3](#0-2) .

The withdraw path builds an `IERC20::transferCall` for `amount` tokens, executes it via `bare_call`, and — if the call didn't revert and decodes to `true` — mints `AssetsInHolding` XCM holding value equal to the *requested* `amount`, not the amount actually moved: [4](#0-3) 

The comment on `Erc20Credit` explicitly acknowledges this design flaw: "the actual balance constraints are enforced by the ERC20 smart contract itself rather than the runtime" [5](#0-4) . This assumes every possible ERC20 contract strictly enforces `transfer(amount)` == balance delta of `amount` and reverts otherwise — precisely the non-guaranteed behavior called out in the external report (fee-on-transfer tokens, and ERC20-compliant tokens that return `true`/don't revert on partial or failed execution).

The symmetrical `deposit_asset_with_surplus` has the identical pattern: it releases holding credit and pays out `surplus` weight as if the transfer succeeded fully, based only on the boolean return value [6](#0-5) .

Existing guards do not stop this path:
- `return_value.did_revert()` only guards against outright EVM reverts, not fee/short-transfer semantics.
- The ABI-decoded boolean only reflects whatever the (attacker-controlled) contract chooses to return; a malicious or fee-on-transfer contract can return `true` while transferring less than `amount`, or more than `amount` to itself via reentrancy-like effects, without reverting.
- There is no post-call `balanceOf` check comparing pre/post balances of `TransfersCheckingAccount` against `amount`.

### Impact Explanation
Because any user can deploy a `pallet-revive` contract and reference it via XCM using its `AccountKey20` location, an attacker can deploy a token contract whose `transfer` function returns `true` unconditionally while moving zero or fewer tokens than `amount`. Withdrawing such an "asset" through XCM (e.g., a local `WithdrawAsset` + `DepositAsset`/`InitiateReserveWithdraw` to another chain, or exchanging via `ExchangeAsset`) causes `AssetsInHolding` to be credited with the full nominal `amount` even though the `TransfersCheckingAccount` received nothing (or less). This XCM holding value is fungible within the XCM executor and can then be deposited to a beneficiary account, teleported/reserve-transferred cross-chain, or exchanged against genuinely-backed assets in a pool — creating unbacked value out of an ERC20 asset accounting mismatch, which is the direct "theft or unbacked mint" impact category.

### Likelihood Explanation
High from an attacker-capability standpoint: deploying an arbitrary `pallet-revive` contract and referencing it by address in an XCM message is a fully public, unprivileged action — no governance, admin, relayer, or validator collusion is required. The only "cost" is deploying a trivial malicious/non-standard ERC20 contract and executing an XCM program against it, well within reach of any user with the ability to submit extrinsics on Asset Hub.

### Recommendation
Do not trust the boolean return value alone. Before and after the `transfer`/`transferFrom` `bare_call`, read `balanceOf` of the source and destination accounts and credit `AssetsInHolding` (or debit on deposit) with the *actual observed balance delta*, not the requested `amount`. If the delta doesn't match `amount` exactly (or falls short), treat it as a failed/partial transfer and return an `XcmError`, mirroring the “Warning about listing tokens” recommendation from the referenced report: enforce that the checking account's balance changes by exactly the desired amount.

### Proof of Concept
1. Deploy a `pallet-revive` contract implementing `IERC20` whose `transfer(to, value)` always returns `true` but only moves `value / 2` tokens (or `0` tokens) to `to`, without reverting.
2. Fund an account with this token and register the token's location as `{parents: 0, interior: X1(AccountKey20 { key: <contract address>, network: None })}`.
3. Submit an XCM program from that account: `WithdrawAsset(asset, amount)` for the malicious token, followed by `DepositAsset` to a different beneficiary (or `InitiateReserveWithdraw` to another chain).
4. Observe: `withdraw_asset_with_surplus` calls `transfer(checking_account, amount)`, the malicious contract returns `true` while moving less than `amount`, and `ERC20Transactor` still constructs `AssetsInHolding::new_from_fungible_credit(asset, Erc20Credit(amount))` for the full nominal `amount` [7](#0-6) .
5. The XCM executor then deposits or forwards this inflated `amount` of holding value to the beneficiary/destination chain, even though the `TransfersCheckingAccount`'s real token balance increased by less than `amount` — demonstrating unbacked value creation.

### Citations

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L73-78)
```rust
/// A minimal imbalance tracking type that holds an ERC20 token amount.
///
/// This type implements the necessary imbalance accounting traits but does not perform
/// runtime-level balance enforcement. It's used to track ERC20 token amounts within XCM
/// asset holdings, where the actual balance constraints are enforced by the ERC20 smart
/// contract itself rather than the runtime.
```

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L150-216)
```rust
	fn withdraw_asset_with_surplus(
		what: &Asset,
		who: &Location,
		_context: Option<&XcmContext>,
	) -> Result<(AssetsInHolding, Weight), XcmError> {
		tracing::trace!(
			target: "xcm::transactor::erc20::withdraw",
			?what, ?who,
		);
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

**File:** prdoc/stable2506/pr_7762.prdoc (L8-14)
```text
    description: |
      This PR introduces an Asset Transactor for dealing with ERC20 tokens and adds it to Asset Hub
      Westend.
      This means asset ids of the form `{ parents: 0, interior: X1(AccountKey20 { key, network }) }` will be
      matched by this transactor and the corresponding `transfer` function will be called in the
      smart contract whose address is `key`.
      If your chain uses `pallet-revive`, you can support ERC20s as well by adding the transactor, which lives
```
