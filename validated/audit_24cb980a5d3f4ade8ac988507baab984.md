## Finding

### Title
ERC20Transactor silently fails to decode void/non-bool return data from revive-hosted ERC-20 tokens, breaking XCM asset transfers for non-standard tokens - (File: `cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs`)

### Summary
`ERC20Transactor::withdraw_asset_with_surplus` and `ERC20Transactor::deposit_asset_with_surplus` execute a real on-chain `IERC20::transfer`/`transferFrom` call against a `pallet_revive`-hosted contract via `bare_call`, then unconditionally require the ABI-decoded return data to parse as `bool`, exactly mirroring the pattern flagged in the external report (`ERC20(asset).transferFrom(...)` wrapped in a strict `require`).

### Finding Description
Both transactor functions perform the underlying token movement first, then gate acceptance of that movement on decoding a `bool` from the call's return bytes: [1](#0-0) [2](#0-1) 

`abi_decode_returns_validate` requires the contract's return payload to strictly encode a single `bool`. Any ERC-20-style token contract that returns no data on success (the exact "void return" class from the external report, e.g. a USDT-style token deployed through `pallet_revive`), or that returns a differently-shaped value, causes this decode to error out, and the transactor treats the whole operation as `XcmError::FailedToTransactAsset`, even though the real token `transfer` call already executed and mutated the contract's internal balance storage.

This is not a hypothetical: the repository's own test suite documents this exact strict-interface failure mode for a contract that returns a non-`bool` value instead of the expected boolean: [3](#0-2) 

The test only asserts `PolkadotXcm::execute(...).is_err()`; it does not verify that the underlying `pallet_revive` contract storage (i.e., the actual ERC-20 balance change already performed via `bare_call`) was rolled back. `bare_call` operates on contract storage directly and its atomicity with respect to the enclosing XCM message/extrinsic depends entirely on the transactional wrapping of the calling context (works for `pallet_xcm::execute`-triggered dispatches via FRAME's automatic transactional rollback on `Err`, but is not verified for XCMP/UMP inbound message processing paths driven by `MessageQueue`, where errors are handled as "unprocessable" rather than as a dispatch-level `Err`).

### Impact Explanation
This directly maps to the bug class in the external report: an implicit strict-interface assumption about ERC-20 return values that is incompatible with legitimate, widely-used void-return tokens. In the polkadot-sdk context this affects the message/asset-accounting pivot — "message queues... and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically." Here, the on-chain token transfer (execution) can succeed while the XCM-level settlement (holding-register credit and downstream beneficiary deposit) is rejected due to a decode failure. For inbound XCM/XCMP message processing this risks tokens being debited from the sender or checking account without being properly credited into the XCM holding, a false-rejection/fund-accounting mismatch — as opposed to simple denial-of-service for that specific asset.

### Likelihood Explanation
No privileged actor is required — a normal user configuring or using any revive-hosted asset that follows the void-return convention (a real-world, common pattern, not a contrived edge case) triggers this path deterministically on every `withdraw_asset`/`deposit_asset` call through `ERC20Transactor`. The condition is entirely reachable through the public XCM execution and message-queue processing surfaces.

### Recommendation
Do not perform a strict ABI-decode-or-fail on the raw return bytes of `transfer`/`transferFrom`. Instead, follow the same remediation direction as the referenced report: treat an empty return payload as success (matching OpenZeppelin's `SafeERC20` semantics — success is `did_revert() == false` AND (`return_value.data.is_empty()` OR decodes to `true`)), and ensure that any state assumed already committed by the underlying `bare_call` before an error path is explicitly compensated for, verified transactionally, or the token movement itself is deferred until decode validation succeeds.

### Proof of Concept
The existing repository test `smart_contract_does_not_return_bool_fails` in `cumulus/parachains/runtimes/assets/asset-hub-westend/tests/tests.rs` already demonstrates the strict-decode failure using `MyTokenFake`, a contract implementing ERC20 `transfer` but returning a non-bool value; the same fixture pattern (or a genuine void/empty-return contract) reproduces the decode failure in `abi_decode_returns_validate` at `erc20_transactor.rs:191` and `erc20_transactor.rs:276`, while the underlying `pallet_revive::Pallet::bare_call` at lines 170-181/255-266 has already executed the token movement against contract storage.

### Citations

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L185-216)
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
			}
		} else {
			tracing::debug!(target: "xcm::transactor::erc20::withdraw", ?result, "Error");
			// This error could've been duplicate smart contract, out of gas, etc.
			// If the issue is gas, there's nothing the user can change in the XCM
			// that will make this work since there's a hardcoded gas limit.
			Err(XcmError::FailedToTransactAsset("ERC20 contract execution errored"))
		}
	}
```

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L270-306)
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
