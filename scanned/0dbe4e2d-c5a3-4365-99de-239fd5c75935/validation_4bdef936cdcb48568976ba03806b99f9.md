## Local Analog Found

### Title
Strict boolean ABI-decode of ERC20 `transfer` return data executes real token settlement before validating success — non-compliant tokens (USDT-style) cause debited value to bypass the XCM asset-trap safety net - (File: `cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs`)

### Summary
`ERC20Transactor` (the `TransactAsset` implementation used by the XCM executor to move Solidity-ERC20 assets deployed on `pallet_revive`) reproduces exactly the bug class from the report: it treats a missing/non-boolean return value from an ERC20 `transfer` as failure, even though the underlying token transfer already executed and mutated real contract storage.

### Finding Description
`withdraw_asset_with_surplus` and `deposit_asset_with_surplus` both encode an `IERC20::transferCall`, dispatch it via `pallet_revive::Pallet::<T>::bare_call`, and only *after* the call has actually executed do they attempt to `abi_decode_returns_validate` the return bytes as a `bool`: [1](#0-0) [2](#0-1) 

If the token contract does not return a `bool` at all (e.g. it returns nothing, like USDT, or returns a differently-typed value), `abi_decode_returns_validate` fails and the transactor returns `XcmError::FailedToTransactAsset`, **after the ERC20 `transfer` has already mutated the token's internal balance mapping** — settlement happened before the success check, not after. The identical pattern exists in `pallet_revive`'s `fungibles::Mutate` impl (`burn_from` / `mint_into`), used by `xcm_builder::FungiblesAdapter`: [3](#0-2) [4](#0-3) 

The repository already contains a fixture and regression test that reproduces this exact scenario — a contract (`MyTokenFake`) whose `transfer` performs the real balance update but returns a `uint256` instead of `bool`: [5](#0-4) [6](#0-5) 

This inverts the required ordering from the Polkadot SDK pivot: *"Message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically."* Here, execution/settlement (the ERC20 balance mutation) happens **before** decode validation, so a decode failure leaves the chain in a state where value has moved but the XCM holding register was never credited — the debited value never enters `AssetsInHolding`, so it is not eligible for the normal XCM asset-trap recovery mechanism that protects value lost mid-program.

### Impact Explanation
For the top-level `pallet_xcm::execute` extrinsic path exercised by the existing tests, FRAME's per-extrinsic atomic dispatch means a returned `Err` rolls back all storage mutations made during that call, including the ERC20 contract's internal balance mapping — so in that specific path the value is not actually lost, only the extrinsic fails (a pure availability/compatibility problem, matching the "Low/DoS" character of the original report rather than a fund-loss bug). I was not able to fully verify, within the available tool budget, whether the same atomic-rollback guarantee holds for XCM programs executed by the inbound message-queue path (HRMP/XCMP-driven reserve/teleport deposits using this transactor) rather than the user-initiated `execute` extrinsic — that boundary determines whether the settle-before-validate ordering can produce a genuine, non-reversible value loss for the affected token instead of just a reverted extrinsic.

### Likelihood Explanation
Any Solidity ERC20 contract that deviates from strict `IERC20` (returns no data, a non-`bool` type, or partial data on success — a well-known real-world pattern, notably USDT) will trigger this path whenever it is used as an XCM-transactable asset via `ERC20Transactor` or the `pallet_revive` `fungibles` adapter. No privileged actor, relayer, or validator collusion is required — an ordinary user or a token issuer simply registering/using such a token triggers it.

### Recommendation
- Validate the decoded/expected success signal **before** treating the transfer as settled, or better: perform a call-existence + return-data-length check (empty return data on success should be treated as success, consistent with common `SafeERC20`-style handling) before crediting/debiting business state.
- For `withdraw_asset_with_surplus`/`deposit_asset_with_surplus`, ensure that decode-failure paths cannot leave asset value moved without a corresponding credit into `AssetsInHolding`; either decode leniently (treat empty/non-bool-but-non-revert as success) or reorder logic so that failure to interpret the result also compensates/reverses the already-executed transfer explicitly rather than relying on outer-transaction atomicity assumptions that may not hold for all execution contexts (e.g. message-queue-driven inbound XCM).
- Add a checklist-style compatibility test matrix (as OpenZeppelin/Consensys token-integration checklists recommend) explicitly covering: no-return-value tokens, tokens returning non-bool truthy values, and tokens with fee-on-transfer/rebasing semantics, verifying balances/holding consistency (not just `.is_err()`) after each scenario, including via the inbound message-queue path, not solely via `pallet_xcm::execute`.

### Proof of Concept
The repository's own test `smart_contract_does_not_return_bool_fails` is a working PoC of the broken invariant: it deploys `MyTokenFake` (whose `transfer` mutates balances and returns `1243657816489523`, a non-bool value), sends an XCM `withdraw_asset`/`deposit_asset` program through `PolkadotXcm::execute`, and asserts only that execution errors — without asserting that the sender's real ERC20 balance was restored or that no value was trapped outside the holding register: [7](#0-6) 

To turn this into a full exploit-confirmation, extend the test to assert the ERC20 contract's balance mapping via `balanceOf` on the sender/checking account after the failed `execute` call, and separately construct the same XCM program via an inbound-message (non-`execute`) delivery path to determine whether the debited value is trapped, lost, or rolled back outside the atomic-extrinsic guarantee.

### Citations

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L166-207)
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
				} else {
					tracing::debug!(target: "xcm::transactor::erc20::withdraw", "contract transfer failed");
					Err(XcmError::FailedToTransactAsset("ERC20 contract transfer failed"))
				}
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

**File:** substrate/frame/revive/src/impl_fungibles.rs (L186-203)
```rust
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

**File:** substrate/frame/revive/src/impl_fungibles.rs (L225-241)
```rust
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

**File:** substrate/frame/revive/fixtures/contracts/MyTokenFake.sol (L15-19)
```text
    function transfer(address to, uint256 value) public virtual returns (uint256) {
        address owner = msg.sender;
        _transfer(owner, to, value);
        return 1243657816489523;
    }
```

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/tests/tests.rs (L2019-2073)
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
```
