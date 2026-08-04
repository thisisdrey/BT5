Confirmed: `ERC20Matcher` in `cumulus/parachains/runtimes/assets/common/src/lib.rs:159-160` matches **any** local `AccountKey20` location — i.e., any deployed EVM/revive contract address can be used directly as an XCM fungible asset, with no allowlist/registration requirement, and `ERC20Transactor` (`cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs`) blindly trusts the `amount` requested in the XCM `Asset` rather than measuring the actual balance delta on the underlying ERC20 contract.

### Title
Unbacked XCM asset credit via fee-on-transfer/rebasing ERC20 tokens in `ERC20Transactor::withdraw_asset_with_surplus` - (File: cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs)

### Summary
`ERC20Transactor` moves ERC20 tokens by calling `IERC20::transfer` and only checks the boolean return value / revert status to decide success [1](#0-0) . It never verifies that the checking account's actual token balance increased by the requested `amount`. Combined with `ERC20Matcher`, which accepts **any** `AccountKey20` location as a valid fungible asset with no registration/allowlist check [2](#0-1) , an attacker can supply a fee-on-transfer (or rebasing/deflationary) ERC20 contract and have the XCM executor credit the full nominal `amount` into `AssetsInHolding`, even though the checking account received fewer tokens.

### Finding Description
In `withdraw_asset_with_surplus`, the transactor calls `transfer(checking_address, amount)` on the ERC20 contract found at `asset_id` (an arbitrary `H160` derived from the XCM `Location` via `AccountKey20ToH160`) [3](#0-2) . If the call does not revert and the ABI-decoded return value is `true`, it unconditionally mints an `Erc20Credit(amount)` into the XCM holding register [4](#0-3) . This mirrors exactly the M-1 bug class: trusting a token's boolean success signal instead of verifying the effect of the transfer. For an ERC20 that charges a transfer fee or rebases balances downward, `transfer(to, amount)` can return `true` while the recipient's (checking account's) balance increases by less than `amount`.

Because `ERC20Matcher` is `MatchedConvertedConcreteId<H160, u128, IsLocalAccountKey20, AccountKey20ToH160, TryConvertInto>`, matching is purely structural (any `Location::new(0, [AccountKey20{..}])`) with no whitelist of vetted contracts [5](#0-4) . Any unprivileged user can permissionlessly deploy such a contract via `pallet_revive` and reference it in an XCM message — no governance/registration step gates which ERC20 contracts are usable through this transactor.

`deposit_asset_with_surplus` has the symmetric weakness: it transfers `amount` from the checking account to the beneficiary and again only checks the boolean/revert result, not the beneficiary's realized balance delta [6](#0-5) . This means the on-chain `Erc20Credit` amount can permanently drift from the checking account's real ERC20 balance: repeated withdraw/deposit cycles with a fee-on-transfer token cause the internally tracked holding-register amount to exceed what the checking account actually possesses, while every other legitimately deposited ERC20 asset shares the same single `TransfersCheckingAccount` per contract. A later user attempting to deposit/withdraw the same ERC20 type can then find the checking account under-funded relative to bookkeeping, or — depending on ordering — a user can walk away with more tokens transferred out than they ever put in, at the expense of the checking account's real backing for that asset.

### Impact Explanation
This breaks the "conserve value / settle exactly once" invariant for value that XCM treats as backed 1:1: the `AssetsInHolding` register can be credited with `amount` of an asset while the actual token balance backing it is smaller. Over repeated executions this creates unbacked internal balance for a specific ERC20 asset class routed through the checking account, which can be redeemed as real value (transferred to a beneficiary) that was never actually deposited by that redeemer — a theft/unbacked-mint condition against the shared checking account and, transitively, against any other legitimate holder of the same asset type.

### Likelihood Explanation
High feasibility for an unprivileged attacker: deploying a custom Solidity ERC20 with a transfer fee via `pallet_revive` is not privileged, and `ERC20Matcher`/`AccountKey20ToH160` accept any `AccountKey20` address with no allowlisting [7](#0-6) . The existing test suite explicitly exercises non-standard-return-value contracts (`smart_contract_does_not_return_bool_fails`) but there is no equivalent test that exercises fee-on-transfer/rebasing behavior with a boolean `true` return [8](#0-7) , confirming this class of discrepancy is not currently checked or guarded against.

### Recommendation
Do not trust the return-value/no-revert signal alone. Snapshot the checking account's (or beneficiary's) ERC20 balance via `balanceOf` before and after the `transfer` call, and credit/require exactly the observed balance delta rather than the requested `amount`; reject the transaction if the delta is less than requested. Alternatively, restrict `ERC20Matcher`/this transactor to an allowlist of vetted, standards-compliant ERC20 contracts (analogous to how `TrustBackedAssets`/`ForeignAssets` require explicit registration), closing the permissionless "any `AccountKey20`" matching path.

### Proof of Concept
1. Deploy an ERC20 contract via `pallet_revive` whose `transfer(to, value)` deducts `value` from sender but credits `to` with `value - fee` (fee-on-transfer), while still returning `true`.
2. Submit an XCM `WithdrawAsset` referencing `AccountKey20{key: <fee_token_address>}` for `amount = X` from the attacker's own balance (attacker only needs enough tokens to trigger one non-reverting `transfer`, e.g. `X`).
3. `withdraw_asset_with_surplus` calls `transfer(checking_address, X)`; it reverts/returns `false` never — it returns `true`. `Erc20Credit(X)` is placed into holding despite the checking account only receiving `X - fee`.
4. Follow with `DepositAsset` to any beneficiary for `X`; `deposit_asset_with_surplus` calls `transfer(beneficiary, X)` from the checking account. Because the checking account's real balance is `X - fee` (short by `fee`), the beneficiary either receives less than intended (silent value loss) or, once multiple such withdraw operations accumulate holding credits beyond the checking account's true balance, later legitimate depositors' `transfer` calls begin failing/reverting for insufficient balance — demonstrating the accounting desync and consequent fund lock/loss for the shared checking account.

### Citations

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L159-181)
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

**File:** cumulus/parachains/runtimes/assets/common/src/lib.rs (L132-160)
```rust
/// `Contains<Location>` implementation that matches locations with no parents,
/// a `PalletInstance` and an `AccountKey20` junction.
pub struct IsLocalAccountKey20;
impl Contains<Location> for IsLocalAccountKey20 {
	fn contains(location: &Location) -> bool {
		matches!(location.unpack(), (0, [AccountKey20 { .. }]))
	}
}

/// Fallible converter from a location to a `H160` that matches any location ending with
/// an `AccountKey20` junction.
pub struct AccountKey20ToH160;
impl MaybeEquivalence<Location, H160> for AccountKey20ToH160 {
	fn convert(location: &Location) -> Option<H160> {
		match location.unpack() {
			(0, [AccountKey20 { key, .. }]) => Some((*key).into()),
			_ => None,
		}
	}

	fn convert_back(key: &H160) -> Option<Location> {
		Some(Location::new(0, [AccountKey20 { key: (*key).into(), network: None }]))
	}
}

/// [`xcm_executor::traits::MatchesFungibles`] implementation that matches
/// ERC20 tokens.
pub type ERC20Matcher =
	MatchedConvertedConcreteId<H160, u128, IsLocalAccountKey20, AccountKey20ToH160, TryConvertInto>;
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
