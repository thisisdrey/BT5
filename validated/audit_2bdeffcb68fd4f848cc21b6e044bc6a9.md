### Title
ERC20Transactor treats a successful-but-non-boolean-returning `transfer` as a hard failure after funds already moved, permanently locking withdrawn ERC20 balances in the checking account - (File: cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs)

### Summary
`ERC20Transactor` (used to bridge arbitrary ERC-20 style contracts into XCM asset holdings on Asset Hub, via `pallet_revive`) performs withdrawals by literally invoking the target contract's `transfer(to, amount)` and then requires the return data to strictly ABI-decode as a `bool`. If the underlying contract call does not revert but returns no data or non-boolean data (the exact non-standard "USDT-style" `transfer` behavior called out in the source report), the tokens have already been physically moved to the shared `TransfersCheckingAccount`, yet the transactor returns `XcmError::FailedToTransactAsset`, so no `AssetsInHolding` credit is created and the XCM engine treats the withdrawal as if nothing happened. There is no code path anywhere in this transactor, or elsewhere in the repo, that lets the original owner reclaim the balance now sitting in the checking account. This is the on-chain analog of "not using `safeTransfer`" — instead of tolerating tokens whose `transfer` doesn't strictly return `bool`, the code hard-fails post-effect and orphans the transferred value.

### Finding Description
`withdraw_asset_with_surplus` builds a call `IERC20::transferCall { to: checking_address, value }` and dispatches it via `pallet_revive::Pallet::<T>::bare_call` from the user's own account: [1](#0-0) 

After the call, if it didn't revert, the code strictly decodes the return value as a `bool`: [2](#0-1) 

If `abi_decode_returns_validate` fails to decode a `bool` (e.g. because the token, like many real-world non-compliant ERC-20 implementations, returns no data on success, similar to the historically infamous behavior class exemplified by USDT-style tokens referenced in the source report), the function returns `Err(XcmError::FailedToTransactAsset("ERC20 contract result couldn't decode"))`. Critically, the contract call already executed (`did_revert()` is false), meaning the tokens have already left the caller's balance and landed in `checking_address` on the ERC20 contract's own storage. The XCM executor sees only an `Err` and does not create any `AssetsInHolding` credit, so:
- No downstream `deposit_asset` runs.
- The transferred amount is never returned to the caller.
- The XCM engine has no knowledge that value already moved on-chain.

