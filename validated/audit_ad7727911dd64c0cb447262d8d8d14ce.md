## Analysis

The external report's core broken invariant: an external-call boundary is trusted to represent a state change (token movement) without independent on-chain verification of the actual balance delta — the caller relies solely on the callee's own bookkeeping/return value.

The closest local analog is in `ERC20Transactor` in Snowbridge/Cumulus Asset Hub's XCM asset transactor, which is used to bridge/execute XCM `WithdrawAsset`/`DepositAsset` instructions against arbitrary ERC20 contracts hosted by `pallet-revive`.

### Title
XCM `ERC20Transactor` credits/debits holding based solely on a self-reported ERC20 contract call, without verifying actual balance movement - (`cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs`)

### Summary
`ERC20Transactor::withdraw_asset_with_surplus` and `::deposit_asset_with_surplus` execute a full, unconstrained contract call (`pallet_revive::Pallet::<T>::bare_call`) into an ERC20 contract's `transfer` function, then treat the ABI-decoded boolean return value as ground truth that `amount` of value moved [1](#0-0) . The code's own comment acknowledges this design: `Erc20Credit` "does not perform runtime-level balance enforcement... the actual balance constraints are enforced by the ERC20 smart contract itself rather than the runtime" [2](#0-1) .

### Finding Description
`withdraw_asset_with_surplus` calls into the contract at `asset_id` with a crafted `transfer(checking_address, amount)` payload and, if the call does not revert and decodes to `true`, unconditionally constructs `AssetsInHolding::new_from_fungible_credit(what.id.clone(), Box::new(Erc20Credit(amount)))` [3](#0-2) . There is no `balanceOf` check before/after the call, no comparison of actual token movement, and no re-entrancy guard around the `bare_call`, which executes arbitrary PVM/EVM contract code with the calling account's signed origin [4](#0-3) . The symmetric `deposit_asset_with_surplus` path has the same pattern for crediting a beneficiary via `transfer` [5](#0-4) .

Because `bare_call` runs the full call stack of `pallet-revive` (not a gas-stipend-limited "transfer"), the invoked contract's `transfer` implementation can perform arbitrary state-changing execution, including further nested calls, before returning control and its self-reported boolean. The XCM executor's holding-register accounting is thus keyed on a value (`is_success == true`) that is entirely controlled by the code at `asset_id`, not by an independently verified ledger delta. This mirrors the reported CEI-violation pattern in that trust is placed in the outcome of an external call rather than in a state value the pallet itself controls and verifies before/after the call.

### Impact Explanation
If the asset-to-contract matching (`Matcher::matches_fungibles`) can be satisfied for an asset id/location that resolves to a contract not genuinely holding/moving the claimed balance (e.g., a contract that always returns `true` without transferring, or reenters to manipulate holding accounting mid-call), the XCM executor will accept a `withdraw_asset` as fully successful and mint an equivalent, unbacked `AssetsInHolding` credit that can then be deposited, teleported, or paid out downstream — a direct "theft or unbacked mint" style outcome, and the deposit path symmetrically allows crediting a beneficiary without a corresponding verified debit.

### Likelihood Explanation
Exploitability depends entirely on runtime configuration of `Matcher`/`AccountIdConverter` and which ERC20 contract addresses are reachable for a given asset id. Where such mappings are governance-restricted to vetted, honest ERC20 contracts, the path is not exploitable by an unprivileged actor. The finding is therefore a structural weakness (missing independent balance verification and reentrancy exposure around the `bare_call` boundary) rather than a demonstrated, fully unprivileged exploit — this could not be fully confirmed against a concrete runtime instantiation with attacker-reachable asset-id-to-contract mapping within the available index.

### Recommendation
Verify the actual balance delta (e.g., `balanceOf` before and after the `bare_call`) rather than trusting the decoded boolean return value, and disallow/guard reentrancy into the XCM executor or into `ERC20Transactor` itself during the nested `bare_call`, matching CEI discipline: read pre-call balance, perform the call, then assert the post-call balance change equals `amount` before crediting/decrementing `AssetsInHolding`.

### Proof of Concept
Not independently reproducible from the indexed code alone: doing so requires a concrete runtime wiring where `Matcher` resolves an attacker-influenceable asset id to an attacker-deployed contract address reachable via `withdraw_asset_with_surplus`/`deposit_asset_with_surplus`. That wiring (which specific chain's `Matcher`/asset registration governs this) was not found in the indexed portions of the repository, so likelihood/reachability against a live configuration remains unverified.

### Citations

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

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L168-203)
```rust
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
