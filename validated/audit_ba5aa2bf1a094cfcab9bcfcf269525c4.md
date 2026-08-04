This confirms `ERC20Transactor` is wired into `AssetHub Westend`'s `xcm_config.rs`, making it live production XCM code, not a test-only path. [1](#0-0) 

### Title
Non-compliant ERC-20 return data causes fund loss/lock in `ERC20Transactor` XCM asset transactor - (File: `cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs`)

### Summary
`ERC20Transactor::withdraw_asset_with_surplus` and `ERC20Transactor::deposit_asset_with_surplus` invoke `IERC20::transferCall` on an arbitrary ERC-20 contract (the "asset id" is a contract address matched by `Matcher`) and then strictly ABI-decode the return data as a `bool` via `abi_decode_returns_validate`. [2](#0-1) [3](#0-2) 
This mirrors the `MorphoMarketFactory`/USDT bug class: the code assumes every ERC-20-like token strictly follows the ABI and returns a `bool` from `transfer`, but the actual token transfer (state mutation, balance movement) already executed inside `bare_call` **before** the return-data decode is attempted. If the target contract is a non-standard ERC-20 (e.g. it returns no data, like real-world USDT, or returns malformed data), the on-chain balance transfer has already happened, yet the transactor reports `XcmError::FailedToTransactAsset("ERC20 contract result couldn't decode")` and the XCM executor treats the operation as if it never occurred.

### Finding Description
`withdraw_asset_with_surplus` calls `pallet_revive::Pallet::<T>::bare_call` with an `IERC20::transferCall` to move tokens from the user to the `TransfersCheckingAccount`. [4](#0-3) 
This `bare_call` executes real state-changing EVM/PVM code: the token balance is actually debited from `who` and credited to the checking account as soon as the contract call runs (assuming it doesn't revert). Only *after* this state mutation does the code inspect `return_value.data` and try to `abi_decode_returns_validate` it as `bool`: [5](#0-4) 
If decoding fails (non-compliant token that doesn't return a bool, or returns extra/insufficient bytes), the function returns `Err(XcmError::FailedToTransactAsset(...))` even though the tokens were already moved on-chain. The XCM executor, seeing a hard error from `withdraw_asset`, does **not** create an `AssetsInHolding` credit for the withdrawn amount — the tokens are now stuck in the `TransfersCheckingAccount` with no corresponding holding entry, and no error-recovery path returns them to the user.

The mirror case is `deposit_asset_with_surplus`: it transfers from the checking account to the beneficiary, and only after the (already-executed) transfer does it attempt to decode the return value. [3](#0-2) 
On decode failure the function returns `Err((what, XcmError::FailedToTransactAsset(...)))`, handing the untouched `AssetsInHolding` (`what`) back to the caller as if the deposit never happened — while the beneficiary's on-chain ERC-20 balance was in fact already credited by the real `transferCall`. The XCM executor will then treat `what` as un-deposited holdings, which get trapped (`AssetTrap`) or otherwise re-processed, creating a duplicate-settlement path: the beneficiary already received the tokens from the successful `transferCall`, and the same value is now also retained/trapped in the XCM asset-holding bookkeeping.

This directly parallels the `MorphoMarketFactory` bug: the report's root cause is "code assumes ERC-20 always returns a spec-compliant boolean and reverts (or here, mis-reports) when it doesn't" — except here the on-chain effect (the transfer itself) has *already* happened by the time the check runs, so the failure isn't a harmless revert but a genuine bookkeeping desync between real token balances and the XCM asset-holding/trap accounting.

### Impact Explanation
- **Fund lock**: A non-standard ERC-20 registered as an XCM-transactable asset on Asset Hub can cause user withdrawals to silently debit the user and credit the checking account while the XCM message reports failure — the withdrawn amount becomes untracked and effectively lost/locked, satisfying the "permanent user-fund or bridge-state lock" impact category.
- **Duplicate settlement**: On the deposit side, the beneficiary's on-chain ERC-20 balance is credited by the real `transferCall`, but the XCM engine's internal holding/trap accounting still treats the amount as un-deposited `AssetsInHolding`, which can be trapped and later claimed again via `ClaimAsset`, producing a double credit to whoever claims the trap.
- This does not require a malicious relayer, validator, or governance actor — only a permissionlessly-created or already-registered non-standard ERC-20 contract used as an XCM asset, and normal XCM transfer traffic, satisfying the "unprivileged attacker / no privileged actor" requirement.

### Likelihood Explanation
Likelihood is Medium: it requires an ERC-20 contract with non-standard return semantics (no boolean return, or extra/short return data) to be routed through `ERC20Transactor`'s `Matcher`. Given `pallet-revive` permits arbitrary Solidity/PolkaVM contract uploads and USDT-style non-compliant tokens are common in production (this is literally the same real-world pattern that caused the referenced external Morpho bug), this is a realistic configuration once any such asset is matched/whitelisted by `Matcher::matches_fungibles`.

### Recommendation
- Decouple the on-chain transfer's success determination from strict ABI decoding of the return value: treat "call did not revert" (`!return_value.did_revert()`) as success by default, and only attempt to decode a boolean return value when present, falling back to trusting `did_revert()` when the return data is empty (mirroring how many wallets/backends and OpenZeppelin's `SafeERC20` treat empty return data as success).
- More importantly, ensure that any failure path (decode failure, non-bool return) either atomically rolls back the underlying transfer (wrap the `bare_call` in `frame_support::storage::with_transaction` and roll back on decode failure) or, if the transfer cannot be rolled back, credit the equivalent `AssetsInHolding`/trap state so the accounting stays consistent with the real on-chain balance movement.
- Add explicit test coverage using a "non-compliant ERC20" fixture (transfer that succeeds but returns no data or malformed data) for both `withdraw_asset_with_surplus` and `deposit_asset_with_surplus`.

### Proof of Concept
1. Deploy (or use an already-whitelisted) ERC-20 contract via `pallet-revive` whose `transfer(address,uint256)` performs the balance mutation but returns no data (or returns a non-bool encoded value) — analogous to real USDT's `approve`/`transfer` behavior.
2. Register/point `Matcher` (used by `ERC20Transactor` in `asset-hub-westend`'s `xcm_config.rs`) at this contract as a transactable fungible asset.
3. Send an XCM program that performs `WithdrawAsset` for this asset from a user account. `withdraw_asset_with_surplus` executes the `transferCall`, which actually moves the balance from `who` to `TransfersCheckingAccount` (verifiable via `IERC20::balanceOfCall` before/after).
4. Because `IERC20::transferCall::abi_decode_returns_validate(&return_value.data)` fails to decode the non-compliant return data, the function returns `Err(XcmError::FailedToTransactAsset("ERC20 contract result couldn't decode"))`. [6](#0-5) 
5. Observe: the user's balance decreased (real transfer happened), but the XCM instruction failed, and no `AssetsInHolding` credit representing the withdrawn amount exists — the tokens are stranded in the checking account with no way to reclaim them via the XCM path, demonstrating the fund-lock condition. The symmetric deposit-side PoC (using `deposit_asset_with_surplus`) demonstrates the beneficiary receiving tokens on-chain while `what` (the holding) is returned to the caller un-deposited, which can lead to duplicate accounting via asset trapping.

### Citations

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/src/xcm_config.rs (L1-1)
```rust
// Copyright (C) Parity Technologies (UK) Ltd.
```

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L166-208)
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
				} else {
					tracing::debug!(target: "xcm::transactor::erc20::withdraw", "contract transfer failed");
					Err(XcmError::FailedToTransactAsset("ERC20 contract transfer failed"))
				}
			}
```

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L251-298)
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
```
