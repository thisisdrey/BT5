### Title
ERC20Transactor XCM asset transactor trusts requested amount instead of actual ERC20 balance delta, breaking reserve backing for fee‑on‑transfer / non‑standard ERC20 tokens - (File: cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs)

### Summary
`ERC20Transactor` (used by AssetHub Westend's XCM config to let `pallet-revive`-deployed ERC20 contracts act as XCM-transactable reserve assets) implements `withdraw_asset_with_surplus` and `deposit_asset_with_surplus` by calling `IERC20::transfer`/`transferFrom` on the underlying Solidity contract and trusting the boolean return value together with the caller-supplied `amount` to construct the `AssetsInHolding` credit/debit. It never checks the checking account's actual ERC20 balance delta. This is the exact bug class from the external report: a token that reports success (or a fee‑on‑transfer token that moves less than requested) causes the runtime's internal accounting (`Erc20Credit(amount)`) to diverge from the real balance held by `TransfersCheckingAccount`.

### Finding Description
`withdraw_asset_with_surplus` withdraws `amount` from a user by calling the ERC20 contract's `transfer(checking_address, amount)` from the user's account, then unconditionally mints an `Erc20Credit(amount)` into the XCM holding register as soon as the contract returns `true`: [1](#0-0) 

`deposit_asset_with_surplus` does the mirror operation: it takes the `amount` recorded in the holding register (not the real checking-account balance) and calls `IERC20::transfer(beneficiary, amount)` from `TransfersCheckingAccount`, again trusting only the boolean return value: [2](#0-1) 

The custom `Erc20Credit` imbalance type used to track this asset inside the XCM holding register is explicitly documented as *not* runtime-enforced — it exists purely to shuttle the number `amount` through the executor, with real enforcement deferred to "the ERC20 smart contract itself": [3](#0-2) 

This means the invariant "1 unit of `Erc20Credit` in the XCM holding register == 1 unit of real ERC20 balance actually held by `TransfersCheckingAccount`" is never verified. Any ERC20 contract that:
- returns `true` from `transfer`/`transferFrom` while moving a different amount than `value` (fee‑on‑transfer, deflationary/burn-on-transfer, rebasing), or
- takes its fee from the sender's balance rather than from the transferred amount,

will desynchronize the recorded holding-register amount from `TransfersCheckingAccount`'s real balance. Because `TransfersCheckingAccount` is a single shared escrow account for *all* users of a given ERC20 asset moved through this transactor, a shortfall caused by one withdrawal silently reduces the pooled backing available to every other user's future `deposit_asset` for the same asset — exactly the "contract's balance reduces by more than the transfer amount... users may not be able to withdraw the expected amounts later" scenario called out in the source report. This differs from the ordinary ERC20-precompile path (`substrate/frame/assets/precompiles/src/lib.rs`), which moves balances directly inside `pallet_assets` and has no such trust boundary; `erc20_transactor.rs` is the one XCM-facing path that bridges arbitrary Solidity contracts (deployable by any unprivileged user via `pallet-revive`) into the chain's asset-accounting model without balance verification.

### Impact Explanation
The `Matcher: MatchesFungibles<H160, u128>` generic parameter determines which contract addresses are eligible; wherever it is configured to accept locally-deployed contract addresses (as used in `asset-hub-westend/src/xcm_config.rs`), an unprivileged user can deploy an arbitrary Solidity ERC20 contract via `pallet-revive` and register/use it as an XCM-transactable asset. By making the contract fee‑on‑transfer (or otherwise non-standard while returning `true`), the attacker can cause the recorded `AssetsInHolding` amount to diverge from `TransfersCheckingAccount`'s real balance. This breaks the "conserve value / settle exactly once" invariant required for asset accounting: reserve backing held by the shared checking account is exhausted faster than the ledger of "credits owed" in flight, so a subsequent legitimate depositor's `deposit_asset` for the same asset can fail (funds locked/trapped) or, depending on execution ordering, allow an attacker to redeem more real tokens from the shared pool than they actually deposited (since Erc20Credit amount is not validated against actual balance movement at either leg).

### Likelihood Explanation
Likelihood is moderate-to-high wherever this transactor is wired to permit user-deployed contract addresses: `pallet-revive` contract deployment is unprivileged, Solidity fee-on-transfer tokens are common and trivial to write, and the transactor's `abi_decode_returns_validate` check only validates the *boolean* return value — it performs no balance introspection before or after either `transfer` call. No malicious peer, validator, collator, or governance action is required; the attacker only needs to deploy a contract and initiate an XCM transfer/reserve operation through it.

### Recommendation
Apply the same fix pattern the source report recommends for `BaseJackpot.sol`: after each `IERC20::transferCall` in both `withdraw_asset_with_surplus` and `deposit_asset_with_surplus`, read `TransfersCheckingAccount`'s (or the sender's) actual ERC20 balance before and after the call via `IERC20::balanceOfCall`, and construct/redeem `Erc20Credit` using the observed delta rather than the caller-supplied `amount`. Additionally, consider restricting the `Matcher` to an allow-list of vetted, non-fee-on-transfer, non-rebasing ERC20 contracts (governance-curated) if pre/post-balance verification cannot be added cheaply, and document/enforce that `TransfersCheckingAccount` balances must equal the sum of outstanding `Erc20Credit` at all times (e.g., via a periodic invariant check or `defensive_assert!`).

### Proof of Concept
1. Deploy (unprivileged, via `pallet-revive`) a minimal ERC20 contract whose `transfer`/`transferFrom` deduct a fee (e.g., burn 5%) from the transferred `value` before crediting the recipient, while still returning `true`.
2. Register/route this contract address through `ERC20Transactor`'s configured `Matcher` (as wired in `asset-hub-westend/src/xcm_config.rs`) so it is usable as a reserve asset in XCM.
3. User A executes an XCM `WithdrawAsset` for `amount = 1000` of this token. `withdraw_asset_with_surplus` calls `transfer(checking, 1000)`; the contract returns `true` but only credits `checking` with `950` (5% fee burned). The holding register nonetheless records `Erc20Credit(1000)`. [4](#0-3) 
4. Repeat with additional users depositing into the same shared `TransfersCheckingAccount`, each leaving a shortfall versus the ledgered `Erc20Credit` amounts.
5. Eventually, a `DepositAsset` for a legitimate holding of `Erc20Credit(N)` attempts `transfer(beneficiary, N)` from `TransfersCheckingAccount`, which now holds less than the cumulative outstanding credits, causing that user's deposit to revert/trap while earlier users had already been paid out in full — demonstrating that value is not conserved and does not settle exactly once across all holders of the asset. [2](#0-1)

### Citations

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L73-107)
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

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L248-298)
```rust
		// We need to map the 32 byte beneficiary account to a 20 byte account.
		let eth_address = T::AddressMapper::to_address(&who);
		let address = Address::from(Into::<[u8; 20]>::into(eth_address));
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
