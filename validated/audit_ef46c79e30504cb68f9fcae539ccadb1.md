Audit Report

## Title
XCM `ERC20Transactor` credits/debits the requested `amount` instead of the actual ERC20 balance delta, allowing deflationary/fee-on-transfer tokens to desynchronize XCM holding-register backing from real custodied balance - (File: cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs)

## Summary
`ERC20Transactor::withdraw_asset_with_surplus` and `deposit_asset_with_surplus` move ERC20 tokens by calling `IERC20::transfer(to, value)` and treat only the boolean success return as proof that exactly `value` tokens moved, without ever reading `balanceOf` before/after the call. [1](#0-0)  For any deflationary or fee-on-transfer ERC20 configured as an XCM-transactable asset, this causes the XCM executor's holding register (`Erc20Credit(amount)`) and the deposit-success accounting to diverge from the true balance change in the `TransfersCheckingAccount`/beneficiary.

## Finding Description
In `withdraw_asset_with_surplus`, tokens are moved from the user to `TransfersCheckingAccount` via a raw `transfer` call, and on a `true` return the code unconditionally constructs `AssetsInHolding` backed by `Erc20Credit(amount)` — the requested amount, not the observed delta: [2](#0-1)  No `balanceOf` check surrounds the `bare_call` to `transfer`. [3](#0-2) 

Symmetrically, `deposit_asset_with_surplus` transfers `amount` from the checking account to the beneficiary and treats `Ok(true)` as confirmation the beneficiary received the full `amount`, again with no balance verification: [4](#0-3) 

This is distinct from `substrate/frame/revive/src/impl_fungibles.rs`'s `burn_from`, which re-reads `balance()` after a successful transfer as a partial mitigation, but `ERC20Transactor` (the actual XCM asset-transactor path) has no equivalent re-check. [5](#0-4) 

The corrupted value is the `amount`/`Erc20Credit(amount)` used as the XCM holding-register backing for the asset, which the executor treats as settled/custodied value without validating it against the real balance change on the checking or beneficiary account.

## Impact Explanation
This breaks the invariant that assets and contract-held value must conserve value and settle exactly once to the rightful beneficiary and amount. If a runtime configures `ERC20Transactor` for an ERC20 with fee-on-transfer/deflationary semantics, `withdraw_asset_with_surplus` can register more XCM-tracked backing than is truly custodied in the checking account, and `deposit_asset_with_surplus` can under-deliver to a beneficiary while reporting full success. In a reserve-transfer flow this can desynchronize the XCM-tracked amount from the actual token custody, falling into the "theft or unbacked mint or unlock" impact class, contingent on the specific asset configuration.

## Likelihood Explanation
No privileged actor is required: any user performing a normal reserve-style XCM transfer of an ERC20 asset that is fee-on-transfer/deflationary and wired through `ERC20Transactor` triggers this on every transfer, since ERC-20's `transfer` ABI returns only a boolean and the transactor never derives the real delta via `balanceOf`. The precondition is that a chain operator specifically configures such a non-standard token through this transactor; I was unable to confirm within this repository whether any live parachain runtime (e.g., asset-hub-westend) actually wires `ERC20Transactor` to arbitrary/unvetted ERC20 contracts versus a curated, standards-conforming set — this affects real-world exploitability but not the correctness of the code-level gap itself.

## Recommendation
Before constructing `Erc20Credit` or reporting deposit success, read `balanceOf` on the relevant account before and after the `transfer` call and use the observed delta as the credited/debited amount, rather than the requested `amount`. Alternatively, restrict `ERC20Transactor` to a governance-vetted allowlist of strictly standard-conforming ERC20 tokens and document/enforce that assumption at asset-registration time.

## Proof of Concept
1. Deploy a fee-on-transfer ERC20 `T` on `pallet-revive`, where `transfer(to, value)` burns/redirects 5% of `value` and returns `true`.
2. Configure `T` as an XCM-transactable asset via `ERC20Transactor` with `TransfersCheckingAccount = C`.
3. A user with `1000 T` initiates a reserve-style XCM transfer of `1000 T`.
4. `withdraw_asset_with_surplus` calls `transfer(C, 1000)`; `C`'s actual balance increases by only `950`, but the call returns `true`, so `AssetsInHolding` is credited with `Erc20Credit(1000)`. [2](#0-1) 
5. The XCM executor now treats `1000 T` as custodied by `C`, while only `950 T` is actually held — a `50 T` backing deficit accrues per transfer, repeatable without limit.

### Citations

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L168-184)
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
```

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L191-207)
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
					tracing::debug!(target: "xcm::transactor::erc20::withdraw", "contract transfer failed");
					Err(XcmError::FailedToTransactAsset("ERC20 contract transfer failed"))
				}
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

**File:** substrate/frame/revive/src/impl_fungibles.rs (L185-195)
```rust
		);
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
```
