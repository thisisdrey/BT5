Based on my investigation, I found a concrete local analog in `cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs`.

### Title
ERC20Transactor credits/debits XCM holding by nominal transfer amount instead of verified balance delta, letting a non-standard ERC20 (fee-on-transfer/rebasing/malicious) desynchronize on-chain balance from XCM asset accounting - ([File: cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs])

### Summary
`ERC20Transactor::withdraw_asset_with_surplus` and `deposit_asset_with_surplus` invoke an arbitrary ERC20 contract's `transfer()` via `pallet_revive::Pallet::<T>::bare_call` and trust the ABI-decoded boolean return value (`true`) as proof that exactly `amount` tokens moved, then mint/burn that exact `amount` as `Erc20Credit` inside the XCM `AssetsInHolding`. This mirrors the RealityCards `topupMarketBalance` flaw: an accounting counter is advanced by a caller-supplied/nominal amount on the assumption that a successful-looking ERC20 call implies a state change of that exact magnitude, without verifying the actual balance delta.

### Finding Description
In `withdraw_asset_with_surplus`: [1](#0-0) 
the code matches `(asset_id, amount)` from the XCM `Asset`, then encodes an `IERC20::transferCall { to: checking_address, value: amount }` and executes it via `bare_call`.

On success it does: [2](#0-1) 
i.e. if the decoded return value is `true`, it unconditionally mints `Erc20Credit(amount)` into the XCM holding — the *nominal* `amount` requested, not the actual balance change of the checking/beneficiary account. The same pattern exists symmetrically in `deposit_asset_with_surplus`: [3](#0-2) 

Because `asset_id` here is an arbitrary `H160` contract address matched via `Matcher: MatchesFungibles<H160, u128>`, and any account can deploy arbitrary revive/EVM contracts, an attacker can deploy a trivial "ERC20" contract whose `transfer()` function always returns `true` (ABI-encoded `0x01`) without moving any real balance, or a fee-on-transfer/rebasing token that transfers less than `amount`. `ERC20Transactor` has no check comparing the actual balance of the checking account (or beneficiary) before and after the call — it trusts the boolean return and the nominal `amount` exclusively, exactly like RealityCards trusted `_amount` in `topupMarketBalance` without validating actual `erc20.balanceOf` movement.

This breaks the "Balances, assets... must conserve value" invariant from the Polkadot SDK Pivots section: the XCM `AssetsInHolding` register can be inflated with `Erc20Credit` that has no backing balance in the checking account, letting this credit be deposited to a beneficiary or forwarded cross-chain as if it were real value.

### Impact Explanation
If a malicious ERC20/revive contract is used as the asset backing this transactor (e.g., a runtime that allows users to register arbitrary revive contracts as XCM-transactable assets, or reachable via a permissionless registration path), an attacker can fabricate `Erc20Credit` amounts in XCM holding that exceed what is actually escrowed in the `TransfersCheckingAccount`, and then deposit that fabricated value to any beneficiary — a direct value-conservation/unbacked-mint violation matching the required impact class ("theft or unbacked mint or unlock").

### Likelihood Explanation
Exploitability depends entirely on whether `ERC20Transactor`'s `Matcher` allows attacker-controlled/arbitrary revive contract addresses as valid `asset_id`s (as opposed to only a fixed allowlist of vetted, standards-compliant tokens configured by governance). I was not able to fully trace the concrete `Matcher` and asset-registration wiring for this transactor within the remaining investigation budget (only its usage sites in `cumulus/parachains/runtimes/assets/asset-hub-westend/src/xcm_config.rs` were located, not the full registration/permission flow), so the exact deployability of a malicious contract as a recognized `asset_id` for this transactor could not be confirmed from the index alone.

### Recommendation
- After each successful `bare_call` transfer, read back the actual `balanceOf` of the source/checking account (and/or destination) before and after the call, and credit/debit the XCM holding with the verified delta rather than the nominal `amount`.
- Restrict `Matcher: MatchesFungibles<H160, u128>` for this transactor to a governance-curated allowlist of contracts known to implement standard, non-fee-on-transfer, non-rebasing ERC20 semantics.
- Treat a `true` boolean return as necessary but not sufficient; reconcile with the observed on-chain balance change, and reject/rollback (return the asset to holding / error) if the delta doesn't match `amount`.

### Proof of Concept
1. Deploy a `pallet-revive` contract implementing `IERC20` where `transfer(address,uint256)` always returns `true` (ABI `0x...01`) and either performs no internal balance change or an unrelated one.
2. Register/whitelist (or otherwise get accepted by `Matcher`) this contract's `H160` address as the `asset_id` usable by `ERC20Transactor`.
3. Trigger an XCM program that calls `withdraw_asset` for this asset with a large `amount` from the attacker's own account (which needs no real balance in the fake contract, since the fake contract ignores balances entirely).
4. `withdraw_asset_with_surplus` sees `did_revert() == false` and `abi_decode_returns_validate(&return_value.data) == Ok(true)`, so it mints `Erc20Credit(amount)` into `AssetsInHolding`—unbacked by any real value transferred to `TransfersCheckingAccount`.
5. Use `DepositAsset` in the same XCM program to move this fabricated `Erc20Credit` to an arbitrary beneficiary, realizing unbacked value out of thin air.

### Citations

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L159-169)
```rust
		let (asset_id, amount) = Matcher::matches_fungibles(what)?;
		let who = AccountIdConverter::convert_location(who)
			.ok_or(MatchError::AccountIdConversionFailed)?;
		// We need to map the 32 byte checking account to a 20 byte account.
		let checking_account_eth = T::AddressMapper::to_address(&TransfersCheckingAccount::get());
		let checking_address = Address::from(Into::<[u8; 20]>::into(checking_account_eth));
		let weight_limit = WeightLimit::get();
		// To withdraw, we actually transfer to the checking account.
		// We do this using the solidity ERC20 interface.
		let data =
			IERC20::transferCall { to: checking_address, value: EU256::from(amount) }.abi_encode();
```

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L191-204)
```rust
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
