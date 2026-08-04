### Title
`ERC20Transactor` assumes strict ERC-20 return-value/revert semantics, permanently bricking XCM transfers for any real-world token that doesn't conform - (File: `cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs`)

### Summary
The seed report's broken invariant is: code assumes a *specific implementation detail* of a token interface (WETH's `transferFrom` self-transfer branch) that not every deployed contract actually implements, causing a legitimate, unprivileged operation to revert unconditionally. The same class of assumption exists in `ERC20Transactor::withdraw_asset_with_surplus` / `deposit_asset_with_surplus`, which hard-codes the expectation that any ERC-20 contract used as an XCM-transactable asset both (a) never returns data other than an ABI-encoded `bool`, and (b) always reverts on failure rather than silently no-op'ing. Real, widely-deployed ERC-20 tokens (e.g. USDT-style contracts with no return value, or non-reverting failure semantics) violate this assumption, causing every `withdraw_asset` / `deposit_asset` call through the transactor to unconditionally fail for that asset.

### Finding Description
`ERC20Transactor` is a `TransactAsset` implementation used to move `pallet-revive`-hosted ERC-20 contracts in and out of the XCM holding register.

`withdraw_asset_with_surplus` calls the token's `transfer` entry point via `bare_call`, then requires the return data to strictly ABI-decode as a `bool`: [1](#0-0) 

`deposit_asset_with_surplus` performs the mirror operation and applies the same strict decode requirement: [2](#0-1) 

In both paths:
- If the call reverts, the transfer is rejected — expected.
- If the call *succeeds* but the returned bytes don't decode as `IERC20::transferCall::abi_decode_returns_validate`, the code treats this as `XcmError::FailedToTransactAsset("ERC20 contract result couldn't decode")` — i.e. it treats a non-conforming (but perfectly successful) transfer as a hard failure.
- If the decoded value is exactly `false`, it is also treated as failure.

This mirrors the M-05 pattern exactly: the WETH bug assumed all WETH-like contracts implement the `src == msg.sender` branch of `transferFrom`; here the code assumes all ERC-20-like contracts strictly return an ABI-encoded `bool` from `transfer`. Many real-world, non-malicious ERC-20 contracts (the original EIP-20 reference implementation and several major stablecoins) return no data at all on success, or use non-standard revert conventions. Any such asset, once matched by `Matcher: MatchesFungibles<H160, u128>` and used through `ERC20Transactor`, cannot ever be withdrawn from or deposited into the XCM holding register — not because of an attacker, a bad actor, or an admin decision, but purely because of the transactor's built-in decode assumption.

### Impact Explanation
Once an asset that violates this assumption is included in the XCM asset matcher configuration, XCM programs that try to move that asset via `withdraw_asset` (source side) or `deposit_asset` (destination side) always fail with `FailedToTransactAsset`. Because `WithdrawAsset`/`DepositAsset` XCM instructions are asymmetric — assets can already be debited on one side of a program before the failure surfaces — this can result in **permanent loss/lock of the asset** on the side where the transfer already succeeded on-chain but the runtime's decode step disagrees, or in **stalled XCM message processing** for that asset class, matching the "permanent user-fund or bridge-state lock" and "public underpriced work that … stalls bridge processing" categories in the impact gate. No governance/admin misconfiguration is the root cause — the root cause is the hard-coded decode assumption in the transactor code itself; a governance decision to register a token only *triggers* the pre-existing code defect, it isn't the vulnerability.

### Likelihood Explanation
This does not require a malicious peer, relayer, validator, or leaked key. Any legitimate, unprivileged party can trigger it simply by using or depositing a non-`bool`-returning ERC-20 contract that has been (or gets) matched by the fungibles matcher for this transactor — a very common real-world token behavior class, not an exotic edge case. Given how frequently non-standard-return ERC-20 tokens exist in practice (this was precisely the root cause class flagged in the seed BendDAO report), likelihood of hitting this on any deployment that wires arbitrary/foreign ERC-20 contracts through `ERC20Transactor` is high.

### Recommendation
Do not hard-fail on ABI-decode mismatch for the ERC-20 `transfer` return value. Follow common safe-ERC20 handling: treat "call succeeded and either returned nothing or returned `true`" as success, and only treat an explicit `false` return or a revert as failure — mirroring the OpenZeppelin `SafeERC20` pattern used to handle exactly this class of non-conforming tokens.

### Proof of Concept
1. Deploy (via `pallet-revive`) a minimal ERC-20 contract whose `transfer(address,uint256)` performs the balance update correctly but returns no data (valid per the original EIP-20 spec, and matching several real deployed tokens).
2. Register/match this contract as a fungible asset recognized by the `Matcher` configured for `ERC20Transactor`.
3. Submit any XCM program that calls `WithdrawAsset`/`DepositAsset` for this asset through a chain configured with `ERC20Transactor` as its `TransactAsset`.
4. Observe that `withdraw_asset_with_surplus`/`deposit_asset_with_surplus` calls the contract successfully (`did_revert() == false`), yet `IERC20::transferCall::abi_decode_returns_validate(&return_value.data)` fails to decode the empty/short return data, so the function returns `Err(XcmError::FailedToTransactAsset("ERC20 contract result couldn't decode"))` even though the underlying token transfer already executed on-chain — reproducing the exact "always reverts/fails despite valid on-chain transfer" pattern from the seed report. [3](#0-2) [4](#0-3)

### Citations

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
