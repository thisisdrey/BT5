Confirmed: `ERC20Matcher` matches **any** location of the form `{ parents: 0, interior: X1(AccountKey20 { key, .. }) }` to *any* H160 contract address, with no registration/allowlist step (`ERC20Matcher = MatchedConvertedConcreteId<H160, u128, IsLocalAccountKey20, AccountKey20ToH160, TryConvertInto>` at `cumulus/parachains/runtimes/assets/common/src/lib.rs:159-160`). This means any unprivileged user can reference an arbitrary self-deployed `pallet-revive` contract as an "asset" in an XCM message, and the `ERC20Transactor` will treat it as a fungible asset without any governance action.

### Title
ERC20Transactor credits/pays fixed XCM `amount` instead of measured ERC20 balance delta, enabling holding-vs-reality desync via attacker-controlled contract callbacks - ([File: cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs])

### Summary
`ERC20Transactor::withdraw_asset_with_surplus` and `deposit_asset_with_surplus` (`cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs:150-306`) implement XCM's `TransactAsset` for arbitrary ERC20 contracts matched by `ERC20Matcher`, which requires no permission or registration — any `AccountKey20` location is accepted (`cumulus/parachains/runtimes/assets/common/src/lib.rs:159-160`). Unlike the vulnerable Linea `bridgeToken` pattern that at least measured `balanceAfter - balanceBefore`, this code does something structurally weaker: it never measures actual balance movement at all. It only checks the boolean `IERC20::transfer` return value and then unconditionally credits/debits the XCM-declared `amount` into `AssetsInHolding` [1](#0-0) .

### Finding Description
Because the caller fully controls the contract code at the referenced `H160` address (it can be any account, including a fresh `pallet-revive` contract instantiated by the attacker), the "ERC20" being transacted is entirely attacker-controlled logic invoked mid-XCM-execution via `pallet_revive::Pallet::<T>::bare_call` [2](#0-1) . The transactor:
- On withdraw: calls `transfer(checking_account, amount)` on the attacker's contract, and if it returns `true`, credits `AssetsInHolding` with the full XCM `amount` regardless of what the contract's `transfer` implementation actually moved [3](#0-2) .
- On deposit: calls `transfer(beneficiary, amount)` from the checking account, and treats a `true` return as success without verifying the beneficiary's balance actually increased by `amount` [4](#0-3) .

Since the contract code is attacker-supplied, `transfer` can simply return `true` unconditionally while moving zero or a different amount (or performing any other logic, including reentrant calls back into the XCM executor / other precompiles / `pallet_revive` calls, since `bare_call` is a full nested execution with `ReentrancyProtection::AllowReentry` semantics by default for external calls). The XCM `AssetsInHolding` state — which subsequent `DepositAsset`, `BuyExecution`, or reserve-transfer instructions in the *same* XCM program rely on for correctness — is thus driven by an unauthenticated boolean return value rather than a verified balance change, exactly the invariant class ("credit computed from an untrusted signal rather than a verified balance delta") that broke the Linea bridge.

### Impact Explanation
An unprivileged attacker can:
1. Instantiate a contract via `pallet-revive` whose `transfer(address,uint256)` always returns `true` without moving any real value (or moves less than declared).
2. Reference this contract's address as an `AccountKey20` asset location in an XCM program (e.g., `WithdrawAsset` + `DepositAsset`, or a reserve/local-reserve transfer instruction) processed by the `AssetTransactors` tuple that includes `ERC20Transactor` on Asset Hub (`cumulus/parachains/runtimes/assets/asset-hub-westend/src/xcm_config.rs:239-246`).
3. Cause `AssetsInHolding` to be credited with a phantom `amount` that was never actually escrowed to the `ERC20TransfersCheckingAccount`, then have that phantom value paid out to any beneficiary chosen by the attacker via `DepositAsset` — a false-credit / unbacked-value path within a single XCM execution.

This directly matches the "theft or unbacked mint" and "public wrappers must not... undercharge nested execution" impact classes for a public, unprivileged entrypoint (XCM execution reachable through `pallet_xcm::execute`/`transfer_assets` style extrinsics on Asset Hub).

### Likelihood Explanation
High for any parachain/runtime that wires `ERC20Transactor` with `ERC20Matcher` into its `AssetTransactors` (confirmed present in `asset-hub-westend-runtime`, per `cumulus/parachains/runtimes/assets/asset-hub-westend/src/xcm_config.rs:221-246`), since:
- No permission/registration is required to make an arbitrary contract match as an "asset" (`IsLocalAccountKey20` / `AccountKey20ToH160` accept any 20-byte key).
- Deploying a malicious return-value contract via `pallet-revive` is a normal, permissionless, unprivileged action.
- Triggering `withdraw_asset`/`deposit_asset` for this asset type only requires constructing a normal XCM program executed via a signed origin.

### Recommendation
Do not trust the boolean `transfer` return value as authoritative for holding accounting. Measure the actual balance delta of the checking account (and/or beneficiary) via `balanceOf` calls immediately before and after the `transfer` call, and credit/debit `AssetsInHolding` using that measured delta, clamped to the requested `amount` — mirroring the "measure don't trust" fix pattern, while also adding explicit reentrancy protection around the nested `bare_call` (e.g. `ReentrancyProtection::Strict` semantics or a top-level lock on the XCM-driven checking account) so a malicious contract cannot re-enter the XCM executor mid-transfer to further corrupt holding state.

### Proof of Concept
Conceptual PoC (cannot be executed without a running node/emulated test in this environment):
1. On Asset Hub Westend, deploy via `pallet_revive::Pallet::instantiate` a minimal PVM/EVM contract `FakeERC20` whose `transfer(address,uint256)` selector handler always returns ABI-encoded `true` and performs no storage state changes (or an unrelated cheap SSTORE), i.e., it never actually debits/credits any balance.
2. Construct XCM: `WithdrawAsset(Asset{ id: AccountKey20(FakeERC20_address), amount: 1_000_000 })` followed by `DepositAsset(All, beneficiary: attacker_account)`, executed via `pallet_xcm::execute` from a normal signed account.
3. Trace execution: `ERC20Transactor::withdraw_asset_with_surplus` calls `FakeERC20.transfer(checking_account, 1_000_000)`, gets `true`, and credits `AssetsInHolding` with `1_000_000` even though the checking account's real balance in `FakeERC20`'s storage never changed [5](#0-4) .
4. `deposit_asset_with_surplus` then calls `FakeERC20.transfer(attacker_beneficiary, 1_000_000)` from the checking account and again trusts the `true` return, completing a full round-trip that produced XCM-level "1,000,000 units transferred" state with zero real value ever moving [6](#0-5) .

Note: I was not able to execute this PoC in a live test harness within this session; the analysis is based on static reading of `erc20_transactor.rs` and its wiring in `asset-hub-westend/src/xcm_config.rs`. If further confirmation of runtime-level exploitability (e.g., whether some outer XCM barrier or benefit-of-clearing logic mitigates this) is needed, a Devin session with test-execution access should reproduce this end-to-end in the `asset-hub-westend` emulated test suite.

### Citations

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L166-203)
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
```

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L253-280)
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
```
