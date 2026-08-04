### Title
`ERC20Transactor` credits XCM holding with the declared amount on a bare boolean success flag instead of verifying the actual token balance delta — ([File: cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs])

### Summary
`ERC20Transactor::withdraw_asset_with_surplus` (and its counterpart `deposit_asset_with_surplus`) implements the `TransactAsset` trait used by the XCM executor to move ERC20 tokens (deployed on `pallet-revive`) in and out of XCM's `AssetsInHolding`. It calls the token contract's `transfer(to, value)` via `pallet_revive::Pallet::<T>::bare_call` and, if the ABI-decoded return value is `true`, unconditionally credits the XCM holding register with the caller-declared `amount` — the exact amount requested in the XCM `WithdrawAsset` instruction — without ever checking that the checking account's real balance changed by that amount. This is the same broken invariant as the MonoX `EvilERC20`/`addLiquidityPair` bug: minting/crediting value based on a caller/attacker-controlled declared amount and a bare "call succeeded" signal, rather than the observed balance delta.

### Finding Description
`withdraw_asset_with_surplus` at [1](#0-0)  extracts `(asset_id, amount)` straight from the XCM `Asset` being withdrawn (fully attacker-controlled — an unprivileged user submitting an XCM program via `pallet_xcm::execute`/`send` chooses both the ERC20 contract address and the amount), then issues a `transfer` call to that arbitrary contract.

The result handling only inspects the boolean return value of the call: [2](#0-1) 

If `is_success` is `true`, the function mints exactly `amount` worth of `AssetsInHolding` credit via `Erc20Credit(amount)`: [3](#0-2) 

The doc comment on `Erc20Credit` even states the design assumption explicitly: *"the actual balance constraints are enforced by the ERC20 smart contract itself rather than the runtime"* [4](#0-3) . Because the contract address is attacker-supplied (it's just whatever `H160`/`AccountKey20` the attacker encodes in the XCM `Asset` location), the attacker can deploy a custom contract whose `transfer` function always returns `true` while performing no real balance change (an exact structural analog of `EvilERC20.sol` in the external report, where `transferFrom` is overridden to under-transfer but still return success). The runtime has no way to detect this because it never re-reads the checking account's actual token balance before/after the call — it trusts the declared `amount` plus the boolean return.

`deposit_asset_with_surplus` has the identical pattern in reverse — it also only checks the boolean return of `transfer()` without verifying that the beneficiary's real balance increased by `amount`: [5](#0-4) 

Once the withdraw step has fabricated `amount` of `AssetsInHolding` credit for a token that was never actually moved, that credit is fungible within the XCM executor and can be:
- Deposited to the attacker's own account via `DepositAsset` (self-mint of on-chain-recognized value out of nothing), or
- Fed into `pallet-asset-conversion`'s `swap_exact_tokens_for_tokens`/`add_liquidity`, which itself calls `T::Assets::transfer` (same `ERC20Transactor` path) for the declared amount and, seeing the same "success" boolean from the same attacker-controlled contract, will pay out real reserve assets or mint real LP tokens against a token that was never actually deposited into the pool account. This directly parallels `Monoswap::addLiquidityPair` minting LP tokens against a `tokenAmount` that `safeTransferFrom` never actually delivered.

### Impact Explanation
An unprivileged attacker who can submit XCM programs (e.g. via `pallet_xcm::execute`, a public, unprivileged extrinsic) and deploy a `pallet-revive` contract can:
1. Deploy a "fake ERC20" contract whose `transfer` always returns `true` without moving any real value.
2. Use `WithdrawAsset` against that fake ERC20 to obtain `AssetsInHolding` credit for an arbitrary declared `amount`.
3. Route that fabricated credit through `DepositAsset`, or through `pallet-asset-conversion` swaps/`add_liquidity`, to extract real backed assets (native currency, `pallet-assets` tokens, or other ERC20s) from liquidity pools or from any account willing to accept the fake asset as payment.

This is theft/unbacked-mint of value and directly matches the "theft or unbacked mint" and "public underpriced work" pivots in the impact gate — no malicious validator, collator, relayer, or privileged actor is required; only a self-authored contract and a normal XCM execute call.

### Likelihood Explanation
Likelihood is high: deploying an EVM contract via `pallet-revive` and submitting `pallet_xcm::execute` are both ordinary, permissionless operations available to any account. The `ERC20Transactor` design explicitly documents that it does not enforce runtime-level balance constraints and instead defers entirely to the (attacker-authored) contract's return value, so no privilege escalation or race condition is needed — it is a direct, reliably reproducible logic gap.

### Recommendation
Do not trust the boolean return value of the ERC20 `transfer` call as proof that `amount` was moved. Read the checking/beneficiary account's actual token balance (via a `balanceOf` call) before and after the `bare_call`, and use the observed delta — capped at the declared `amount` — as the credited/debited amount, mirroring the MonoX fix of computing minted/credited amounts from the before/after balance difference rather than the caller-declared value.

### Proof of Concept
1. Deploy (via `pallet-revive`) a Solidity-like contract `FakeERC20` implementing `IERC20` where `transfer(address,uint256)` always `return true;` without adjusting any real backing balance (or backing an internal ledger the attacker fully controls, decoupled from any genuine reserve).
2. Submit `pallet_xcm::execute` with an XCM program:
   - `WithdrawAsset((AccountKey20 { key: FakeERC20_address }, huge_amount))` — triggers `ERC20Transactor::withdraw_asset_with_surplus`, which calls `FakeERC20.transfer(checking_account, huge_amount)`, gets `true` back, and credits `huge_amount` of `AssetsInHolding`.
   - `DepositAsset(...)` into a real liquidity pool operation (e.g., a `Transact` call into `pallet_asset_conversion::add_liquidity` or `swap_exact_tokens_for_tokens`) pairing the fake ERC20 credit with a real asset.
3. Observe that `pallet-asset-conversion` mints LP tokens or pays out real reserve assets based on `huge_amount`, even though the checking account never actually received `huge_amount` of genuine backing from `FakeERC20`.

### Citations

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L76-107)
```rust
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

impl UnsafeManualAccounting<u128> for Erc20Credit {
	fn saturating_subsume(&mut self, mut other: Box<dyn ImbalanceAccounting<u128>>) {
		let amount = other.forget_imbalance();
		self.0 = self.0.saturating_add(amount);
	}
}

impl ImbalanceAccounting<u128> for Erc20Credit {
	fn amount(&self) -> u128 {
		self.0
	}
	fn saturating_take(&mut self, amount: u128) -> Box<dyn ImbalanceAccounting<u128>> {
		let new = self.0.min(amount);
		self.0 = self.0 - new;
		Box::new(Erc20Credit(new))
	}
}
```

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L150-169)
```rust
	fn withdraw_asset_with_surplus(
		what: &Asset,
		who: &Location,
		_context: Option<&XcmContext>,
	) -> Result<(AssetsInHolding, Weight), XcmError> {
		tracing::trace!(
			target: "xcm::transactor::erc20::withdraw",
			?what, ?who,
		);
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
