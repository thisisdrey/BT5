## Analysis

The report's "deflation token" issue — code assuming `transfer(value)` moves exactly `value`, without checking actual balance delta — has a direct analog in the XCM `ERC20Transactor` used to bridge ERC20 tokens hosted on `pallet-revive` contracts through XCM.

### Title
XCM `ERC20Transactor` credits/debits the requested amount instead of the actual ERC20 balance delta, allowing deflationary/fee-on-transfer tokens to desynchronize backing from minted XCM credit - (File: cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs)

### Summary
`ERC20Transactor::withdraw_asset_with_surplus` and `deposit_asset_with_surplus` move ERC20 tokens by invoking the standard `IERC20::transfer(to, value)` call and interpreting only the boolean `true`/`false` return value as proof that exactly `value` tokens moved. Per the ERC-20 standard, a `true` return only guarantees the call succeeded — it says nothing about the exact amount actually credited to the recipient. Deflationary or fee-on-transfer ERC20 tokens burn or redirect part of `value` during `transfer`, so the `TransfersCheckingAccount` (or final beneficiary) can receive less than `amount` while the call still returns `true`.

### Finding Description
In `withdraw_asset_with_surplus`, tokens are withdrawn from a user by transferring `amount` to the `TransfersCheckingAccount`, and on success the code unconditionally constructs an `AssetsInHolding` credit for the full requested `amount`: [1](#0-0) 

That `Erc20Credit(amount)` value becomes the XCM executor's holding-register backing for this asset — but the checking account may hold strictly less than `amount` if the underlying ERC20 is a deflationary/fee-on-transfer token, since no `balanceOf` check is performed before/after the transfer.

Symmetrically, `deposit_asset_with_surplus` transfers `amount` from the checking account to the beneficiary and, again, treats the boolean `true` return as confirmation the beneficiary received the full `amount`: [2](#0-1) 

The equivalent low-level `pallet-revive` `fungibles::Mutate` implementation used by `xcm_builder::FungiblesAdapter` has the identical pattern: it decodes only the boolean success flag from `IERC20::transferCall` and then reports the *requested* accounting outcome by re-reading `balance()` after the call (which is a partial mitigation on the `impl_fungibles.rs` side because it does call `Inspect::balance` afterward), but the `ERC20Transactor` used for the actual cross-chain XCM asset transactor path does not re-check the checking account's/beneficiary's balance at all: [3](#0-2) 

The corrupted value is the `amount` field baked into `Erc20Credit(amount)` (and the `surplus`-only success path in deposit), which the XCM executor treats as backing/settled value for the asset without validating it against the actual token balance change on the checking/beneficiary account.

### Impact Explanation
This breaks the "Balances, assets ... and contract-held value must conserve value and settle exactly once to the rightful beneficiary and amount" invariant for any runtime that configures `ERC20Transactor` for an ERC20 asset with deflationary/fee-on-transfer semantics (a legitimate, commonly-deployed token design, not requiring any malicious actor). On withdraw, the runtime's internal holding register believes it locked `amount` in the checking account when it actually locked less — this can be leveraged repeatedly to inflate the XCM-tracked backing of an asset relative to what is truly custodied, and on deposit the destination account can be credited less than intended while upstream execution/success accounting assumes an exact settlement. Over time this creates unbacked/duplicate value in the message-routing and asset-accounting flow, which is within the "theft or unbacked mint or unlock" and "public underpriced work" impact classes for the Polkadot SDK program.

### Likelihood Explanation
No privileged, governance, relayer, validator, or malicious-peer assumption is required — a normal, unprivileged user simply needs to send an XCM message (e.g., a reserve/teleport-style transfer) involving an ERC20 asset that is deflationary/fee-on-transfer and configured to go through `ERC20Transactor`. Since ERC-20's `transfer` ABI only returns a boolean and the transactor never re-derives the actual delta via `balanceOf`, this triggers on every such transfer, making the likelihood high wherever a non-standard ERC20 is wired into this transactor.

### Recommendation
Before constructing `Erc20Credit`/reporting success, read `balanceOf` on the relevant account before and after the `transfer` call and use the **actual observed delta** as the credited/debited amount instead of the requested `amount`. Alternatively, explicitly document that `ERC20Transactor` only supports strictly-conforming ERC20 tokens (1:1 transfer, no fee-on-transfer/rebasing) and add a runtime-level check/allowlist that rejects assets failing an invariant check (e.g., verify `balanceOf` delta equals requested amount on first use, similar to safeguards already present in `substrate/frame/assets` which explicitly documents and handles amount adjustments).

### Proof of Concept
1. Deploy (or use) a fee-on-transfer ERC20 contract `T` on `pallet-revive`, where `transfer(to, value)` burns/redirects e.g. 5% of `value` and returns `true`.
2. Configure `T` as an XCM-transactable asset via `ERC20Transactor` with `TransfersCheckingAccount = C`.
3. A user with `1000` `T` tokens initiates an XCM reserve transfer of `1000` `T` off-chain.
4. `withdraw_asset_with_surplus` calls `transfer(C, 1000)`; `C`'s actual balance increases by only `950`, but the call returns `true`, so `AssetsInHolding` is credited with `Erc20Credit(1000)` — see [4](#0-3) .
5. The XCM executor now believes `1000` `T` is custodied by `C`, while only `950` is truly held — a `50` token backing deficit is created that can be repeated to accumulate unbacked XCM-tracked value versus the true ERC20 balance in `C`.

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

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L253-286)
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
```

**File:** substrate/frame/revive/src/impl_fungibles.rs (L162-203)
```rust
	fn burn_from(
		asset_id: Self::AssetId,
		who: &T::AccountId,
		amount: Self::Balance,
		_: Preservation,
		_: Precision,
		_: Fortitude,
	) -> Result<Self::Balance, DispatchError> {
		let checking_account_eth = T::AddressMapper::to_address(&Self::checking_account());
		let checking_address = Address::from(Into::<[u8; 20]>::into(checking_account_eth));
		let data =
			IERC20::transferCall { to: checking_address, value: EU256::from(amount) }.abi_encode();
		let ContractResult { result, weight_consumed, .. } = Self::bare_call(
			OriginFor::<T>::signed(who.clone()),
			asset_id,
			U256::zero(),
			TransactionLimits::WeightAndDeposit {
				weight_limit: WEIGHT_LIMIT,
				deposit_limit:
					<<T as pallet::Config>::Currency as fungible::Inspect<_>>::total_issuance(),
			},
			data,
			&ExecConfig::new_substrate_tx(),
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
				} else {
					Err("Contract transfer failed".into())
				}
			}
		} else {
			Err("Contract out of gas".into())
		}
	}
```
