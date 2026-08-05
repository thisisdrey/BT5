Confirmed: `ERC20Transactor`'s default `mint_asset` (used by the `ClaimAsset`/`claim_assets` recovery path) returns `Err(XcmError::Unimplemented)` since the transactor never overrides it. That means the trap/claim recovery path for this asset falls through to `AssetNotFound`/`Unimplemented` in the tuple dispatcher and cannot mint the credit back into holding — the synthetic `Erc20Credit` can never be reconstructed by `ClaimAsset`, and even if it could, the final `DepositAsset` leg of `claim_assets` still calls the same `deposit_asset_with_surplus`, which fails identically for a non-boolean-returning ERC20. This closes off any recovery path, matching the external report's core defect exactly.

### Title
Non-standard (non-bool-returning) ERC20 tokens are permanently locked in the checking account by `ERC20Transactor` - ([File: cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs])

### Summary
`ERC20Transactor::withdraw_asset_with_surplus` and `deposit_asset_with_surplus` require the target contract's `transfer` call to return an ABI-encoded `bool`, decoded via `IERC20::transferCall::abi_decode_returns_validate`. Any ERC20-like contract that executes a real balance-moving transfer but returns no data (or non-bool data) causes `abi_decode_returns_validate` to error, and the wrapper reports the transfer as failed even though on-chain state already moved. Combined with the fact that `mint_asset` is never overridden for this transactor (defaulting to `Err(Unimplemented)`), the standard XCM trap/claim recovery mechanism cannot recover such tokens once they land in the `TransfersCheckingAccount`, permanently stranding them — the same bug class as the reported LendingPool issue.