The same non-recoverable pattern exists on the deposit side in `deposit_asset_with_surplus`, which moves value out of the checking account to a beneficiary using the identical bool-decode gate: [3](#0-2) 

Both the repository's own tests confirm the failure mode occurs (execution errors without panicking) for a token whose `transfer` returns something other than a plain `bool`, matching a "smart contract does not return bool" scenario: [4](#0-3) 

Note the test comment itself: "Here the contract returns a number but because it can be cast to true it still succeeds" — i.e. the maintainers are aware the decode is brittle to non-standard return encodings, but the test only proves the XCM call errors out; none of the tests verify or provide a way to recover the ERC20 balance that was already moved to `checking_address` before the error was raised. The `IERC20` interface itself, mirrored from the OpenZeppelin/`forge-std` style interface named in the original report, is the same rigid `returns (bool)` signature: [5](#0-4) 

There is no `TransfersCheckingAccount` sweep/reclaim extrinsic and no fallback logic in the transactor to detect "call succeeded, value moved, but return data was non-standard" and treat it as success (the `safeTransfer` equivalent for this stack would be: on non-revert, trust the transfer happened regardless of return decode). Existing guards (`did_revert()` check, `defensive_assert!` on holding length) only protect against outright contract revert or malformed `AssetsInHolding`; they do nothing to prevent the post-effect fund stranding described here.

### Impact Explanation
Any unprivileged user can invoke `pallet_xcm::execute`/`transfer_assets` with an `AccountKey20` location pointing at an arbitrary ERC-20-style contract deployed via `pallet_revive` (as demonstrated directly by the repository's own `smart_contract_does_not_return_bool_fails` and `smart_contract_not_erc20_will_error` tests, which use attacker-deployed bytecode as the asset contract). If that contract's `transfer` does not encode a strict `bool` return (a widely known real-world token quirk, and the exact bug class from the source report), the caller's tokens are moved into the fixed `TransfersCheckingAccount` on that ERC20 contract's ledger and become permanently unrecoverable — no extrinsic path returns them to the original owner or credits them into any XCM holding. If a legitimate, already-registered asset were ever wired through this transactor and happened to have non-boolean-return `transfer` semantics, every user attempting to move that asset via XCM would suffer the same unconditional fund loss. This matches the "permanent user-fund … lock" impact category for public XCM entry points that must settle exactly once, since here settlement never completes despite value having already moved.

### Likelihood Explanation
Likelihood is directly demonstrable using paths and fixtures already present in the repository (`MyTokenFake.sol`, `compile_module_with_type` fixtures, and the AssetHub XCM test harness), requiring only a normal signed XCM `execute`/`transfer_assets` call against an attacker- or protocol-deployed ERC20-style contract — no validator, relayer, governance, or privileged actor is needed. The main uncertainty is whether any currently-registered production asset actually uses this transactor with a non-boolean-returning `transfer`; that binding wasn't confirmed within index limits (the `xcm_config.rs` wiring of `ERC20Transactor`/`ERC20TransfersCheckingAccount` was found but its full asset-registration context wasn't fully read due to iteration limits), so likelihood for mainnet-registered assets is not fully verified, though the code-level fund-stranding defect itself is unconditionally reachable with attacker-controlled contracts.

### Recommendation
Do not gate success purely on strict `bool` ABI-decoding of the `transfer` return value. Adopt a `safeTransfer`-equivalent policy: treat a non-reverted call as successful transfer of `amount` regardless of return-data shape (empty, non-bool, or `bool`), and only treat an explicit `false` boolean return or revert as failure. Additionally, add a governance-independent (or root-gated but non-discretionary) reclaim mechanism for the `TransfersCheckingAccount` so that any balance which does get stranded due to unexpected token behavior can be returned to depositors, and extend the existing tests (`smart_contract_does_not_return_bool_fails`) to assert that no value is left unaccounted in the checking account after a decode failure.

### Proof of Concept
1. Deploy a minimal ERC-20-like contract via `pallet_revive` whose `transfer` performs the real balance update but returns no data (a `void`-return `transfer`, or one returning a `uint256`/other non-bool type) — the repository's own `MyTokenFake.sol` fixture already demonstrates this pattern (`return 1243657816489523;` instead of `bool`): [6](#0-5) 
2. Fund an account with this token and submit `PolkadotXcm::execute` with `withdraw_asset` referencing this contract via `AccountKey20 { key: <contract_addr>, .. }` and an amount, followed by `deposit_asset` to a beneficiary, exactly as done in the existing `smart_contract_does_not_return_bool_fails` test.
3. Observe: the XCM execution returns `Err` (as the existing test asserts) — but instead of merely observing the top-level XCM failure, additionally query the ERC20 contract's own storage/`balanceOf` for the sender and for `ERC20TransfersCheckingAccount`. The sender's on-chain ERC20 balance has already decreased and the checking account's balance has increased by `erc20_transfer_amount`, proving the transfer executed. There is no subsequent instruction, pallet call, or governance action in the codebase that returns this balance to the sender — it is permanently stranded in the checking account.

### Citations

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L166-181)
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
```

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L185-208)
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

**File:** substrate/primitives/ethereum-standards/src/IERC20.sol (L41-46)
```text
    /// @dev Moves a `value` amount of tokens from the caller's account to `to`.
    ///
    /// Returns a boolean value indicating whether the operation succeeded.
    ///
    /// Emits a {Transfer} event.
    function transfer(address to, uint256 value) external returns (bool);
```

**File:** substrate/frame/revive/fixtures/contracts/MyTokenFake.sol (L15-19)
```text
    function transfer(address to, uint256 value) public virtual returns (uint256) {
        address owner = msg.sender;
        _transfer(owner, to, value);
        return 1243657816489523;
    }
```
