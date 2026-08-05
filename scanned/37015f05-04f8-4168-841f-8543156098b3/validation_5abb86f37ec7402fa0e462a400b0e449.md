## Title
Fee-on-transfer / reverting ERC20 tokens permanently strand user funds in the `TransfersCheckingAccount` of `ERC20Transactor` - (File: `cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs`)

### Summary
`ERC20Transactor` implements XCM's `TransactAsset` for ERC20-backed reserve assets by literally calling `IERC20::transfer` via `pallet_revive::bare_call` on `withdraw_asset_with_surplus` and `deposit_asset_with_surplus`. The withdraw leg blindly credits an internal, purely-accounting `Erc20Credit(amount)` imbalance into `AssetsInHolding` as soon as the withdraw-side `transfer` call reports success — without verifying that the `TransfersCheckingAccount` actually received `amount` tokens. If the underlying token is non-standard (fee-on-transfer, pausable, deny-listable, or otherwise able to make a *subsequent* `transfer` fail for reasons other than a plain boolean `false`), the deposit leg's `transfer` from the checking account to the beneficiary will fail, and the `Erc20Credit` gets trapped by the XCM executor with no way to ever recover the real tokens sitting in the checking account.

### Finding Description
`withdraw_asset_with_surplus` performs an ERC20 `transfer(checking_address, amount)` from the user to `TransfersCheckingAccount`, and on success mints a fictitious `Erc20Credit(amount)` into `AssetsInHolding`: [1](#0-0) 

This credit is a pure `u128` counter (`Erc20Credit`) with no binding to the actual on-chain balance held by `TransfersCheckingAccount` — it implements only in-memory imbalance bookkeeping traits (`UnsafeConstructorDestructor`, `UnsafeManualAccounting`, `ImbalanceAccounting`), and dropping/forgetting it does not touch chain state: [2](#0-1) 

Later, `deposit_asset_with_surplus` attempts to move `amount` from `TransfersCheckingAccount` to the beneficiary via another `IERC20::transfer` call. If that call reverts, returns `false`, or fails to decode, the function returns the (still-just-an-accounting-entry) `what: AssetsInHolding` back to the caller as an error: [3](#0-2) 

For a well-behaved plain ERC20, `amount` withdrawn == `amount` sitting in the checking account == `amount` depositable, so this design normally round-trips correctly. But nothing in `withdraw_asset_with_surplus` verifies that the checking account's actual balance increased by `amount` — it only trusts the boolean return of the withdraw-side `transfer`. Many real-world tokens do not preserve this invariant:
- Fee-on-transfer tokens: the checking account receives `amount - fee`, but `Erc20Credit(amount)` is credited into holding regardless.
- Tokens with transfer restrictions that can trigger *after* funds are already collected (e.g., pausable, deny-list, per-block transfer caps) can make the deposit-leg `transfer` fail even though the withdraw-leg `transfer` succeeded.

When the deposit-leg `transfer` subsequently fails for either reason, the XCM executor treats the returned `AssetsInHolding` (containing only the fictitious `Erc20Credit`) as a failed-instruction asset that gets trapped (`AssetsTrapped`) or handed back for a `ClaimAsset`. Reclaiming it will simply retry `deposit_asset_with_surplus` with the same (fee-deducted or restricted) checking-account balance, hitting the identical failure again — there is no code path that ever reconciles the trapped fictitious credit against the real leftover ERC20 balance in `TransfersCheckingAccount`. The real tokens are permanently stuck in that account with no recovery mechanism, matching this repo's own regression tests that only cover full-revert / non-bool-return cases, not the partial-transfer / fee-on-transfer case: [4](#0-3) 

### Impact Explanation
This is a permanent user-fund lock: once a user's tokens are pulled into `TransfersCheckingAccount` on the withdraw leg, if the deposit leg later fails due to a non-standard token's transfer semantics, the trapped `AssetsInHolding` entry is disconnected from the actual token custody. There is no mint/redeem accounting tying the `Erc20Credit` back to the checking account's real balance, so neither `ClaimAsset` retries nor any other pallet function can ever return the stuck tokens to the user. This falls under the accepted impact category of "permanent user-fund ... lock" and "Balances ... must conserve value and settle exactly once."

### Likelihood Explanation
No privileged actor, governance action, or malicious relayer/validator is required. Any unprivileged user constructing or receiving an XCM message that reserve-transfers a fee-on-transfer or transfer-restrictable ERC20 (once such an asset is registered as a reserve asset for `ERC20Transactor`, which is a normal, non-privileged operational scenario for any asset that meets the `Matcher`) can trigger the mismatch between what leaves the sender and what the checking account can subsequently forward. Fee-on-transfer and denylist-capable ERC20 tokens are common in the wild, so likelihood of encountering this class of token is realistic, matching the external report's premise that "widespread use of tokens that do not strictly follow the ERC20 standard" makes this a practical concern.

### Recommendation
- After the withdraw-leg `transfer`, read back the ERC20 `balanceOf(checking_address)` delta (or otherwise measure actually-received amount) and credit `Erc20Credit` with the actual received amount rather than the requested `amount`.
- On deposit-leg failure, do not treat the un-backed `Erc20Credit` as a generic trappable/reclaimable XCM asset; instead track a per-account real balance ledger in `TransfersCheckingAccount` and provide an explicit sweep/refund extrinsic that transfers whatever real balance remains back to the original depositor, keyed by the failed XCM instruction/context.
- Alternatively, adopt a safe-transfer wrapper pattern (as recommended in the source report) that performs a pre/post balance check around every ERC20 `transfer` call in both `withdraw_asset_with_surplus` and `deposit_asset_with_surplus`, and reject registering assets as ERC20-backed reserve assets whose `transfer` behavior isn't verified to be amount-preserving.

### Proof of Concept
1. Register a fee-on-transfer ERC20 token `T` (deducts e.g. 5% on every `transfer`) as a reserve asset routed through `ERC20Transactor` on the asset hub.
2. User Alice sends an XCM `WithdrawAsset(T, 1000)` — `withdraw_asset_with_surplus` calls `T.transfer(checking_account, 1000)`; `checking_account`'s real balance increases by only `950` (5% fee deducted), but the function still credits `AssetsInHolding` with `Erc20Credit(1000)` because the boolean return was `true`.
3. The XCM then executes `DepositAsset(T, 1000, beneficiary)` — `deposit_asset_with_surplus` calls `T.transfer(beneficiary, 1000)` from `checking_account`, but `checking_account` only holds `950`, so the ERC20 `transfer` reverts/returns `false`.
4. `deposit_asset_with_surplus` returns `Err((what, XcmError::FailedToTransactAsset(...)))`; the XCM executor traps the `Erc20Credit(1000)` accounting entry (`AssetsTrapped` event) or offers it via `ClaimAsset`.
5. Any subsequent `ClaimAsset` + `DepositAsset` retry repeats step 3 identically (checking account still only has 950 available), so the `950` real tokens remain permanently locked in `TransfersCheckingAccount` — irrecoverable by Alice or any other party.

### Citations

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L79-107)
```rust
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

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L270-305)
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
		} else {
			tracing::debug!(target: "xcm::transactor::erc20::deposit", ?result, "Error");
			// This error could've been duplicate smart contract, out of gas, etc.
			// If the issue is gas, there's nothing the user can change in the XCM
			// that will make this work since there's a hardcoded gas limit.
			Err((what, XcmError::FailedToTransactAsset("ERC20 contract execution errored")))
		}
```

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/tests/tests.rs (L2019-2074)
```rust
// Here the contract returns a number but because it can be cast to true
// it still succeeds.
#[test]
fn smart_contract_does_not_return_bool_fails() {
	let sender: AccountId = ALICE.into();
	let beneficiary: AccountId = BOB.into();
	let revive_account = pallet_revive::Pallet::<Runtime>::account_id();
	let checking_account =
		asset_hub_westend_runtime::xcm_config::ERC20TransfersCheckingAccount::get();
	let initial_wnd_amount = 10_000_000_000_000u128;

	ExtBuilder::<Runtime>::default().build().execute_with(|| {
		// Bring the revive account to life.
		assert_ok!(Balances::mint_into(&revive_account, initial_wnd_amount));

		// Fund all accounts involved.
		assert_ok!(Balances::mint_into(&sender, initial_wnd_amount));
		assert_ok!(Balances::mint_into(&beneficiary, initial_wnd_amount));
		assert_ok!(Balances::mint_into(&checking_account, initial_wnd_amount));

		// This contract implements the ERC20 interface for `transfer` except it returns a uint256.
		let code = compile_module_with_type("MyTokenFake", FixtureType::Resolc)
			.expect("compile ERC20")
			.0;

		let initial_amount_u256 = U256::from(1_000_000_000_000u128);
		let constructor_data = sol_data::Uint::<256>::abi_encode(&initial_amount_u256);

		let Contract { addr: non_erc20_address, .. } = bare_instantiate(&sender, code)
			.transaction_limits(TransactionLimits::WeightAndDeposit {
				weight_limit: Weight::from_parts(500_000_000_000, 10 * 1024 * 1024),
				deposit_limit: Balance::MAX,
			})
			.data(constructor_data)
			.build_and_unwrap_contract();

		let wnd_amount_for_fees = 1_000_000_000_000u128;
		let erc20_transfer_amount = 100u128;
		let message = Xcm::<RuntimeCall>::builder()
			.withdraw_asset((Parent, wnd_amount_for_fees))
			.pay_fees((Parent, wnd_amount_for_fees))
			.withdraw_asset((
				AccountKey20 { key: non_erc20_address.into(), network: None },
				erc20_transfer_amount,
			))
			.deposit_asset(AllCounted(1), beneficiary.clone())
			.build();
		// Execution fails but doesn't panic.
		assert!(PolkadotXcm::execute(
			RuntimeOrigin::signed(sender.clone()),
			Box::new(VersionedXcm::V5(message)),
			Weight::from_parts(2_500_000_000, 220_000),
		)
		.is_err());
	});
}
```
