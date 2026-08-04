## Analysis

The report's core broken invariant: **code assumes a spec-perfect ERC20 return value (`bool`) and treats any deviation from that assumption incorrectly, while the underlying token balance has already moved.** In the original bug, `transferFrom` reverts on non-bool-returning tokens (USDT/BNB-style), causing funds to get stuck in the strategy contract because the state transition never registers.

`ERC20Transactor` in this repository is a real, wired-in local analog: it is used as the `AssetTransactor` for ERC20-class assets in `AssetHubWestend`'s XCM configuration. [1](#0-0) 

### Title
Non-standard ERC20 tokens (no/void `bool` return, e.g. USDT/BNB-style) cause `ERC20Transactor::withdraw_asset_with_surplus` to report failure after the on-chain transfer already succeeded, permanently stranding user funds in the checking account - ([File: cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs])

### Summary
`ERC20Transactor::withdraw_asset_with_surplus` executes a real EVM `transfer()` call (via `pallet_revive::bare_call`) that physically moves the user's ERC20 balance to the `TransfersCheckingAccount`, and only afterward attempts to strictly ABI-decode a `bool` return value to decide whether the XCM-level withdrawal succeeded. Tokens that do not return a spec-compliant `bool` (the same class of non-standard ERC20 tokens named in the external report — USDT, BNB, and hundreds of others) will make `abi_decode_returns_validate` fail even though the transfer itself succeeded and did not revert. The function then returns `Err(XcmError::FailedToTransactAsset(...))`, so the XCM executor believes the withdrawal never happened and never credits `AssetsInHolding` for the amount. The tokens are left sitting in `TransfersCheckingAccount` with no accounting record tying them back to the user or to any subsequent deposit path.

### Finding Description [2](#0-1) 

