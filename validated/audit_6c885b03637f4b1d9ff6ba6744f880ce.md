## Analysis

The DAO Pool bug reduces to: **trusting a return value from an externally-controlled, untrusted token contract as proof of an actual balance transfer, instead of verifying the balance delta before/after the call.**

The closest and most concrete analog in this repository is the `ERC20Transactor` XCM `TransactAsset` implementation, which treats arbitrary `pallet-revive` contracts as ERC20-compatible fungible assets for XCM purposes, and does exactly the same thing: it calls `transfer()` on the token contract and trusts the ABI-decoded boolean return value as proof that tokens actually moved, without ever checking the checking-account balance before and after the call. [1](#0-0) 

### Title
Unbacked XCM asset credit can be minted via ERC20 contracts that lie about `transfer` success - (File: `cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs`)

### Summary
`ERC20Transactor::withdraw_asset_with_surplus` and `::deposit_asset_with_surplus` treat any `pallet-revive` contract as a valid ERC20 fungible asset for XCM. Both functions perform a low-level `bare_call` to the token's `transfer()` function and accept the operation as successful purely based on `!did_revert()` plus a decoded `true` boolean return value, never checking that the `TransfersCheckingAccount`'s actual token balance changed by the claimed `amount`.

### Finding Description
In `withdraw_asset_with_surplus`, the transactor calls the asset contract's `IERC20::transfer(checking_address, amount)` from the withdrawing account, and if the call doesn't revert and the ABI-decoded return is `true`, it unconditionally mints an `Erc20Credit(amount)` into the XCM holding register: [2](#0-1) 

The same pattern occurs in `deposit_asset_with_surplus`, which calls `transfer()` from the checking account to the beneficiary and again trusts only the decoded boolean: [3](#0-2) 

`Erc20Credit` is a bespoke, unenforced imbalance type explicitly documented as *not* performing runtime-level balance enforcement — "the actual balance constraints are enforced by the ERC20 smart contract itself": [4](#0-3) 

Since an unprivileged user can deploy their own `pallet-revive` contract that implements an ERC20-shaped `transfer(address,uint256)` function which always returns `abi_encode(true)` without decrementing the caller's balance or incrementing the checking account's balance, the corrupted value produced is the `Erc20Credit(amount)` object placed into `AssetsInHolding` — an XCM-native token credit that has no corresponding real balance movement in the underlying contract or the `TransfersCheckingAccount`. This credit is indistinguishable, from the XCM executor's point of view, from a genuine token withdrawal, and can subsequently be deposited to any beneficiary, or forwarded through reserve-transfer/teleport XCM programs to other parachains that treat this asset location as backed 1:1 by the checking account's real ERC20 balance.

Existing tests only guard against a decode *failure* (e.g., a contract returning a `uint256` instead of `bool`), as seen in `smart_contract_does_not_return_bool_fails`: [5](#0-4) 

but there is no defense against a contract that returns a syntactically valid `true` while performing a no-op internally — exactly the "fake `transferFrom`" pattern from the external report.

### Impact Explanation
This breaks the value-conservation invariant for bridged/cross-chain fungible assets: an unprivileged user can fabricate XCM holding credits for an "ERC20" asset without any real balance backing in `TransfersCheckingAccount`. If such a fabricated credit is subsequently reserve-transferred or teleported to another chain (which trusts this location as 1:1 backed), it produces unbacked mint/settlement on the destination, and any later attempt by legitimate holders to redeem/withdraw the real underlying ERC20 balance from the checking account can fail or under-fund, causing fund loss / permanent lock — directly matching the "theft or unbacked mint" and "permanent user-fund or bridge-state lock" impact categories.

### Likelihood Explanation
High likelihood for an unprivileged attacker: deploying a custom `pallet-revive` contract and wiring an XCM message (`WithdrawAsset`/`InitiateReserveWithdraw`/`InitiateTeleport`) that references it via `AccountKey20` requires no special privilege, node compromise, or governance action — only contract-deployment rights, which any user has.

### Recommendation
Before minting `Erc20Credit`/treating the ERC20 `transfer` as successful, record the target (checking account or beneficiary) balance via `IERC20::balanceOfCall` before and after the `bare_call`, and require that the delta equals exactly `amount` (accounting for potential fee-on-transfer semantics if ever supported), matching the mitigation recommended in the original report — verify actual balance movement, not just the return value.

### Proof of Concept
1. Deploy a `pallet-revive` contract `FakeERC20` implementing the `IERC20` ABI where `transfer(address,uint256)` always returns `abi_encode(true)` and performs no storage mutation (no balance change for caller or `to`).
2. Submit an XCM program (e.g. via `pallet_xcm::execute`) containing `WithdrawAsset` for `Asset { id: AccountKey20{ key: FakeERC20_address }, fun: Fungible(amount) }` from the attacker's own account.
3. `ERC20Transactor::withdraw_asset_with_surplus` calls `FakeERC20.transfer(checking_address, amount)`; it returns `true` without moving any balance; the transactor mints `Erc20Credit(amount)` into the XCM holding register — confirmed by the same integration-test harness style as `smart_contract_does_not_return_bool_fails` in `cumulus/parachains/runtimes/assets/asset-hub-westend/tests/tests.rs:2019-2074`, but using a contract that fully mimics the ABI (returns `true`) instead of returning the wrong type.
4. Follow with `DepositAsset`/`InitiateReserveWithdraw` to move the now-fabricated `amount` to a beneficiary or to another chain, while `TransfersCheckingAccount`'s real balance in `FakeERC20` never increased — demonstrating unbacked value creation.

### Citations

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L73-89)
```rust
/// A minimal imbalance tracking type that holds an ERC20 token amount.
///
/// This type implements the necessary imbalance accounting traits but does not perform
/// runtime-level balance enforcement. It's used to track ERC20 token amounts within XCM
/// asset holdings, where the actual balance constraints are enforced by the ERC20 smart
/// contract itself rather than the runtime.
struct Erc20Credit(u128);
impl UnsafeConstructorDestructor<u128> for Erc20Credit {
	fn unsafe_clone(&self) -> Box<dyn ImbalanceAccounting<u128>> {
		Box::new(Erc20Credit(self.0))
	}
	fn forget_imbalance(&mut self) -> u128 {
		let amount = self.0;
		self.0 = 0;
		amount
	}
}
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
