## Analysis

This repository contains an analog of the ERC20 "unsafe transfer" bug class in `cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs`, which implements `TransactAsset` for `ERC20Transactor` — the XCM asset transactor used to treat arbitrary `pallet_revive` (EVM-compatible) contracts as fungible assets inside the XCM executor.

### Title
ERC20Transactor trusts nominal `transfer()` boolean without verifying actual balance delta, allowing fee-on-transfer/rebasing tokens to desynchronize XCM holding register from real contract balance - (File: `cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs`)

### Summary
`ERC20Transactor::withdraw_asset_with_surplus` and `::deposit_asset_with_surplus` move value by calling the target contract's Solidity `IERC20::transfer()` via `pallet_revive::Pallet::<T>::bare_call`, then trust the ABI-decoded boolean return value (`is_success`) as proof that exactly `amount` moved. Neither function reads the checking account's or beneficiary's actual balance before/after the call. Any ERC20 contract that returns `true` while transferring less than the requested `amount` (fee-on-transfer, rebasing, deflationary, or otherwise non-standard tokens) causes the XCM `AssetsInHolding` register to diverge from the real token balance held by `TransfersCheckingAccount`. [1](#0-0) [2](#0-1) 

### Finding Description
`withdraw_asset_with_surplus` (lines 150-216) calls `IERC20::transferCall` to move `amount` from the XCM origin (`who`) to a shared `TransfersCheckingAccount`. On decode success (`is_success == true`) it unconditionally credits the XCM holding register with `Erc20Credit(amount)` — the *requested* amount, not the amount actually received: [3](#0-2) 

Symmetrically, `deposit_asset_with_surplus` (lines 225-306) debits `amount` from holding and transfers from `TransfersCheckingAccount` to the beneficiary, again trusting the boolean return rather than measuring balance change: [4](#0-3) 

This is the exact broken invariant described in the external report: an ERC20 `transfer`/`transferFrom` call can report success (`true`) while moving a different amount than requested (fee-on-transfer, tax, or rebasing tokens), and code that trusts the boolean without checking the resulting balance treats the nominal amount as ground truth. Here that nominal amount becomes the XCM `AssetsInHolding` credit/debit, which is subsequently used by the XCM executor for further instructions (e.g. `DepositAsset`, `BuyExecution`, teleports to other chains via the wider XCM program) within the same message.

Because `TransfersCheckingAccount` is a single shared pooled account across all users and all ERC20 asset kinds handled by this transactor, and because any unprivileged account can deploy an arbitrary `pallet_revive` contract implementing `IERC20` (a fee-on-transfer/rebasing token) and then get it matched by `Matcher::matches_fungibles` as a fungible XCM asset, an attacker fully controls the token's `transfer` semantics while the transactor still credits the *full nominal* amount to the XCM holding register on `withdraw_asset_with_surplus`. The attacker can then use that phantom credited amount within the rest of the XCM program (e.g. `DepositAsset` to another beneficiary/account, or bridging onward) to move out more real value than was actually locked into the checking account, since the checking account only received `amount - fee`.

The existing guard (`return_value.did_revert()` + `abi_decode_returns_validate` for `true/false`) only defends against a hard revert or explicit `false` return — it does not defend against a truthful "success" report that silently moves a smaller amount, which is precisely the non-reverting-but-wrong-amount class of token behavior called out in the source report (analogous to a "successful" `transfer`/`transferFrom` that doesn't actually deliver the stated value).

### Impact Explanation
Because the checking account is shared, the discrepancy between "requested amount" and "actually-received amount" leaves the checking account under-collateralized relative to what the XCM holding register (and hence the runtime's global accounting for that asset) believes it holds. Over repeated withdraw operations with a hostile fee-on-transfer token, the checking account's real balance can be drained faster than the register decreases, and conversely on deposits the checking account can retain un-credited residual balances that become permanently stuck (no code path reads or reclaims a leftover balance mismatch). This falls under both "theft or unbacked mint" (phantom credited value usable in the same XCM program) and "permanent user-fund or bridge-state lock" (stranded residual balances in the checking account) impact classes.

### Likelihood Explanation
The attacker only needs to deploy a `pallet_revive` (EVM) contract implementing `IERC20` with custom, malicious `transfer` semantics (return `true` while moving less), and cause it to be matched as an XCM fungible asset via `Matcher::matches_fungibles` — both are actions available to an unprivileged user with no special node, validator, relayer, or governance access. No malicious peer/validator/relayer assumption is required; only ordinary control over a self-deployed contract, which is squarely within the "public underpriced work" / "unauthorized value creation via public entrypoint" pivot category.

### Recommendation
Instead of trusting the ABI-decoded `bool` return, measure the actual `balanceOf` delta of the recipient (`TransfersCheckingAccount` in `withdraw_asset_with_surplus`, the beneficiary in `deposit_asset_with_surplus`) before and after the `bare_call`, and credit/debit `AssetsInHolding` with the *observed* delta rather than the nominally requested `amount`. If the observed delta differs from `amount`, either fail the transaction (`FailedToTransactAsset`) or credit exactly the observed delta so the XCM holding register never diverges from real on-chain balances.

### Proof of Concept
1. Deploy a `pallet_revive` contract implementing `IERC20` where `transfer(to, value)` moves `value * 99 / 100` to `to` but returns `abi_encode(true)` unconditionally (fee-on-transfer token), registered so that `Matcher::matches_fungibles` recognizes it as a fungible XCM asset handled by `ERC20Transactor`.
2. Submit an XCM program from the attacker's account that performs `WithdrawAsset` of `amount` of this token (invoking `withdraw_asset_with_surplus`), then within the same program issue a `DepositAsset`/`InitiateTransfer` moving the *full nominal* `amount` (not `amount * 99/100`) out to a second account or onward to another chain.
3. Observe: `TransfersCheckingAccount`'s real ERC20 balance only increased by `amount * 99/100`, but `AssetsInHolding` was credited with the full `amount` at line 200 of `erc20_transactor.rs`, and the subsequent XCM instruction successfully moves the full nominal `amount`, so 1% of the value in every leg is fabricated relative to what is actually collateralized in the checking account.

### Citations

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L185-207)
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