The sequence is:
1. `data = IERC20::transferCall{ to: checking_address, value: amount }.abi_encode()` is built and dispatched via `pallet_revive::Pallet::<T>::bare_call` from the withdrawing user's account. This is the actual value-moving operation — equivalent to the vault's `profitToken.transferFrom(...)` in the external report.
2. If the call does not revert (`!return_value.did_revert()`), the code assumes the transfer succeeded from a state perspective, but then still requires `IERC20::transferCall::abi_decode_returns_validate(&return_value.data)` to parse a `bool` out of the return data.
3. Standard OpenZeppelin-style contracts, as bundled in this same repo, always return `true`/`false` from `transfer()` [3](#0-2) 
but many real-world tokens (USDT, BNB, and — per the external report — "hundreds of other tokens") return no data at all on success. In that case `return_value.data` is empty, `abi_decode_returns_validate` fails to decode a `bool`, and the `map_err` branch converts this into `XcmError::FailedToTransactAsset("ERC20 contract result couldn't decode")`.
4. Critically, this failure is returned *after* the real ERC20 balance transfer has already been executed on-chain (step 1 already moved tokens from `who` to `TransfersCheckingAccount`, and this is not rolled back merely because the outer XCM instruction returns an error — the `bare_call` is a separate, already-committed state transition triggered from within the `TransactAsset::withdraw_asset_with_surplus` call). No `AssetsInHolding` credit is created because the function returns `Err` before reaching `Ok((AssetsInHolding::new_from_fungible_credit(...), surplus))`.

The same pattern exists symmetrically in `deposit_asset_with_surplus` [4](#0-3)  and in the `fungibles::Mutate` impl used by XCM's `FungiblesAdapter` (`burn_from` / `mint_into`), which apply the identical strict-bool-decode-or-fail logic after already dispatching the real ERC20 transfer: [5](#0-4) 

This directly mirrors the reported bug class: code that assumes ERC20 `transferFrom`/`transfer` always yields the exact expected return-data shape, and mishandles the "hundreds of tokens" that don't conform, and — worse than the original bug (which merely reverted, leaving funds where they started) — here the funds have *already moved* by the time the mis-decoding is discovered, so the failure path leaves them orphaned in `TransfersCheckingAccount` rather than simply reverting the whole operation.

### Impact Explanation
This falls squarely within the "Balances, assets ... must conserve value and settle exactly once to the rightful beneficiary" and "permanent user-fund ... lock" categories of the impact gate. Any ERC20 registered for use with `ERC20Transactor` that is not a strict OpenZeppelin-style implementation (i.e., doesn't return `bool` from `transfer`) will, on every single withdrawal attempt by any unprivileged user, have their tokens moved into `TransfersCheckingAccount` while the XCM program is told the withdrawal failed. Since `TransfersCheckingAccount` has no exposed extrinsic to reconcile or refund stray inbound transfers that weren't matched by a corresponding `AssetsInHolding` credit, the tokens are permanently stuck — a chain-level fund-loss/lock bug reachable by any ordinary user attempting a normal cross-chain transfer of a non-standard ERC20, with no admin, relayer, or validator involvement.

### Likelihood Explanation
Likelihood is high for any ERC20 contract registered under this transactor that isn't a strictly conforming OpenZeppelin-style token — this is exactly the class of "hundreds of ERC20 tokens" called out in the source report (USDT, BNB, and similar non-standard tokens skip returning `bool`). No special preconditions, governance action, or malicious actor are required: a normal user simply initiating a withdraw/reserve-transfer of such a token from the parachain triggers the path. The trigger requires only that such a token is configured as a supported asset for the `ERC20Transactor`/`Matcher` (an operational/governance decision separate from any attacker action), at which point every ordinary user withdrawal against that asset is affected deterministically.

### Recommendation
Do not conflate "cannot ABI-decode return data" with "call failed." Adopt SafeERC20-equivalent semantics for `pallet_revive`-mediated ERC20 calls used by `ERC20Transactor` and the `fungibles::Mutate` impl in `impl_fungibles.rs`:
- Treat a non-reverted call with empty return data as success (mirroring `SafeERC20.safeTransfer`'s behavior of accepting "no return data" as success when the call itself didn't revert), instead of failing on decode.
- Only fail definitively when the call actually reverts, or when it returns non-empty data that decodes to an explicit `false`.
- Add explicit tests using a mock ERC20 contract that returns no data on `transfer`/`transferFrom` (mimicking USDT/BNB) to confirm withdrawals against such tokens don't strand funds in `TransfersCheckingAccount`.
- Consider adding a governance-gated sweep/reconciliation extrinsic for `TransfersCheckingAccount` as a defense-in-depth measure for any assets that do get orphaned there.

### Proof of Concept
1. Deploy (or register via `Matcher`) a minimal ERC20 contract whose `transfer(address,uint256)` performs the balance update and emits `Transfer`, but returns no data (i.e., a `function transfer(...) external { ... }` without `returns (bool)`), replicating USDT/BNB semantics.
2. As an ordinary user, initiate an XCM operation causing `ERC20Transactor::withdraw_asset_with_surplus` to run for this asset (e.g., a normal reserve-transfer or local-reserve withdrawal routed through this transactor on `AssetHubWestend`).
3. Observe: `pallet_revive::bare_call` executes `IERC20::transferCall`, the ERC20 contract's internal balance mapping decreases for `who` and increases for `TransfersCheckingAccount` — an actual on-chain, irreversible balance movement — and `did_revert()` is `false`.
4. `return_value.data` is empty; `IERC20::transferCall::abi_decode_returns_validate(&return_value.data)` returns `Err(...)`, and the function returns `Err(XcmError::FailedToTransactAsset("ERC20 contract result couldn't decode"))`.
5. The overall XCM instruction fails; no `AssetsInHolding` is credited to the user or any destination. Query the ERC20 contract's `balanceOf(who)` (decreased) and `balanceOf(TransfersCheckingAccount)` (increased) to confirm the funds are now held by the checking account with no corresponding successful XCM state — the funds are effectively locked and unrecoverable through any exposed extrinsic in this transactor path.

### Citations

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/src/xcm_config.rs (L1-1)
```rust
// Copyright (C) Parity Technologies (UK) Ltd.
```

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L166-194)
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

**File:** substrate/frame/revive/fixtures/contracts/external/openzeppelin/contracts/token/ERC20/ERC20.sol (L99-103)
```text
    function transfer(address to, uint256 value) public virtual returns (bool) {
        address owner = _msgSender();
        _transfer(owner, to, value);
        return true;
    }
```

**File:** substrate/frame/revive/src/impl_fungibles.rs (L186-203)
```rust
		log::trace!(target: "whatiwant", "{weight_consumed}");
		if let Ok(return_value) = result {
			if return_value.did_revert() {
				Err("Contract reverted".into())
			} else {
				let is_success =
					bool::abi_decode_validate(&return_value.data).expect("Failed to ABI decode");
				if is_success {
					let balance = <Self as fungibles::Inspect<_>>::balance(asset_id, who);
					Ok(balance)
				} else {
					Err("Contract transfer failed".into())
				}
			}
		} else {
			Err("Contract out of gas".into())
		}
	}
```
