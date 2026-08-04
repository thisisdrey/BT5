### Title
ERC20 XCM Asset Transactor credits the nominal transfer amount instead of the actual ERC20 balance delta, permanently locking funds for fee-on-transfer/deflationary tokens - (File: `cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs`)

### Summary
`ERC20Transactor::withdraw_asset_with_surplus` and `ERC20Transactor::deposit_asset_with_surplus` move ERC20 tokens by calling the contract's `transfer()` function and, if the call does not revert and returns `true`, unconditionally credit/debit the **nominal** `amount` taken from the XCM `Asset` specification into `AssetsInHolding` — the code never reads the ERC20 contract's `balanceOf` before/after the call to confirm how many tokens actually moved. This is the same root defect as the external report: trusting a token's reported success/nominal amount instead of verifying the real balance delta, which breaks for fee-on-transfer, deflationary, or otherwise non-standard ERC20 tokens.

### Finding Description
`withdraw_asset_with_surplus` withdraws `amount` from a user by calling:
```rust
let data = IERC20::transferCall { to: checking_address, value: EU256::from(amount) }.abi_encode();
...
Ok(true) => Ok((AssetsInHolding::new_from_fungible_credit(what.id.clone(), Box::new(Erc20Credit(amount))), surplus))
``` [1](#0-0) 

and `deposit_asset_with_surplus` symmetrically transfers `amount` from the checking account to a beneficiary, again trusting the boolean return value: [2](#0-1) 

In both functions, `amount` is the value declared in the XCM `Asset` (matched via `Matcher::matches_fungibles`), not a measured balance change. `Erc20Credit` itself is a bare accounting wrapper with no link back to the real token balance: [3](#0-2) 

Any account can deploy an arbitrary Solidity contract through `pallet_revive` and reference it directly as an XCM asset via an `AccountKey20` junction — no registry or allow-list gates which contracts can be used as the ERC20 asset id, as demonstrated by the runtime test that deploys a fresh `MyToken` contract and immediately uses its address in a `withdraw_asset`/`deposit_asset` XCM program: [4](#0-3) 

So an attacker can deploy a token whose `transfer()` moves `amount - fee` (or otherwise less than `amount`) to the recipient while still returning `true`, exactly like the `MockTokenWithFee` example in the external report. When this token is withdrawn via XCM:
1. The user's real balance decreases by `amount` (or more), but the `TransfersCheckingAccount` only actually receives `amount - fee`.
2. `AssetsInHolding` nevertheless records a full `Erc20Credit(amount)`.
3. When that holding is later deposited (to a beneficiary, forwarded to another parachain leg, or reclaimed from a trap), `deposit_asset_with_surplus` attempts to move the full recorded `amount` out of the checking account. Since the checking account never actually held that many tokens, this transfer fails at that point.
4. Because the withdrawal step already executed a real, irreversible ERC20 transfer, the underlying value is now stuck in `TransfersCheckingAccount`; the XCM either traps the (fictitious) `amount`-sized credit or errors out, but there is no way to reconcile the trapped/erroring credit with the real, smaller balance sitting in the checking account. The result is either (a) assets permanently locked (unrecoverable, since any future deposit attempt of the trapped credit will again try to move `amount`, an amount the checking account never has), or, in multi-asset/multi-leg XCM programs, (b) a mismatch that lets a following leg believe it is backed by `amount` of value it does not actually possess.

None of the existing guards catch this: `matches_fungibles` only validates the asset's location format, `bare_call`'s `did_revert()`/return-value checks only see the boolean `true`, and there is no `balanceOf` before/after comparison anywhere in this transactor.

### Impact Explanation
This falls under "public underpriced work / theft or unbacked mint or unlock / permanent user-fund or bridge-state lock" for the Polkadot SDK program: an unprivileged user can deploy a fee-on-transfer or deflationary ERC20 contract and use it as an XCM-transactable asset, causing the runtime's internal ERC20 accounting (`AssetsInHolding`/`Erc20Credit`) to diverge from the real on-chain ERC20 balance held by `TransfersCheckingAccount`. This can permanently strand value in the checking account (funds no user can recover), or — in multi-hop reserve/teleport XCM programs where the erroneously-full credited amount is forwarded to another leg — create a state where downstream accounting assumes backing that does not exist.

### Likelihood Explanation
High feasibility: the attacker needs no privileged role, no relayer/validator/collator collusion, and no governance action. They simply deploy an ordinary Solidity contract through `pallet_revive` (permissionless, as shown by the runtime's own test harness) and reference it via `AccountKey20` in a self-authored XCM program executed with `pallet_xcm::execute`. No allow-list currently restricts which contracts can act as ERC20 assets for `ERC20Transactor`.

### Recommendation
In both `withdraw_asset_with_surplus` and `deposit_asset_with_surplus`, read the actual balance of the sender/recipient (via `balanceOf`) before and after the `transferCall`, and use the observed delta — not the nominal `amount` — when constructing/consuming `Erc20Credit`. If the delta does not match the requested amount, either fail the transaction (returning it as `XcmError::FailedToTransactAsset`) or credit only the real, measured amount so ledger and contract state can never diverge.

### Proof of Concept
1. Deploy a Solidity ERC20 contract (analogous to `MockTokenWithFee` in the report) whose `transfer(to, amount)` moves `amount - fee` to `to` but returns `true`, via `pallet_revive::Pallet::bare_instantiate`, exactly as done in `withdraw_and_deposit_erc20s` in `asset-hub-westend/tests/tests.rs`. [5](#0-4) 
2. Submit `pallet_xcm::execute` with a program that `WithdrawAsset`s this token (as `AccountKey20`) for `amount`, then `DepositAsset`s it to a beneficiary, mirroring the existing test's XCM shape. [6](#0-5) 
3. Observe that `withdraw_asset_with_surplus` records `Erc20Credit(amount)` even though `TransfersCheckingAccount`'s real ERC20 balance only increased by `amount - fee`. [1](#0-0) 
4. On the subsequent `deposit_asset_with_surplus` call for the full `amount`, the underlying `transferCall` from the checking account will either fail (returns `false`, since the account is short by `fee`) or, if the checking account has residual balance from other users' deposits, silently pay the shortfall out of other users' funds — producing wrong-beneficiary/wrong-amount settlement. [2](#0-1)

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

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/tests/tests.rs (L1864-1929)
```rust
#[test]
fn withdraw_and_deposit_erc20s() {
	let sender: AccountId = ALICE.into();
	let beneficiary: AccountId = BOB.into();
	let revive_account = pallet_revive::Pallet::<Runtime>::account_id();
	let checking_account =
		asset_hub_westend_runtime::xcm_config::ERC20TransfersCheckingAccount::get();
	let initial_wnd_amount = 100_000_000_000_000_000u128;
	sp_tracing::init_for_tests();

	ExtBuilder::<Runtime>::default().build().execute_with(|| {
		// Bring the revive account to life.
		assert_ok!(Balances::mint_into(&revive_account, initial_wnd_amount));
		// Fund all accounts involved.
		assert_ok!(Balances::mint_into(&sender, initial_wnd_amount));
		assert_ok!(Balances::mint_into(&beneficiary, initial_wnd_amount));
		assert_ok!(Balances::mint_into(&checking_account, initial_wnd_amount));

		let code = compile_module_with_type("MyToken", FixtureType::Resolc)
			.expect("compile ERC20")
			.0;

		let initial_amount_u256 = U256::from(1_000_000_000_000u128);
		let constructor_data = sol_data::Uint::<256>::abi_encode(&initial_amount_u256);
		let Contract { addr: erc20_address, .. } = bare_instantiate(&sender, code)
			.transaction_limits(TransactionLimits::WeightAndDeposit {
				weight_limit: Weight::from_parts(500_000_000_000, 10 * 1024 * 1024),
				deposit_limit: Balance::MAX,
			})
			.data(constructor_data)
			.build_and_unwrap_contract();

		let sender_balance_before = <Balances as fungible::Inspect<_>>::balance(&sender);

		let erc20_transfer_amount = 100u128;
		let wnd_amount_for_fees = 10_000_000_000_000u128;
		// Actual XCM to execute locally.
		let message = Xcm::<RuntimeCall>::builder()
			.withdraw_asset((Parent, wnd_amount_for_fees))
			.pay_fees((Parent, wnd_amount_for_fees))
			.withdraw_asset((
				AccountKey20 { key: erc20_address.into(), network: None },
				erc20_transfer_amount,
			))
			.deposit_asset(AllCounted(1), beneficiary.clone())
			.refund_surplus()
			.deposit_asset(AllCounted(1), sender.clone())
			.build();
		assert_ok!(PolkadotXcm::execute(
			RuntimeOrigin::signed(sender.clone()),
			Box::new(VersionedXcm::V5(message)),
			Weight::from_parts(600_000_000_000, 15 * 1024 * 1024),
		));

		// Revive is not taking any fees.
		let sender_balance_after = <Balances as fungible::Inspect<_>>::balance(&sender);
		// Balance after is larger than the difference between balance before and transferred
		// amount because of the refund.
		assert!(sender_balance_after > sender_balance_before - wnd_amount_for_fees);

		// Beneficiary receives the ERC20.
		let beneficiary_amount =
			<Revive as fungibles::Inspect<_>>::balance(erc20_address, &beneficiary);
		assert_eq!(beneficiary_amount, erc20_transfer_amount);
	});
}
```
