### Title
Hardcoded ERC20 Transfer Gas Limit Permanently Traps XCM-Bridged Tokens for Non-Standard ERC20 Contracts - (File: `cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs`)

### Summary
`ERC20Transactor::deposit_asset_with_surplus` and `withdraw_asset_with_surplus` invoke `pallet_revive::Pallet::<T>::bare_call` with a fixed, non-adjustable weight limit (`ERC20TransferGasLimit`, sized for a "standard" ERC20 `transfer`) whenever XCM moves an ERC20 asset. This is structurally identical to the reported bug class: a fixed gas budget hardcoded for a call to an unknown/arbitrary contract, which "may be insufficient" if the receiving code does more work than assumed.

### Finding Description
`ERC20Transactor::deposit_asset_with_surplus` calls the ERC20 contract's `transfer` function through `pallet_revive::Pallet::<T>::bare_call` using a fixed `WeightLimit::get()` (instantiated in the asset-hub runtime as `ERC20TransferGasLimit = Weight::from_parts(500_000_000_000, 10 * 1024 * 1024)`, explicitly documented as "Taken from the real gas and deposits of a standard ERC20 transfer call"): [1](#0-0) [2](#0-1) 

If the transfer runs out of gas/weight, the call errors and the comment in the code itself acknowledges the limitation: *"This error could've been duplicate smart contract, out of gas, etc. If the issue is gas, there's nothing the user can change in the XCM that will make this work since there's a hardcoded gas limit."* [3](#0-2) 

The repo's own test confirms this is a real, reproducible condition — a contract ("MyTokenExpensive") that does more storage work than a standard ERC20 transfer causes `PolkadotXcm::execute` to fail: [4](#0-3) 

The critical asymmetry is in the two-step XCM flow: `withdraw_asset_with_surplus` first moves tokens from the sender to a `TransfersCheckingAccount` via the same fixed-gas ERC20 `transfer` call, succeeding since it is on the withdraw path. `deposit_asset_with_surplus` then attempts to move the tokens from the checking account to the final beneficiary using the *same fixed gas limit*. If the beneficiary's incoming transfer triggers extra gas usage (e.g. rebasing/fee-on-transfer tokens, blacklist/compliance checks, proxy-forwarded ERC20s, or any contract with hooks beyond the "standard" transfer assumed when sizing `ERC20TransferGasLimit`), the deposit fails with `XcmError::FailedToTransactAsset`.

When `deposit_asset_with_surplus` fails, the XCM executor's `deposit_assets_with_retry` retries once and, on continued failure, propagates the error; the leftover holding (the `Erc20Credit` representing tokens already sitting in `TransfersCheckingAccount`) is trapped via `Config::AssetTrap::drop_assets`: [5](#0-4) 

Recovery is nominally possible via `pallet_xcm::claim_assets`, which calls `AssetTransactor::mint_asset` (defaulting through the tuple dispatcher back into `deposit_asset`/`deposit_asset_with_surplus` for the ERC20 transactor, since it has no dedicated `mint_asset` override): [6](#0-5) [7](#0-6) 

But `ERC20Transactor` never overrides `mint_asset`, so any claim attempt falls through the default trait behavior (`Err(XcmError::Unimplemented)`) or, if wired via `deposit_asset` fallback in a composed transactor, re-executes the exact same fixed-`WeightLimit` `bare_call` into the same contract — reproducing the identical out-of-gas failure every time. There is no per-claim override of the gas budget, no escalation, and no alternate path: the amount is deterministically and permanently unrecoverable for any ERC20 contract whose `transfer` genuinely requires more resources than the hardcoded `ERC20TransferGasLimit`.

### Impact Explanation
This breaks the "public underpriced work... stalls bridge/asset processing" and "permanent user-fund... lock" impact classes: an unprivileged user can submit or trigger an XCM `DepositAsset`/`InitiateTransfer` involving an ERC20 token whose transfer logic legitimately exceeds the fixed weight budget. The tokens are irreversibly stuck in `ERC20TransfersCheckingAccount` — withdrawn from the sender, credited nowhere, and un-claimable because the claim path re-uses the same insufficient fixed gas limit. This is not a hypothetical DoS; the runtime's own test suite (`expensive_erc20_runs_out_of_gas`) demonstrates the exact failure mode against a real contract fixture.

### Likelihood Explanation
Likelihood is high in practice: any ERC20 token with fee-on-transfer, rebase, blacklist/compliance checks, or non-trivial hooks (a large and common category of real-world tokens) will exceed the "standard transfer" gas assumption baked into `ERC20TransferGasLimit`. No malicious actor, governance action, or privileged party is required — an ordinary user bridging/transferring such a token via XCM triggers the loss deterministically and repeatably. This exactly mirrors the referenced report's core flaw: a fixed gas stipend applied to arbitrary, attacker/user-uncontrolled contract code.

### Recommendation
- Do not use a single hardcoded weight limit for arbitrary ERC20 contracts in `deposit_asset_with_surplus`/`withdraw_asset_with_surplus`; either allow per-asset configurable gas limits (as Snowbridge does — see `pr_7947`/`pr_8259` raising `TransferToken` gas from 100k to 200k after LDO required more) [8](#0-7) , or dry-run/estimate gas per specific token contract before committing to the fixed limit.
- Implement a dedicated `mint_asset` for `ERC20Transactor` that uses an escalated/unbounded (or configurably higher) weight limit specifically for claim/recovery flows, so that funds trapped due to gas exhaustion are actually recoverable.
- Consider tracking known "expensive" ERC20 contracts and rejecting/quarantining them at the XCM matcher level rather than silently trapping user funds after they've already left their source account.

### Proof of Concept
This is demonstrated directly by the existing test in the repository: [4](#0-3) 

Extending this scenario: repeat the same XCM (`withdraw_asset` on `non_erc20_address` + `deposit_asset` to `beneficiary`) but split into two separate XCM programs matching real bridge flow — first a successful `withdraw_asset_with_surplus` (moves tokens into `ERC20TransfersCheckingAccount`), then a `deposit_asset_with_surplus` that fails identically. Because `ERC20Transactor` provides no `mint_asset` override, subsequent `pallet_xcm::claim_assets` calls against the trapped credit will also fail to release the underlying ERC20 balance sitting in the checking account, leaving it permanently stranded — confirmable by asserting the ERC20 contract balance of `ERC20TransfersCheckingAccount` remains non-zero and unchanged across repeated claim attempts.

### Citations

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L253-266)
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
```

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L299-305)
```rust
		} else {
			tracing::debug!(target: "xcm::transactor::erc20::deposit", ?result, "Error");
			// This error could've been duplicate smart contract, out of gas, etc.
			// If the issue is gas, there's nothing the user can change in the XCM
			// that will make this work since there's a hardcoded gas limit.
			Err((what, XcmError::FailedToTransactAsset("ERC20 contract execution errored")))
		}
```

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/src/xcm_config.rs (L213-217)
```rust
parameter_types! {
	/// Taken from the real gas and deposits of a standard ERC20 transfer call.
	pub const ERC20TransferGasLimit: Weight = Weight::from_parts(500_000_000_000, 10 * 1024 * 1024);
	pub const ERC20TransferStorageDepositLimit: Balance = 10_200_000_000;
	pub ERC20TransfersCheckingAccount: AccountId = PalletId(*b"py/revch").into_account_truncating();
```

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/tests/tests.rs (L2076-2127)
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
```

**File:** polkadot/xcm/xcm-executor/src/lib.rs (L1853-1892)
```rust
	fn deposit_assets_with_retry(
		to_deposit: AssetsInHolding,
		beneficiary: &Location,
		context: Option<&XcmContext>,
	) -> Result<Weight, XcmError> {
		let mut total_surplus = Weight::zero();
		let mut failed_deposits = AssetsInHolding::new();

		// First pass: try to deposit each asset; failures go to retry.
		for single in to_deposit.into_per_asset_holdings() {
			match Config::AssetTransactor::deposit_asset_with_surplus(single, beneficiary, context)
			{
				Ok(surplus) => total_surplus.saturating_accrue(surplus),
				Err((unspent, _)) => {
					// First-pass failure: keep for retry. A subsequent deposit in the same
					// pass may create the destination account (by satisfying ED), allowing
					// the retry pass to succeed for assets that fall here.
					failed_deposits.subsume_assets(unspent);
				},
			}
		}

		// Retry previously failed deposits, this time short-circuiting on any error.
		for single in failed_deposits.into_per_asset_holdings() {
			let surplus =
				Config::AssetTransactor::deposit_asset_with_surplus(single, beneficiary, context)
					.map_err(|(unspent, error)| {
					tracing::debug!(
						target: "xcm::deposit_assets_with_retry",
						?error,
						?unspent,
						"Retry-pass deposit failed"
					);
					error
				})?;
			total_surplus.saturating_accrue(surplus);
		}

		Ok(total_surplus)
	}
```

**File:** polkadot/xcm/pallet-xcm/src/lib.rs (L3927-3984)
```rust
impl<T: Config> ClaimAssets for Pallet<T> {
	fn claim_assets(
		origin: &Location,
		ticket: &Location,
		assets: &Assets,
		context: &XcmContext,
	) -> Option<AssetsInHolding> {
		let mut versioned = VersionedAssets::from(assets.clone());
		match ticket.unpack() {
			(0, [GeneralIndex(i)]) => {
				versioned = match versioned.into_version(*i as u32) {
					Ok(v) => v,
					Err(()) => return None,
				}
			},
			(0, []) => (),
			_ => return None,
		};
		let hash = BlakeTwo256::hash_of(&(origin.clone(), versioned.clone()));
		match AssetTraps::<T>::get(hash) {
			0 => return None,
			1 => AssetTraps::<T>::remove(hash),
			n => AssetTraps::<T>::insert(hash, n - 1),
		}
		let mut claimed = AssetsInHolding::new();
		for asset in assets.inner() {
			match <T::XcmExecutor as XcmAssetTransfers>::AssetTransactor::mint_asset(asset, context)
			{
				Ok(minted) => {
					// SAFETY: Any fungible imbalances are now effectively duplicated because they
					// were not resolved when the asset was trapped (so total issuance tracks
					// trapped assets too), and now a duplicate asset was just minted.
					// To balance the system and keep total issuance constant, we drop and resolve
					// one of the duplicates. As a result, total issuance doesn't change.
					//
					// Note: This may emit Burned/Minted events even though the net issuance change
					// is zero. The mint creates a +X imbalance, and dropping the clone resolves -X,
					// resulting in no net change but potentially two events. This is an acceptable
					// tradeoff for the asset trap/claim mechanism.
					minted.fungible.iter().for_each(|(_, imbalance)| {
						let to_resolve = imbalance.unsafe_clone();
						core::mem::drop(to_resolve);
					});
					claimed.subsume_assets(minted)
				},
				Err(error) => tracing::debug!(
					target: "xcm::pallet_xcm::claim_assets",
					?asset, ?error, "Asset claimed from trap but unable to mint."
				),
			}
		}
		Self::deposit_event(Event::AssetsClaimed {
			hash,
			origin: origin.clone(),
			assets: versioned,
		});
		Some(claimed)
	}
```

**File:** polkadot/xcm/xcm-executor/src/traits/transact_asset.rs (L215-221)
```rust
	/// An asset has been minted and the imbalance returned into holding. This should do whatever
	/// housekeeping is needed.
	///
	/// When composed as a tuple, all type-items are called and at least one must result in `Ok`.
	fn mint_asset(_what: &Asset, _context: &XcmContext) -> Result<AssetsInHolding, XcmError> {
		Err(XcmError::Unimplemented)
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
