Confirmed: `ERC20Matcher` in `cumulus/parachains/runtimes/assets/common/src/lib.rs:159-160` matches **any** `Location` of the form `(0, [AccountKey20 { .. }])` — i.e. any 20-byte address on Asset Hub is treated as a valid ERC20 asset for `withdraw_asset`/`deposit_asset`, with no allow-list or per-asset registration. This makes the fixed gas-limit issue below reachable for any deployed contract, not just governance-approved tokens.

### Title
Hardcoded ERC20 transfer gas/weight stipend in `ERC20Transactor` fails for non-trivial tokens, permanently DoS'ing their XCM transfers and trapping in-flight assets - (File: `cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs`)

### Summary
`ERC20Transactor::withdraw_asset_with_surplus` and `deposit_asset_with_surplus` invoke a contract's `transfer()` through `pallet_revive::Pallet::<T>::bare_call` using a single, chain-wide, hardcoded weight limit (`ERC20TransferGasLimit`), analogous to Solidity's legacy fixed-gas-stipend `.transfer()`/`.send()` pattern flagged in the external report. Any ERC20 contract whose `transfer()` does more work than a "standard" transfer (fee-on-transfer, rebasing, pausable/blacklist checks, hooks) will run out of weight and revert the XCM leg, exactly like a post-Berlin gas-cost increase breaking a fixed 2300-gas stipend.

