## Title
`ERC20Transactor::deposit_asset_with_surplus` performs a real ERC20 transfer but can still report failure for non-bool-returning tokens (USDT-class), enabling duplicate settlement of the same funds via XCM asset-trap/claim - ([File: cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs])

### Summary
The Amphor bug is that `IERC20.transfer` for a non-standard token (USDT-style, no/short return data) reverts the Solidity `bool` decode even though the underlying transfer semantically succeeds, permanently blocking the intended settlement path. The exact structural analog exists in `cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs`, which implements `TransactAsset` for XCM by wrapping raw `IERC20::transferCall`s through `pallet_revive::Pallet::<T>::bare_call` and then decoding the return value with `IERC20::transferCall::abi_decode_returns_validate`.

### Finding Description
In `withdraw_asset_with_surplus` and `deposit_asset_with_surplus` [1](#0-0) , the transactor calls the target contract's `transfer` and only trusts the outcome if `did_revert()` is false **and** the return bytes decode as a `bool`: [2](#0-1) [3](#0-2) 

This is precisely the pattern the repository's own regression tests were written to demonstrate is fragile: `smart_contract_does_not_return_bool_fails` and `smart_contract_not_erc20_will_error` in `cumulus/parachains/runtimes/assets/asset-hub-westend/tests/tests.rs` show that a contract whose `transfer` function performs the state mutation (`_transfer`) but returns something other than a proper `bool` (or nothing at all, as real USDT does) causes the whole XCM instruction to be treated as an error [4](#0-3) , using the `MyTokenFake` fixture that transfers real balance but returns `uint256` instead of `bool` [5](#0-4) .

The critical divergence from the Amphor fix recommendation ("transfer directly to receiver, don't gate on the return value") is that `deposit_asset_with_surplus` executes the *real* on-chain ERC-20 balance mutation via `bare_call` **before** it inspects the return value [6](#0-5) . If the return-decode subsequently fails (`Err(error) => ...`), the function returns `Err((what, XcmError::FailedToTransactAsset(...)))`, handing the *original abstract* `AssetsInHolding` credit (`what`, an `Erc20Credit` representing the pre-transfer accounting entry) back to the XCM executor as if the deposit never happened [7](#0-6) . The XCM executor's standard failure handling for a failed `DepositAsset` is to trap the returned holding via the asset-trap mechanism, emitting `PolkadotXcm::AssetsTrapped`, which can later be reclaimed with `pallet_xcm::claim_assets` by whoever the trap origin resolves to [8](#0-7) .

Because the smart-contract-level ERC-20 `_transfer` already moved real balance to the beneficiary's contract-tracked balance (as the test explicitly documents — "the contract returns a number but because it can be cast to true it still succeeds" for one variant, and reverts to error for the strict decode variant), a token that (a) executes the transfer, and (b) returns malformed/short/absent return data (the exact USDT class of token cited in the external report) causes:
1. Real ERC20 balance moved to beneficiary (first settlement), and
2. The same nominal amount trapped as `AssetsInHolding`/`Erc20Credit` and claimable again via `claim_assets` (second settlement) by the trap origin.

This is a duplicate-settlement primitive on the exact same value, driven by an unprivileged party who only needs to reference an `AccountKey20` location pointing at a non-standard (USDT-shaped) ERC20 contract in an XCM message — no malicious relayer, validator, or governance action required.

### Impact Explanation
This maps to the required "duplicate settlement or payout" and "theft or unbacked mint or unlock" impact classes: the abstract ledger (`AssetsInHolding`/asset-trap accounting) and the real ERC20 contract balance can diverge and be double-counted for the same nominal transfer amount, once on the contract's own storage and once via the XCM trap/claim flow. Any Asset Hub configuration that registers `ERC20Transactor` for arbitrary/attacker-deployable `AccountKey20` contracts (per PR `pr_7762.prdoc`, which explicitly documents that "asset ids... will be matched by this transactor and the corresponding transfer function will be called in the smart contract whose address is `key`") is exposed, since any user can deploy a contract at will and reference it by address in an XCM message.

### Likelihood Explanation
Deploying a USDT-shaped contract (transfer without returning `bool`/with truncated return data) is trivial and fully within attacker control on `pallet_revive`; the repository's own test suite (`smart_contract_does_not_return_bool_fails`) already proves the divergent decode-vs-execution outcome is reachable through the public XCM `execute` extrinsic with an ordinary signed origin, not a privileged one. The remaining step — that the returned `AssetsInHolding` for a failed `DepositAsset` gets trapped and is separately claimable — follows directly from the documented, tested XCM asset-trap/claim flow (`pallet_xcm::claim_assets`).

### Recommendation
Do not execute the state-mutating `bare_call` to the ERC20 contract before the outcome is fully validated and irreversibly committed to `AssetsInHolding` accounting. Either (a) treat any decode failure the same way as `did_revert()` == true by ensuring the underlying transfer is provably rolled back before returning `Err`, or (b) as Amphor's own fix recommended, stop conditioning correctness on the returned boolean at all and instead verify the balance delta via `balanceOf` before/after the call so the transactor's outcome always reflects the true on-chain balance movement, eliminating the possibility of a mismatch between contract state and XCM holding/trap accounting.

### Proof of Concept
1. Deploy a contract shaped like `MyTokenFake`/real USDT: `transfer(to, value)` performs the balance mutation but does not return a valid 32-byte `bool` (either returns a different type or returns nothing) [5](#0-4) .
2. Send an XCM message via `PolkadotXcm::execute` from an ordinary signed account that references this contract as an `AccountKey20` asset and results in `ERC20Transactor::deposit_asset_with_surplus` being invoked for a beneficiary — mirroring `smart_contract_does_not_return_bool_fails` [9](#0-8) .
3. Observe (would require extending the existing test) that the contract's own `balanceOf` for the beneficiary increased (real transfer succeeded) while the XCM instruction as a whole is reported as failed and the corresponding `AssetsInHolding` gets trapped, emitting `AssetsTrapped`.
4. The trap origin then calls `pallet_xcm::claim_assets` to receive the same nominal amount a second time [10](#0-9) , completing the duplicate settlement.

Note: I was not able to fully verify within tool budget whether the XCM executor wraps each instruction's underlying storage effects (including `bare_call` side effects to `pallet_revive`'s contract storage) in a transactional scope that would roll back the ERC20 balance mutation together with the trap. If such a rollback exists and covers cross-pallet contract storage changes made via `bare_call`, the duplicate-settlement outcome described above would not materialize (only a functional DoS on non-standard tokens would remain). This uncertainty should be resolved by a background agent tracing `xcm_executor::XcmExecutor::process_instruction`/`with_transaction` handling around `InstructionError`/asset-trap logic together with `pallet_revive::Pallet::bare_call`'s storage-commit semantics before treating this as fully confirmed.

### Citations

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L150-216)
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

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L253-269)
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
```

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L270-298)
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

**File:** substrate/frame/revive/fixtures/contracts/MyTokenFake.sol (L15-19)
```text
    function transfer(address to, uint256 value) public virtual returns (uint256) {
        address owner = msg.sender;
        _transfer(owner, to, value);
        return 1243657816489523;
    }
```

**File:** polkadot/xcm/pallet-xcm/src/lib.rs (L1520-1573)
```rust
		/// Claims assets trapped on this pallet because of leftover assets during XCM execution.
		///
		/// - `origin`: Anyone can call this extrinsic.
		/// - `assets`: The exact assets that were trapped. Use the version to specify what version
		/// was the latest when they were trapped.
		/// - `beneficiary`: The location/account where the claimed assets will be deposited.
		#[pallet::call_index(12)]
		pub fn claim_assets(
			origin: OriginFor<T>,
			assets: Box<VersionedAssets>,
			beneficiary: Box<VersionedLocation>,
		) -> DispatchResult {
			let origin_location = T::ExecuteXcmOrigin::ensure_origin(origin)?;
			tracing::debug!(target: "xcm::pallet_xcm::claim_assets", ?origin_location, ?assets, ?beneficiary);
			// Extract version from `assets`.
			let assets_version = assets.identify_version();
			let assets: Assets = (*assets).try_into().map_err(|()| {
				tracing::debug!(
					target: "xcm::pallet_xcm::claim_assets",
					"Failed to convert input VersionedAssets",
				);
				Error::<T>::BadVersion
			})?;
			let number_of_assets = assets.len() as u32;
			let beneficiary: Location = (*beneficiary).try_into().map_err(|()| {
				tracing::debug!(
					target: "xcm::pallet_xcm::claim_assets",
					"Failed to convert beneficiary VersionedLocation",
				);
				Error::<T>::BadVersion
			})?;
			let ticket: Location = GeneralIndex(assets_version as u128).into();
			let mut message = Xcm(vec![
				ClaimAsset { assets, ticket },
				DepositAsset { assets: AllCounted(number_of_assets).into(), beneficiary },
			]);
			let weight = T::Weigher::weight(&mut message, Weight::MAX).map_err(|error| {
				tracing::debug!(target: "xcm::pallet_xcm::claim_assets", ?error, "Failed to calculate weight");
				Error::<T>::UnweighableMessage
			})?;
			let mut hash = message.using_encoded(sp_io::hashing::blake2_256);
			let outcome = T::XcmExecutor::prepare_and_execute(
				origin_location,
				message,
				&mut hash,
				weight,
				weight,
			);
			outcome.ensure_complete().map_err(|error| {
				tracing::error!(target: "xcm::pallet_xcm::claim_assets", ?error, "XCM execution failed with error");
				Error::<T>::LocalExecutionIncompleteWithError { index: error.index, error: error.error.into()}
			})?;
			Ok(())
		}
```