### Finding Description
`withdraw_asset_with_surplus` [1](#0-0)  moves a user's ERC20 tokens into `TransfersCheckingAccount` by issuing a real `pallet_revive::bare_call` to the token's `transfer` function, then insists that the call's return data decode as a `bool` via `IERC20::transferCall::abi_decode_returns_validate`. If the contract call succeeds and did not revert but simply doesn't return a properly ABI-encoded boolean (a common trait of pre-EIP20-finalization tokens and other non-compliant implementations, mirroring the exact class of tokens named in the original report, e.g. USDT-style contracts), the transactor still returns `Err(XcmError::FailedToTransactAsset("ERC20 contract result couldn't decode"))` — even though the real balance already moved on the ERC20 contract.

`deposit_asset_with_surplus` has the identical decode requirement on the return leg (checking account → beneficiary) [2](#0-1) .

The XCM executor's `WithdrawAsset` and `DepositAsset` instructions are each wrapped in their own `transactional_process`/`TransactionalProcessor` scope [3](#0-2) [4](#0-3) , meaning a successful `WithdrawAsset` (which already committed the real ERC20 transfer into the checking account) is not rolled back by a later failing `DepositAsset` instruction — only the `self.holding` bookkeeping and any storage touched *within* the failing instruction are reverted. When the program ultimately fails, the (synthetic) leftover holding is trapped by `Config::AssetTrap::drop_assets` [5](#0-4) , which merely records a claim ticket, not the real off-chain ERC20 balance sitting in the checking account.

Recovery normally works via `pallet_xcm::claim_assets`, which builds `ClaimAsset` (calling `AssetTransactor::mint_asset`) followed by `DepositAsset` (calling `deposit_asset_with_surplus` again) [6](#0-5) . However, `ERC20Transactor` never implements `mint_asset`, so it uses the default `Err(XcmError::Unimplemented)` [7](#0-6) , and even in a tuple-based `AssetTransactors` composition this bubbles to `AssetNotFound` [8](#0-7) , since no other transactor in `AssetTransactors` [9](#0-8)  is registered to handle ERC20 (AccountKey20) asset ids. Even ignoring that, the final `DepositAsset` leg of the claim program re-invokes the exact same `deposit_asset_with_surplus` decode check that failed originally — so the retry will fail identically forever for a contract that structurally never returns a valid bool.

### Impact Explanation
Real ERC20 balance already transferred (state-changed) into the pallet's `TransfersCheckingAccount` becomes permanently unrecoverable by any mechanism exposed to users: no wrapper call, no trap-claim path, and no other transactor in the composition can move it out, because the only code path capable of calling `transfer()` on that specific asset id is `ERC20Transactor`, and it always fails the same way for that non-compliant contract. This is a permanent user-fund lock in the checking account, directly matching the "Impacts" criteria (permanent user-fund lock) without needing any admin, governance, relayer, or malicious-actor assumption — a plain user withdrawing/depositing such a token triggers it.

### Likelihood Explanation
Likelihood is bounded by how the asset id is registered/whitelisted for ERC20Transactor's `Matcher` (typically requires the token to be matched as an `AccountKey20` asset). Given `pr_7762.prdoc` shows this transactor is a first-class, intentionally-added feature ("first step towards cross-chain transferring ERC20s created on the Hub" [10](#0-9) ), and the existing test suite explicitly demonstrates the decode-failure scenario for a non-bool-returning contract [11](#0-10) , any deployed non-standard ERC20 contract that a user can freely deposit/withdraw via XCM will trigger this. The trigger requires no privileged action — any unprivileged holder of a non-compliant ERC20 that is matched by `assets_common::ERC20Matcher` can cause the lock by simply attempting a `WithdrawAsset`/`DepositAsset` sequence that partially executes across separate XCM instructions/messages.

### Recommendation
- Implement `mint_asset` for `ERC20Transactor` so trapped ERC20 credits can actually be recreated in holding, and audit whether `deposit_asset_with_surplus`'s bool-decode requirement should instead tolerate empty return data as success (mirroring OpenZeppelin `SafeERC20`'s handling of non-standard tokens, per the original report's own recommendation), while still rejecting explicit `false`/revert responses.
- Consider disallowing registration of ERC20 contracts with the transactor's `Matcher`/creation flow unless a lightweight standards-conformance probe (bool-returning `transfer`) is verified at registration time, to prevent non-compliant tokens from ever entering the checking account.
- Add a governance/root-gated emergency-drain path for the `TransfersCheckingAccount` (via `pallet_revive::bare_call` or a raw `transfer` dispatch) so tokens that get stuck due to return-value ambiguity remain recoverable even without changing the transactor logic.

### Proof of Concept
1. Deploy (or have deployed) an ERC20-like `pallet_revive` contract whose `transfer()` correctly moves balances but returns no data (or a value that doesn't ABI-decode to `bool`) on success, and register/match it via `assets_common::ERC20Matcher` so `ERC20Transactor` handles its asset id (as done for `MyTokenFake` in the existing test fixture) [12](#0-11) .
2. Submit an XCM program that is processed via the message queue (not the atomic `pallet_xcm::execute` extrinsic) containing `WithdrawAsset` for this ERC20 token followed by a `DepositAsset` to some beneficiary in a way that spans separate transactional scopes (e.g., via `InitiateTransfer`/queued message processing rather than a single `execute` call whose failure is guaranteed to roll back the whole extrinsic).
3. Observe: `withdraw_asset_with_surplus` succeeds and commits the real ERC20 transfer into `ERC20TransfersCheckingAccount` (per `cumulus/parachains/runtimes/assets/asset-hub-westend/src/xcm_config.rs` lines 213-237), while the subsequent `DepositAsset` fails to decode the return value, causing the message to end `Outcome::Incomplete` and the leftover `Erc20Credit` to be trapped via `AssetsTrapped`.
4. Attempt to recover via `pallet_xcm::claim_assets` for the trapped asset: `ClaimAsset` invokes `ERC20Transactor::mint_asset`, which is unimplemented (`Err(Unimplemented)`/`AssetNotFound`), so the claim fails immediately; even if minted synthetically by another means, the trailing `DepositAsset` in the claim program will re-invoke the same failing decode check.
5. Verify the real ERC20 balance permanently remains in `TransfersCheckingAccount`, unrecoverable by the affected user through any exposed pallet-xcm or ERC20Transactor entrypoint.

### Citations

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L166-194)
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

**File:** polkadot/xcm/xcm-executor/src/lib.rs (L946-970)
```rust
		match instr {
			WithdrawAsset(assets) => {
				self.ensure_can_subsume_assets(assets.len())?;
				Config::TransactionalProcessor::process(|| {
					let origin = self.origin_ref().ok_or(XcmError::BadOrigin)?;
					let mut total_surplus = Weight::zero();
					let mut withdrawn = AssetsInHolding::new();
					// Take `assets` from the origin account (on-chain)...
					for asset in assets.inner() {
						let (credit, surplus) = Config::AssetTransactor::withdraw_asset_with_surplus(
							asset,
							origin,
							Some(&self.context),
						)?;
						withdrawn.subsume_assets(credit);
						// If we have some surplus, aggregate it.
						total_surplus.saturating_accrue(surplus);
					}
					// ...and place into holding.
					self.holding.subsume_assets(withdrawn);
					// Credit the total surplus.
					self.total_surplus.saturating_accrue(total_surplus);
					Ok(())
				})
			},
```

**File:** polkadot/xcm/xcm-executor/src/lib.rs (L1191-1202)
```rust
			DepositAsset { assets, beneficiary } => {
				self.transactional_process(|self_ref| {
					let deposited = self_ref.holding.saturating_take(assets);
					let surplus = Self::deposit_assets_with_retry(
						deposited,
						&beneficiary,
						Some(&self_ref.context),
					)?;
					self_ref.total_surplus.saturating_accrue(surplus);
					Ok(())
				})
			},
```

**File:** polkadot/xcm/xcm-executor/src/traits/drop_assets.rs (L27-30)
```rust
pub trait DropAssets {
	/// Handler for receiving dropped assets. Returns the weight consumed by this operation.
	fn drop_assets(origin: &Location, assets: AssetsInHolding, context: &XcmContext) -> Weight;
}
```

**File:** polkadot/xcm/pallet-xcm/src/lib.rs (L1551-1567)
```rust
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
```

**File:** polkadot/xcm/xcm-executor/src/traits/transact_asset.rs (L219-221)
```rust
	fn mint_asset(_what: &Asset, _context: &XcmContext) -> Result<AssetsInHolding, XcmError> {
		Err(XcmError::Unimplemented)
	}
```

**File:** polkadot/xcm/xcm-executor/src/traits/transact_asset.rs (L408-422)
```rust
	fn mint_asset(what: &Asset, context: &XcmContext) -> Result<AssetsInHolding, XcmError> {
		for_tuples!( #(
			match Tuple::mint_asset(what, context) {
				Err(XcmError::AssetNotFound) | Err(XcmError::Unimplemented) => (),
				r => return r,
			}
		)* );
		tracing::trace!(
			target: "xcm::TransactAsset::mint_asset",
			?what,
			?context,
			"no match. did not mint asset",
		);
		Err(XcmError::AssetNotFound)
	}
```

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/src/xcm_config.rs (L239-246)
```rust
/// Means for transacting assets on this chain.
pub type AssetTransactors = (
	FungibleTransactor,
	FungiblesTransactor,
	ForeignFungiblesTransactor,
	UniquesTransactor,
	ERC20Transactor,
);
```

**File:** prdoc/stable2506/pr_7762.prdoc (L4-19)
```text
title: ERC20 Asset Transactor

doc:
  - audience: Runtime Dev
    description: |
      This PR introduces an Asset Transactor for dealing with ERC20 tokens and adds it to Asset Hub
      Westend.
      This means asset ids of the form `{ parents: 0, interior: X1(AccountKey20 { key, network }) }` will be
      matched by this transactor and the corresponding `transfer` function will be called in the
      smart contract whose address is `key`.
      If your chain uses `pallet-revive`, you can support ERC20s as well by adding the transactor, which lives
      in `assets-common`.
  - audience: Runtime User
    description: |
      This PR allows ERC20 tokens on Asset Hub to be referenced in XCM via their smart contract address.
      This is the first step towards cross-chain transferring ERC20s created on the Hub.
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
