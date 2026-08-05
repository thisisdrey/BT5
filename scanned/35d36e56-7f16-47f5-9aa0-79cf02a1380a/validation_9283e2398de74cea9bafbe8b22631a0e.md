## Analysis

The `Blast_Adapter` bug is a class of "hard-coded protocol assumption breaks for a specific token, causing the bridging call to unconditionally revert/fail for that token, with no way for the caller to work around it." The direct, locally provable analog in this repository is in the new pallet-revive ERC20 `TransactAsset` implementation used by Asset Hub's XCM configuration. [1](#0-0) 

`ERC20Transactor` is wired into `AssetTransactors` on Asset Hub Westend, so it participates in ordinary XCM `WithdrawAsset`/`DepositAsset` execution for any ERC20 contract registered as an XCM-transactable asset via `ERC20Matcher`.

Both `withdraw_asset_with_surplus` and `deposit_asset_with_surplus` invoke the ERC20 `transfer` function through `pallet_revive::Pallet::<T>::bare_call` using a single, globally fixed `weight_limit = WeightLimit::get()` (bound to `ERC20TransferGasLimit`, a constant "taken from the real gas and deposits of a standard ERC20 transfer call"): [2](#0-1) [3](#0-2) 

The code itself acknowledges the design flaw in a comment on the failure branch of both functions: *"This error could've been duplicate smart contract, out of gas, etc. If the issue is gas, there's nothing the user can change in the XCM that will make this work since there's a hardcoded gas limit."* [4](#0-3) 

This is precisely the DAI/Blast_Adapter bug class transplanted into Substrate: any ERC20 contract whose `transfer` implementation legitimately requires more weight/gas than the single hardcoded `ERC20TransferGasLimit` (e.g. rebasing tokens, fee-on-transfer tokens, tokens with hooks/callbacks, proxy-pattern tokens with extra `DELEGATECALL` overhead) will have every `deposit_asset_with_surplus` call permanently fail with `FailedToTransactAsset("ERC20 contract execution errored")` — deterministically, for every transfer attempt, with no parameter a user can set in the XCM to fix it (analogous to `relayTokens` always reverting for DAI on Blast).

The impact is worse than the Blast case because of the two-step withdraw/deposit XCM flow: `withdraw_asset_with_surplus` first moves the real ERC20 balance into `TransfersCheckingAccount` (this call succeeds since it's the same kind of `transfer`, but note it's equally exposed to the same hardcoded-limit revert risk on the source side). If a later hop's `deposit_asset_with_surplus` then fails for the token due to insufficient hardcoded weight, the XCM executor traps the abstract `AssetsInHolding` credit (`Erc20Credit`) — but the real tokens already sit in `TransfersCheckingAccount`'s on-chain ERC20 balance, only redeemable through this same `deposit_asset_with_surplus` path. Since `check_in`/`check_out` are `Unimplemented` (no teleport reclamation path) and any reclaim/`ClaimAsset` attempt re-runs the identical `deposit_asset_with_surplus` transfer with the same fixed weight limit, the funds become **permanently locked** in `TransfersCheckingAccount` for that ERC20 token — an unrecoverable fund lock, not just a failed transaction.

### Title
Hard-coded ERC20 transfer weight limit in `ERC20Transactor` permanently fails and locks funds for ERC20 tokens whose `transfer` exceeds the fixed gas budget - (File: cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs)

### Summary
`ERC20Transactor::withdraw_asset_with_surplus` and `deposit_asset_with_surplus` call an ERC20 contract's `transfer` function via `pallet_revive::bare_call` using a single, globally configured `WeightLimit` (`ERC20TransferGasLimit`). Any ERC20 contract whose `transfer` legitimately needs more weight than this constant will unconditionally fail on every attempted transfer/deposit, and because the checking-account balance can only be moved out via the same code path, tokens already withdrawn into `TransfersCheckingAccount` become permanently stuck.

### Finding Description
`ERC20Transactor` is registered in `AssetTransactors` and handles XCM `WithdrawAsset`/`DepositAsset` for any ERC20 contract matched by `ERC20Matcher`. Both transact functions build an `IERC20::transferCall` and execute it with:
```
TransactionLimits::WeightAndDeposit { weight_limit: WeightLimit::get(), deposit_limit: StorageDepositLimit::get() }
```
where `WeightLimit` is the fixed `ERC20TransferGasLimit` constant sized for "a standard ERC20 transfer call." There is no per-token override or dynamic sizing, unlike the Snowbridge `ConstantGasMeter`, which itself needed a prior fix (`pr_7947`/`pr_8259`) because 100_000 gas was insufficient for tokens like LDO. The revive-based transactor never received an equivalent per-token adjustment — any ERC20 whose `transfer` implementation does more work than the hardcoded assumption (fee-on-transfer, rebasing, callback hooks, non-trivial storage writes) will exceed the weight limit and the `bare_call` will error out or the contract will run out of gas, hitting the explicit fallback branch that the code comments say cannot be worked around by the user.
This directly mirrors the `Blast_Adapter`/DAI bug class: a fixed, one-size-fits-all on-chain assumption about a token's execution behavior causes the transfer function to permanently fail for a subset of legitimate tokens.

### Impact Explanation
For any ERC20 asset registered for XCM transfer whose `transfer` implementation needs more weight than `ERC20TransferGasLimit`, every deposit will fail. Because `withdraw_asset_with_surplus` already moved the token balance into `TransfersCheckingAccount` before `deposit_asset_with_surplus` is invoked, and reclaiming trapped assets re-executes the same under-provisioned `transfer` call, funds for that token become permanently locked in the checking account with no available recovery path (teleport check-in/check-out is `Unimplemented`). This is a permanent user-fund lock triggered purely by normal use of a registered ERC20 asset, not by a malicious actor.

### Likelihood Explanation
Likelihood is high for any ERC20 contract deployed through pallet-revive with non-trivial `transfer` logic (common patterns: fee-on-transfer, rebasing balances, transfer hooks/callbacks, proxy delegatecall overhead). No privileged action is required — an ordinary user attempting to move such a token through the registered `ERC20Transactor` path triggers the failure deterministically and repeatably.

### Recommendation
Replace the single hardcoded `WeightLimit` with either (a) a per-asset configurable weight/gas budget determined at asset registration time, or (b) a dynamic weight probe/dry-run before committing the transfer, and ensure any failure path unwinds cleanly (e.g., returning the ERC20 balance to the original owner) rather than leaving it stranded in `TransfersCheckingAccount` with no viable exit path.

### Proof of Concept
1. Deploy (or register) an ERC20 contract via pallet-revive whose `transfer` function performs additional logic (e.g., a fee-on-transfer or hook-calling implementation) such that its weight consumption exceeds `ERC20TransferGasLimit` (500_000_000_000 ref-time / 10MiB proof, per `cumulus/parachains/runtimes/assets/asset-hub-westend/src/xcm_config.rs:215`).
2. Register this contract as an XCM-transactable asset matched by `ERC20Matcher`.
3. Submit an XCM `WithdrawAsset`/`DepositAsset` (or a full reserve/local transfer) moving this token from a user account through `ERC20Transactor`.
4. Observe `withdraw_asset_with_surplus` succeeds (moving the token into `TransfersCheckingAccount`), but `deposit_asset_with_surplus` fails with `XcmError::FailedToTransactAsset("ERC20 contract execution errored")` because `bare_call` exceeds `weight_limit`.
5. Any subsequent attempt to deposit/claim the trapped asset re-executes the identical under-provisioned `transfer` call and fails identically, leaving the token balance stuck in `TransfersCheckingAccount` indefinitely.

### Citations

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/src/xcm_config.rs (L213-237)
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
```

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L253-266)
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
```

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L299-305)
```rust
		} else {
			tracing::debug!(target: "xcm::transactor::erc20::deposit", ?result, "Error");
			// This error could've been duplicate smart contract, out of gas, etc.
			// If the issue is gas, there's nothing the user can change in the XCM
			// that will make this work since there's a hardcoded gas limit.
			Err((what, XcmError::FailedToTransactAsset("ERC20 contract execution errored")))
		}
```