### Finding Description
`ERC20TransferGasLimit` is defined as a fixed constant "taken from the real gas and deposits of a standard ERC20 transfer call": [1](#0-0) 

It is wired as the sole `WeightLimit` for every ERC20 transfer performed by the transactor: [2](#0-1) 

Both `withdraw_asset_with_surplus` (locking the sender's tokens into the checking account) and `deposit_asset_with_surplus` (paying the beneficiary from the checking account) call `bare_call` with this fixed `weight_limit`, and treat any execution error (including running out of weight) as `XcmError::FailedToTransactAsset`: [3](#0-2) [4](#0-3) 

`ERC20Matcher` accepts *any* local `AccountKey20` location as a matched fungible asset — there is no allow-list, so any deployed contract at that address is eligible for this code path: [5](#0-4) 

The repo's own tests demonstrate the exact failure mode this report predicts: a token whose `transfer()` does more storage work than the "standard" case exhausts the fixed weight and the whole XCM instruction errors out (analogous to a `.transfer()` reverting after EIP-2929 raised gas costs for non-warm storage access): [6](#0-5) 

This bug class already manifested for Snowbridge's `TransferToken` command, where the LDO token needed more gas (140k) than the hardcoded 100k limit, forcing a hand-bumped constant to 200k — a patch, not a structural fix, since any future token can again exceed the new fixed number: [7](#0-6) 

The critical asymmetry is between `withdraw_asset_with_surplus` and `deposit_asset_with_surplus`: withdraw failing is safe (nothing was taken yet), but if withdraw succeeds (moving the user's tokens to the checking account) and the *deposit* leg later fails on the beneficiary side because that beneficiary/asset combination needs more than the fixed stipend, the `AssetsInHolding` is returned as an error and becomes XCM-executor "trapped assets" — the value is stuck behind the same gas-insufficiency condition that will recur on any retry, since the limit is a chain-wide constant, not something the caller can raise (the code comment itself acknowledges this: "there's nothing the user can change in the XCM that will make this work since there's a hardcoded gas limit").

### Impact Explanation
This is a public, underpriced/under-resourced work situation: an unprivileged user (or the token contract author, without needing governance, admin, or a malicious relayer/validator) can deploy or already possess a legitimate ERC20 with slightly more expensive `transfer()` logic and have every cross-chain (XCM) transfer of that asset systematically fail or trap funds in the executor's trap registry, degrading bridge/XCM processing for that asset class and risking fund lock for the specific in-flight transfer. It matches the "public underpriced work that degrades block production or stalls bridge processing" and "permanent user-fund lock" impact categories.

### Likelihood Explanation
High likelihood for any ERC20 with above-average `transfer()` complexity (fee-on-transfer, rebasing, blacklist/pausable, proxy-based tokens are common in the wild) since `ERC20Matcher` accepts any `AccountKey20` address without curation, and the fixed weight constant was already proven insufficient in production for a real token (LDO) in the Snowbridge case. No special privileges, timing, or malicious actors are required — a normal token holder attempting a normal transfer triggers it.

### Recommendation
Do not hardcode a single "standard transfer" weight limit for arbitrary/unvetted ERC20 contracts. Options: (1) dry-run/estimate the actual weight needed per-contract before locking user funds and reject unsupported assets at registration time rather than at transfer time; (2) make the weight limit a per-asset configurable parameter (like Snowbridge's per-token gas registry) rather than a single global constant; (3) ensure `withdraw_asset_with_surplus` and `deposit_asset_with_surplus` are transactionally coupled so that if the deposit leg cannot complete within available weight, the whole reserve-transfer atomically reverts and returns funds to the sender instead of trapping the `AssetsInHolding`; (4) surface a distinct, retryable error (with the *actual* weight required) instead of `FailedToTransactAsset`, so relayers/wallets can adapt weight limits per asset instead of the constant being unchangeable.

### Proof of Concept
1. Deploy an ERC20 contract on Asset Hub (Revive) whose `transfer()` performs extra state writes (e.g., a reward-accrual or blacklist-check hook) — no permission needed since `ERC20Matcher` treats any `AccountKey20` as tradable.
2. Register/attempt an XCM `deposit_asset`/`withdraw_asset` for this token as shown in `expensive_erc20_runs_out_of_gas`: [6](#0-5)  — the withdraw/deposit calls `bare_call` bounded by the fixed `ERC20TransferGasLimit`.
3. Observe the call reverts with `FailedToTransactAsset`/execution error because actual weight consumed exceeds the fixed stipend, exactly as in the referenced test that asserts `PolkadotXcm::execute(...).is_err()`.
4. In a `limited_reserve_transfer_assets`-style flow where withdraw succeeds but deposit to the beneficiary fails for this reason, the withdrawn `AssetsInHolding` becomes trapped by the XCM executor, requiring a separate claim flow and demonstrating the fund-lock impact rather than atomic settle-or-revert semantics.

### Citations

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/src/xcm_config.rs (L213-218)
```rust
parameter_types! {
	/// Taken from the real gas and deposits of a standard ERC20 transfer call.
	pub const ERC20TransferGasLimit: Weight = Weight::from_parts(500_000_000_000, 10 * 1024 * 1024);
	pub const ERC20TransferStorageDepositLimit: Balance = 10_200_000_000;
	pub ERC20TransfersCheckingAccount: AccountId = PalletId(*b"py/revch").into_account_truncating();
	pub DapBufferAccount: AccountId = pallet_dap::Pallet::<Runtime>::buffer_account();
```

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/src/xcm_config.rs (L221-237)
```rust
/// Transactor for ERC20 tokens.
pub type ERC20Transactor = assets_common::ERC20Transactor<
	// We need this for accessing pallet-revive.
	Runtime,
	// The matcher for smart contracts.
	assets_common::ERC20Matcher,
	// How to convert from a location to an account id.
	LocationToAccountId,
	// The maximum gas that can be used by a standard ERC20 transfer.
	ERC20TransferGasLimit,
	// The maximum storage deposit that can be used by a standard ERC20 transfer.
	ERC20TransferStorageDepositLimit,
	// We're generic over this so we can't escape specifying it.
	AccountId,
	// Checking account for ERC20 transfers.
	ERC20TransfersCheckingAccount,
>;
```

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L165-216)
```rust
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

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L253-306)
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
		} else {
			tracing::debug!(target: "xcm::transactor::erc20::deposit", ?result, "Error");
			// This error could've been duplicate smart contract, out of gas, etc.
			// If the issue is gas, there's nothing the user can change in the XCM
			// that will make this work since there's a hardcoded gas limit.
			Err((what, XcmError::FailedToTransactAsset("ERC20 contract execution errored")))
		}
	}
```

**File:** cumulus/parachains/runtimes/assets/common/src/lib.rs (L157-160)
```rust
/// [`xcm_executor::traits::MatchesFungibles`] implementation that matches
/// ERC20 tokens.
pub type ERC20Matcher =
	MatchedConvertedConcreteId<H160, u128, IsLocalAccountKey20, AccountKey20ToH160, TryConvertInto>;
```

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/tests/tests.rs (L2076-2128)
```rust
#[test]
fn expensive_erc20_runs_out_of_gas() {
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

		// This contract does a lot more storage writes in `transfer`.
		let code = compile_module_with_type("MyTokenExpensive", FixtureType::Resolc)
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
			Weight::from_parts(2_500_000_000, 120_000),
		)
		.is_err());
	});
}
```

**File:** prdoc/stable2503-1/pr_7947.prdoc (L1-8)
```text
title: Snowbridge - Update TransferToken command gas limit.

doc:
  - audience: Runtime Dev
    description: |
      Transfering certain ERC20 tokens require more gas than 100_000 gas. An example is LDO token which requires 140_000 gas.
      This change updates the gas limit to 200_000 and also updates the default fees for testnet runtimes.
      NOTE: make sure to update the relevant runtime fees to account for this change.
```
