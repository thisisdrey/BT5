## Finding

### Title
ERC20 transfers whose return data cannot be ABI-decoded as `bool` are treated as failures even though the underlying token balance has already moved, permanently trapping funds in `TransfersCheckingAccount` - (File: `cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs`)

### Summary
`ERC20Transactor` implements `TransactAsset` for XCM by issuing a raw Solidity `IERC20::transfer(address,uint256)` call via `pallet_revive::Pallet::<T>::bare_call` and then interpreting success purely from the ABI-decoded `bool` return value of that call. Just like the reported `sendAllFundsToLP()` bug — which assumed every ERC20 always returns a boolean from `transfer()` — this code assumes the same thing. When a contract executes the transfer (balances actually move) but returns no boolean-decodable payload, or the boolean decode fails for any other ABI-related reason, the transactor treats the operation as failed, even though the on-chain balance change already happened.

### Finding Description
`withdraw_asset_with_surplus` moves tokens from the user to the pallet's `TransfersCheckingAccount` by calling the ERC20 contract's `transfer`: [1](#0-0) 

The result is then interpreted strictly by ABI-decoding a `bool`: [2](#0-1) 

Symmetrically, `deposit_asset_with_surplus` moves tokens out of `TransfersCheckingAccount` to the beneficiary and decodes the return value the same way: [3](#0-2) 

`did_revert()` correctly distinguishes an EVM revert from a completed call, but the completed-call branch treats "return data does not ABI-decode to `bool` == `true`" identically to "transfer failed." For a contract that executed a real state-changing `transfer` — moving the caller's balance into `TransfersCheckingAccount` (in `withdraw_asset_with_surplus`) or moving `TransfersCheckingAccount`'s balance into the beneficiary (in `deposit_asset_with_surplus`) — but does not return a decodable `bool` (i.e. behaves like the historically non-compliant tokens named in the report, USDT/BNB-style tokens), the code path returns `XcmError::FailedToTransactAsset` while the actual ERC20 ledger inside the token contract has already been mutated.

Because the actual value transfer happens inside an external contract call (not inside pallet storage that XCM's holding-register abstraction tracks), the failure branch does not — and cannot — reverse the on-chain ERC20 balance movement. The XCM executor only sees the abstract `AssetsInHolding`/`Erc20Credit` bookkeeping fail; it has no mechanism to claw back the tokens that already moved into or out of `TransfersCheckingAccount`.

### Impact Explanation
For `withdraw_asset_with_surplus`, a return-decode failure on an otherwise-successful transfer leaves the user's tokens sitting in the pallet's `TransfersCheckingAccount` while the XCM program aborts with an error — the user's funds are debited but never credited into holding, and thus never delivered anywhere. For `deposit_asset_with_surplus`, the same failure after a successful checking-account-to-beneficiary transfer leaves tokens already sent to the beneficiary while the function still reports failure and returns the abstract `Erc20Credit` back to holding — causing either double-accounting or (depending on the caller's handling of `AssetsInHolding` on error, e.g. trapping) a permanent lock of value with no path back to the token contract, since `TransfersCheckingAccount` is a system/pot account not reachable by ordinary user transactions. This matches the "permanent user-fund or bridge-state lock" and "duplicate settlement" categories in the impact gate.

### Likelihood Explanation
This is triggerable by any unprivileged user simply by registering (or having registered) an ERC20 contract as a `pallet_revive`-backed asset whose `transfer` implementation does not return a strict two's-complement `true`/`false` word decodable via `abi_decode_returns_validate` — a real-world, common category of tokens (explicitly the same class called out in the source report: USDT/BNB-style tokens, proxies, or any token returning extra/no data). No malicious peer, validator, or governance action is needed; only an asset registration (which on Asset Hub is either permissionless or low-privilege for foreign/ERC20 asset classes) and a normal XCM transfer using that asset.

### Recommendation
Do not conflate "call succeeded but return data isn't a decodable `bool`" with "transfer failed." At minimum:
- Treat non-decodable-but-non-reverting return data as a *hard error that aborts the whole XCM message before any external call is made* (i.e., pre-validate token compliance at asset registration time, not at transfer time), so that no on-chain ERC20 state mutation occurs before the decode check.
- Alternatively, make the withdraw/deposit calls atomic with the abstract-accounting update by performing a compensating transfer back (or using a nested transactional bare_call that itself validates the return data and reverts internally) so that a decode failure guarantees no external balance change took place.
- Restrict `ERC20Transactor`/the asset registration path to only accept tokens whose `transfer`/`transferFrom` are verified (e.g., via a static call/simulation at registration time) to return a proper `bool`, consistent with the "team response" mitigation cited in the source report (limit support to strictly compliant tokens), and enforce this in code rather than only in documentation/policy.

### Proof of Concept
1. Deploy (or have registered) an ERC20-style contract `T` whose `transfer(address,uint256)` performs the balance update but returns no data (or returns a value that does not strictly ABI-decode to `bool`) — mirroring USDT/BNB-style non-compliant behavior.
2. Register `T` as an XCM-transactable asset routed through `ERC20Transactor` (e.g., via the asset-hub ERC20 asset registration flow used with `pallet_revive`).
3. Submit an XCM program that does `WithdrawAsset` for `T` from account `Alice`.
4. `withdraw_asset_with_surplus` executes `bare_call` with `IERC20::transferCall{to: checking_address, value: amount}` against `T`; `T.transfer` succeeds and moves `amount` from `Alice` to `TransfersCheckingAccount` on the ERC20 ledger — verify via `T.balanceOf(checking_address)` increased and `T.balanceOf(alice)` decreased.
5. Because `return_value.data` cannot be decoded via `IERC20::transferCall::abi_decode_returns_validate`, the function returns `Err(XcmError::FailedToTransactAsset("ERC20 contract result couldn't decode"))`, causing the whole XCM program to fail.
6. Observe: `Alice`'s ERC20 balance is permanently reduced, `TransfersCheckingAccount`'s balance is permanently increased, yet no asset ever entered the XCM holding register and no error-recovery path returns the tokens to `Alice` — the funds are stuck in the pallet's checking account.

### Citations

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L166-181)
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
```

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L185-216)
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
