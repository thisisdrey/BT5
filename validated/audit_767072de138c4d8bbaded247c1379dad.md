Audit Report

## Title
`ERC20Transactor` treats successful (but non-standard) ERC20 transfers as failed, causing permanent loss/lock of user funds - (File: `cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs`)

## Summary
`ERC20Transactor::withdraw_asset_with_surplus` and `deposit_asset_with_surplus` invoke an ERC20 `transfer()` via `pallet_revive::Pallet::<T>::bare_call` and only treat `return_value.did_revert() == true` as failure; a successful (non-reverted) call whose return data is not strictly ABI-decodable as `bool` (e.g., an ERC20 that returns no data on `transfer`, mirroring real-world non-conformant tokens like mainnet USDT) is routed through `abi_decode_returns_validate`, which errors out and causes the whole XCM operation to report failure even though the on-chain token transfer already executed. This causes user funds to be permanently stuck in `TransfersCheckingAccount` on withdraw, or duplicate-settlement/asset-trap inconsistency on deposit.

## Finding Description
In `withdraw_asset_with_surplus`, after the `bare_call` to `transfer(address,uint256)` succeeds without reverting, the code unconditionally calls `IERC20::transferCall::abi_decode_returns_validate(&return_value.data)` and maps any decode error to `Err(XcmError::FailedToTransactAsset("ERC20 contract result couldn't decode"))`, aborting the XCM operation despite the EVM-level transfer to `TransfersCheckingAccount` having already occurred: [1](#0-0) 

The identical pattern exists in `deposit_asset_with_surplus`, where a non-reverted transfer from `TransfersCheckingAccount` to the beneficiary that returns non-decodable data results in `Err((what, XcmError::FailedToTransactAsset("ERC20 contract result couldn't decode")))`, even though the beneficiary's ERC20 balance has already been credited: [2](#0-1) 

The only success/failure signal derived from a non-reverted call is whether `abi_decode_returns_validate` can strictly decode the return bytes as a `bool` — not whether the EVM state (ERC20 balances) actually changed. Any ERC20 contract implementing `transfer` without returning a `bool` (empty return data) will always hit this decode-failure branch, deterministically converting a successful transfer into a reported XCM/transactor failure.

## Impact Explanation
This maps to the "permanent user-fund or bridge-state lock" and "duplicate settlement" impact categories:
- On withdraw, tokens move from `who` to `TransfersCheckingAccount` on-chain, but the function returns `Err`, so the XCM executor aborts the program with no compensating transfer back to `who` — the user's tokens become permanently stuck in `TransfersCheckingAccount`.
- On deposit, tokens move from `TransfersCheckingAccount` to the beneficiary on-chain, but the function returns `Err((what, ...))`, causing the XCM executor to trap the `AssetsInHolding` via the asset-trap mechanism — creating a state where the same value is both delivered on-chain and recorded as trapped/reclaimable, enabling a beneficiary to receive value twice (once via the real `_transfer`, once via `claim_assets`).

## Likelihood Explanation
No privileged action is required to trigger this: any unprivileged user routing an XCM operation through `ERC20Transactor` for an ERC20 asset whose `transfer` does not strictly return a `bool` will deterministically hit this path on every such transfer, since the branching depends purely on `did_revert()` and ABI-decodability of the return payload, not on the actual token movement.

## Recommendation
Do not treat non-reverted calls with non-decodable (e.g., empty) return data as failures. Follow the `safeTransfer` pattern: treat a non-reverted call as success when return data is empty or decodes to `true`, and only treat a non-reverted call with return data that explicitly decodes to `false` as failure. Update both `withdraw_asset_with_surplus` and `deposit_asset_with_surplus` accordingly.

## Proof of Concept
1. Register (via `Matcher`) an ERC20 contract on `pallet-revive` whose `transfer(address,uint256)` performs the transfer but returns no data (mirroring mainnet USDT).
2. Trigger an XCM operation that calls `ERC20Transactor::withdraw_asset_with_surplus` for this asset.
3. Observe that the internal `bare_call` succeeds (`did_revert() == false`), the ERC20 balance moves from `who` to `TransfersCheckingAccount`, but `IERC20::transferCall::abi_decode_returns_validate(&return_value.data)` fails on the empty return data at [3](#0-2) , causing the function to return `Err(XcmError::FailedToTransactAsset("ERC20 contract result couldn't decode"))` and aborting the XCM program with the user's tokens now stuck in `TransfersCheckingAccount`.

### Citations

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L185-194)
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
