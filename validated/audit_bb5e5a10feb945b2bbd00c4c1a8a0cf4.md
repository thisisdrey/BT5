Based on my investigation, I confirm the code matches exactly what's cited. The `ERC20Matcher` at `cumulus/parachains/runtimes/assets/common/src/lib.rs` matches **any** `AccountKey20` location as a fungible asset with no allowlist/registry check [1](#0-0) , meaning any deployed ERC20 contract address is automatically XCM-transactable through `ERC20Transactor` without a governance-gated registration step — this supports the claim's premise that no privileged actor is needed to "register" a malicious contract.

The transactor itself trusts only the ABI-decoded boolean return value from `transfer()`, crediting/debiting the nominal `amount` regardless of what the contract actually moved, for both `withdraw_asset_with_surplus` [2](#0-1)  and `deposit_asset_with_surplus` [3](#0-2) . The `Erc20Credit` imbalance type explicitly documents that actual balance constraints are enforced by the contract, not the runtime [4](#0-3) . No `balanceOf` before/after check exists anywhere in this file — I verified the entire file's ~307 lines and there is no such measurement logic.

Audit Report

## Title
Fee-on-transfer / non-standard ERC20 tokens cause XCM holding/ledger divergence from actual `TransfersCheckingAccount` balance in `ERC20Transactor` - (File: cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs)

## Summary
`ERC20Transactor::withdraw_asset_with_surplus` and `deposit_asset_with_surplus` call `IERC20::transfer` for a requested `amount` and credit/debit XCM's `AssetsInHolding` with that full nominal `amount` based solely on the ABI-decoded boolean success return, never verifying the actual balance delta moved. Any ERC20 contract deployed under `pallet-revive` is automatically matched as a transactable XCM asset via `ERC20Matcher` (any `AccountKey20` location), with no allowlist or governance gate, so a fee-on-transfer/deflationary token can be used to desynchronize the XCM-internal accounting from the real balance of the shared `TransfersCheckingAccount`.

## Finding Description
`withdraw_asset_with_surplus` transfers `amount` from the user to `TransfersCheckingAccount` and, if `transfer` returns `true` (regardless of `did_revert()` being false), credits `AssetsInHolding::new_from_fungible_credit` with the full requested `amount` via `Erc20Credit` [5](#0-4) . `deposit_asset_with_surplus` transfers `amount` from `TransfersCheckingAccount` to the beneficiary and, on `Ok(true)`, reports success for the full `amount` [6](#0-5) . Neither path measures `balanceOf(TransfersCheckingAccount)` before and after the call. The `Erc20Credit` type's own doc comment concedes that "the actual balance constraints are enforced by the ERC20 smart contract itself rather than the runtime" [7](#0-6) . Because `ERC20Matcher` matches any local `AccountKey20` location as a fungible asset with no registry/allowlist check [1](#0-0) , any unprivileged user can deploy a fee-on-transfer contract and immediately use it through this transactor — no governance action is required to "enable" the divergence.

## Impact Explanation
This breaks the value-conservation invariant for the shared `TransfersCheckingAccount`: nominal `AssetsInHolding` credits/debits diverge from the contract's real balance movements. Because the checking account backs *all* users of a given ERC20 contract in XCM operations, cumulative use of a fee-on-transfer token can deplete the account's real balance faster than the nominal ledger implies, causing legitimate, unrelated depositors for that same asset to hit `"ERC20 contract transfer failed"` — a fund-lock/DoS on withdraw and deposit for holders of that specific registered asset. This is a genuine invariant violation of "settle exactly once with the correct amount," consistent with the balances/custody conservation invariant in the pivots.

## Likelihood Explanation
Low-to-Medium. It requires an ERC20 contract with non-standard fee-on-transfer/rebasing semantics to be interacted with through this transactor. Since `ERC20Matcher` imposes no allowlist and matches any `AccountKey20`-addressed contract, any user can deploy such a contract and drive round trips through `withdraw_asset`/`deposit_asset` themselves, with no privileged actor or special conditions needed, making the trigger path fully attacker-reachable via public XCM execution.

## Recommendation
Measure the actual `balanceOf(TransfersCheckingAccount)` delta before/after the `transfer` call (the pattern already used elsewhere for `Inspect::balance` per the report) and use the measured delta — not the requested `amount` — for `AssetsInHolding` accounting on both withdraw and deposit paths. Alternatively/complementarily, restrict which ERC20 contracts can be matched/registered by `ERC20Matcher`/`ERC20Transactor` to a vetted allowlist that excludes non-conforming fee-on-transfer or rebasing tokens.

## Proof of Concept
1. Deploy a `pallet-revive` ERC20 contract whose `transfer` returns `true` but deducts a fee (e.g., 5%) from the transferred amount.
2. Execute an XCM program via `pallet_xcm::execute` that `withdraw_asset`s N units of this token; `withdraw_asset_with_surplus` calls `transfer(checking_account, N)`, only `N*0.95` lands in `TransfersCheckingAccount`, but `AssetsInHolding` is credited the full `N` (per `erc20_transactor.rs` lines 195-203).
3. Follow with `deposit_asset` to a beneficiary for `N`; `deposit_asset_with_surplus` again nominally moves `N` out of the checking account while the beneficiary receives less, compounding the shortfall.
4. Repeat round trips; observe (via test harness similar to `smart_contract_does_not_return_bool_fails` in `asset-hub-westend/tests/tests.rs`) that a later, unrelated `deposit_asset` for the same asset eventually fails with `"ERC20 contract transfer failed"` due to the checking account's real balance being insufficient relative to the nominal ledger.

### Citations

**File:** cumulus/parachains/runtimes/assets/common/src/lib.rs (L134-160)
```rust
pub struct IsLocalAccountKey20;
impl Contains<Location> for IsLocalAccountKey20 {
	fn contains(location: &Location) -> bool {
		matches!(location.unpack(), (0, [AccountKey20 { .. }]))
	}
}

/// Fallible converter from a location to a `H160` that matches any location ending with
/// an `AccountKey20` junction.
pub struct AccountKey20ToH160;
impl MaybeEquivalence<Location, H160> for AccountKey20ToH160 {
	fn convert(location: &Location) -> Option<H160> {
		match location.unpack() {
			(0, [AccountKey20 { key, .. }]) => Some((*key).into()),
			_ => None,
		}
	}

	fn convert_back(key: &H160) -> Option<Location> {
		Some(Location::new(0, [AccountKey20 { key: (*key).into(), network: None }]))
	}
}

/// [`xcm_executor::traits::MatchesFungibles`] implementation that matches
/// ERC20 tokens.
pub type ERC20Matcher =
	MatchedConvertedConcreteId<H160, u128, IsLocalAccountKey20, AccountKey20ToH160, TryConvertInto>;
```

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L73-79)
```rust
/// A minimal imbalance tracking type that holds an ERC20 token amount.
///
/// This type implements the necessary imbalance accounting traits but does not perform
/// runtime-level balance enforcement. It's used to track ERC20 token amounts within XCM
/// asset holdings, where the actual balance constraints are enforced by the ERC20 smart
/// contract itself rather than the runtime.
struct Erc20Credit(u128);
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
