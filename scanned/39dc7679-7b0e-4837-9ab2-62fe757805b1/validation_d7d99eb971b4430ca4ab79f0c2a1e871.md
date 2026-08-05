## Analysis

The external report's core invariant is: **a public entrypoint accepts a caller-declared "amount" but never verifies that the actual value movement it triggers matches that declared amount**, causing either a revert (if less moves) or stuck/lost funds (if more moves, or if the movement silently differs from what's assumed).

The direct analog in this repository is `ERC20Transactor` in `cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs`, which is the XCM `TransactAsset` implementation that lets a parachain treat arbitrary `pallet_revive` ERC20 contracts as reserve/local assets for XCM transfers.

### Title
Unverified ERC20 transfer amount trusted as XCM asset credit/debit — (File: cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs)

### Summary
`ERC20Transactor::withdraw_asset_with_surplus` and `deposit_asset_with_surplus` take the `amount` declared in the XCM `Asset`/`AssetsInHolding` (from `Matcher::matches_fungibles`) and issue a Solidity `IERC20::transfer` call for that exact `amount`. The code only checks the boolean return value of the `transfer` call (or a revert), then unconditionally credits/debits the XCM holding register by the same declared `amount` — it never verifies that the ERC20 contract's actual balance delta equals `amount`.

### Finding Description
In `withdraw_asset_with_surplus`: [1](#0-0) 
the amount is extracted from the XCM asset descriptor and an ERC20 `transfer` to the checking account is issued for that exact value: [2](#0-1) 
If `transfer` returns `true`, the code unconditionally mints `amount` worth of `AssetsInHolding` via `Erc20Credit(amount)`: [3](#0-2) 

Symmetrically, `deposit_asset_with_surplus` burns `amount` from holding and issues an ERC20 `transfer` of `amount` from the checking account to the beneficiary, again only checking the boolean return: [4](#0-3) 

The `Erc20Credit` imbalance type used to back this accounting is explicitly documented as *not* runtime-enforced — "the actual balance constraints are enforced by the ERC20 smart contract itself rather than the runtime": [5](#0-4) 

This is the same broken invariant as the RouterV2 report: a caller/config-declared amount (`amount` from the XCM `Asset`, analogous to `amountIn`) is never cross-checked against the actual value that moved (the ERC20 contract's real balance delta, analogous to `msg.value`). Any ERC20 whose `transfer` returns `true` while moving a different amount than requested — fee-on-transfer tokens, rebasing tokens, tokens with hooks/blacklists that partially process a transfer, or any non-standard implementation — breaks the 1:1 assumption relied upon here.

### Impact Explanation
- On `withdraw_asset_with_surplus`: if the ERC20 actually moves *less* than `amount` to the checking account but still returns `true`, the XCM executor is handed `AssetsInHolding` credited with `amount` that is not fully backed by real ERC20 balance in the checking account — an unbacked mint that can be teleported/reserve-transferred onward, i.e., theft of value not actually present.
- On `deposit_asset_with_surplus`: if the ERC20 moves *less* than `amount` to the beneficiary while returning `true`, the difference is permanently stuck in the checking account and unrecoverable through the transactor's own logic, mirroring the "excess gets stuck" branch of the original report — a permanent user-fund lock.
- Because any parachain that configures `ERC20Transactor` for a given asset location effectively delegates trust to whatever contract sits at that XCM-derived address, this can be triggered without any relayer, validator, or governance compromise — purely by having the asset registration point at (or be spoofable to point at) a non-standard ERC20.

### Likelihood Explanation
Likelihood is low-to-moderate: it requires that an ERC20 contract behind an XCM-registered asset location behaves non-standardly (fee-on-transfer/rebasing/partial-transfer-with-success semantics), which is a real, well-known class of tokens in the broader EVM ecosystem, and `ERC20Transactor` is explicitly designed to interoperate with "any" ERC20 registered this way rather than a whitelisted, audited standard implementation.

### Recommendation
After each `IERC20::transfer` call, read back the ERC20 balance of the sender/checking account (or beneficiary) before and after the call and use the observed delta — not the caller-declared `amount` — to determine how much to credit/debit `AssetsInHolding`. If the delta does not match `amount`, treat it as a partial failure (return the shortfall to holding / error out) rather than trusting the boolean return value alone.

### Proof of Concept
1. Register an ERC20 contract as a foreign/local asset location backed by `ERC20Transactor`, where the ERC20 implements a 1% transfer fee (a common, standards-compliant-looking pattern that still returns `true` on `transfer`).
2. Trigger an XCM program that calls `withdraw_asset_with_surplus` for `amount = 1000` of that asset from a user account.
3. The ERC20 `transfer` call succeeds (`true`) but only 990 units actually reach `TransfersCheckingAccount`.
4. `ERC20Transactor` still returns `AssetsInHolding` credited with the full `1000` (`Erc20Credit(amount)`), which the XCM executor then reserve-transfers/deposits onward as if fully backed — 10 units are unbacked mint, repeatable on every withdrawal to drain the checking account's real balance while apparent XCM-tracked issuance keeps growing.

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

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L185-203)
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
```

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L251-280)
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
```
