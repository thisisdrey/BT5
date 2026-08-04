Confirmed: `ERC20Transactor` is wired into production runtime config at `cumulus/parachains/runtimes/assets/asset-hub-westend/src/xcm_config.rs`, so `erc20_transactor.rs` is live XCM asset-transactor code, not test-only (unlike `substrate/frame/revive/src/impl_fungibles.rs`, which is gated by `#![cfg(any(feature = "std", feature = "runtime-benchmarks", test))]` and therefore out of scope).

### Title
ERC20 transfer success wrongly inferred from ABI-decodable `bool` return, causing permanent fund lock for non-standard tokens (USDT-style) - (File: cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs)

### Summary
`ERC20Transactor::withdraw_asset_with_surplus` and `deposit_asset_with_surplus` execute an ERC20 `transfer` call via `pallet_revive::Pallet::<T>::bare_call` and then require the call's return data to ABI-decode as `bool` via `IERC20::transferCall::abi_decode_returns_validate`. Real-world non-standard ERC20 tokens (e.g. mainnet USDT and clones) execute `transfer` successfully but return no data at all instead of an ABI-encoded `bool`. This is exactly the bug class described in the external report: checking a `bool` return that a legitimate, successfully-executed token call doesn't actually produce.

### Finding Description
In `withdraw_asset_with_surplus` [1](#0-0) , the transfer call is dispatched, and if it did not revert, the code does: [2](#0-1) 
If `abi_decode_returns_validate` fails to decode `return_value.data` as a `bool` (empty return data, as with real USDT), the function returns `Err(XcmError::FailedToTransactAsset("ERC20 contract result couldn't decode"))` — even though the underlying ERC20 `transferCall` already executed and moved the caller's tokens into the checking account `TransfersCheckingAccount`. Because the withdraw is reported as a hard XCM error, the XCM executor treats the asset as never withdrawn: no `AssetsInHolding` credit is created for use by subsequent instructions (deposit, reserve, teleport, forward), while the real on-chain ERC20 balance has already been debited from `who` and credited to the checking account.

The symmetric issue exists in `deposit_asset_with_surplus` [3](#0-2) : the checking account's real ERC20 balance is transferred to the beneficiary, but if the token doesn't return a decodable `bool`, the deposit is reported as failed and the `AssetsInHolding` (`what`) is returned to the caller for trapping — while the ERC20 tokens have actually already moved to the beneficiary's address on the token contract itself.

### Impact Explanation
For any ERC20 asset registered on Asset Hub through this transactor whose implementation does not return an ABI-encoded `bool` on `transfer` (the exact class of non-compliant token described in the source report, e.g. USDT), a `WithdrawAsset` XCM instruction causes:
- The user's real ERC20 balance to be moved into `TransfersCheckingAccount` (an actual, successful transfer on-chain).
- The XCM executor to record the operation as failed, so no matching credit ever enters `AssetsInHolding`.

This results in permanent loss/lock of the user's ERC20 tokens: they are moved out of the user's control into the checking account, but the XCM message that should route them onward (deposit, teleport, reserve-transfer) fails, and there is no compensating mechanism to reclaim the discrepancy from the checking account. This matches the "permanent user-fund … lock" impact category for the Polkadot SDK program.

### Likelihood Explanation
Likelihood is high and requires no privileged actor: any unprivileged user submitting a normal XCM `WithdrawAsset`/`TransferAsset` message for an ERC20 asset registered under `ERC20Transactor` triggers this path automatically if the underlying token is non-standard (doesn't return `bool`). No malicious peer, validator, or governance action is needed — only the pre-existing condition that a non-compliant ERC20 contract (like real USDT) is used as the asset. This is the same condition flagged as high-severity in the original ERC20 bug report.

### Recommendation
Do not rely solely on ABI-decodability of the return value to determine transfer success. Instead:
- Treat a non-reverting call with empty/undecodable return data as success (matching how SafeERC20/SafeTransferLib in Solidity treat token calls that omit the boolean return), rather than as a hard failure.
- Only treat `did_revert() == true` as failure, and only treat `Ok(false)` (an explicit, successfully-decoded `false`) as failure; treat decode failures the same as decode success with `true` if the call didn't revert, since a non-reverting call to a real ERC20 implies balance movement occurred.
- Alternatively/additionally, verify by reading the ERC20 `balanceOf` before and after the call to confirm expected balance movement rather than only trusting the boolean return, eliminating the reliance on the return value entirely.

### Proof of Concept
1. Register a non-standard ERC20 token contract (mirroring mainnet USDT's `transfer`, which performs the transfer but returns no data) as the asset for `ERC20Transactor` on Asset Hub Westend (via the existing `xcm_config.rs` wiring at `cumulus/parachains/runtimes/assets/asset-hub-westend/src/xcm_config.rs`).
2. Have a user submit an XCM message containing a `WithdrawAsset` for that token (e.g., via `pallet_xcm::transfer_assets`), routed through `ERC20Transactor::withdraw_asset_with_surplus`.
3. Observe: `bare_call` returns `Ok(return_value)` with `return_value.did_revert() == false` and `return_value.data` empty (no bool encoding), because the underlying token's `transfer` succeeded but doesn't return `bool` per the standard, non-compliant behavior.
4. `IERC20::transferCall::abi_decode_returns_validate(&return_value.data)` fails to decode, producing `Err(XcmError::FailedToTransactAsset("ERC20 contract result couldn't decode"))` per [2](#0-1) .
5. Confirm via the token's own `balanceOf` that the user's balance was already debited and the checking account credited (the real transfer succeeded), while the XCM instruction returns an error and no asset enters the holding register — demonstrating the fund lock.

### Citations

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L185-208)
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
