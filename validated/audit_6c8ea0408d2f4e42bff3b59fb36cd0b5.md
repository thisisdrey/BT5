I found a real, exact analog: `ERC20Transactor` in `cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs`, used by Asset Hub Westend to let **any user-deployed `pallet-revive` smart contract** be treated as an XCM-fungible asset, exactly mirroring the reported class of bug (unvetted, non-standard ERC-20 accounting causing bridge/asset-transactor fund loss or lock).

### Title
ERC20Transactor trusts the token's own `transfer()` return value instead of verifying real balance deltas, allowing non-standard ERC-20 contracts to mint phantom XCM-holding credit or lock funds in the checking account - (File: `cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs`)

### Summary
`ERC20Transactor::withdraw_asset_with_surplus` and `deposit_asset_with_surplus` implement the `TransactAsset` XCM trait for arbitrary `pallet-revive` smart-contract addresses matched as `AccountKey20` asset ids [1](#0-0) . As documented in `prdoc/stable2506/pr_7762.prdoc`, any asset id of the shape `AccountKey20 { key, network }` is matched and the contract at that address is called directly, with no allowlist or vetting of the contract's behavior [2](#0-1) . This is the direct structural analog of the reported bug: `L1/L2StandardBridge.sol` let anyone register/link arbitrary ERC-20 tokens and assumed transferred/minted/burned amounts always equal the requested `amount`, which breaks for fee-on-transfer, deflationary, blocklisting, or non-conforming tokens.

### Finding Description
On withdraw, the transactor calls `IERC20::transferCall` for the *requested* `amount` and, if the call returns `true` (or doesn't revert), unconditionally credits `AssetsInHolding` with that same requested `amount` via `Erc20Credit(amount)` — it never checks the actual balance change of the checking account: [3](#0-2) 

On deposit, the same pattern is used: the transactor calls `transfer(beneficiary, amount)` from the checking account and treats a truthy return as proof that the beneficiary received exactly `amount`: [4](#0-3) 

Because `Matcher::matches_fungibles` accepts any `AccountKey20` address as a valid asset id (as documented in the PR that introduced this transactor) [5](#0-4) , an unprivileged user can deploy any `pallet-revive` contract implementing a `transfer` function that returns `true`/non-reverting while behaving non-standardly:
- A **fee-on-transfer / deflationary** contract that burns part of the value on each `transfer()` call but still returns `true`. On withdraw, the executor believes the checking account received the full requested `amount` and issues `AssetsInHolding` credit for that full amount — creating **phantom credit not backed by real tokens held** in the checking account.
- A contract that **reports success but doesn't actually move funds to the beneficiary** on deposit — the `AssetsInHolding` is consumed (debited from the sender/holding) but the beneficiary never receives it, and the tokens stay stuck in the `TransfersCheckingAccount`, permanently locking user funds — the exact "block user funds" analog from the report.
- A contract with a **blocklist** on a party in the flow will make the `deposit_asset` call revert or return false — the code does catch reverts/false results as errors [6](#0-5) , but the asset has already been irrevocably debited from holding earlier in the XCM program (e.g., withdrawn from sender), so on failure the value is already gone from the sender's control and cannot be safely refunded to them, matching the "funds get blocked" impact.

The existing guards (checking `did_revert()` and decoding the boolean return value) only validate the *reported* outcome of the `transfer()` call — they do not perform a `balanceOf` before/after comparison, so they cannot catch fee-on-transfer, deflationary, or partially-successful transfers. This is structurally identical to the reported `L1/L2StandardBridge.sol` weakness: trusting the token's self-reported success/return value as ground truth for the amount moved, rather than measuring actual balance deltas.

### Impact Explanation
This breaks the invariant that XCM `AssetsInHolding` accounting must conserve real value 1:1 with actual token balances (Balances/assets/contract-held value must conserve value and settle exactly once to the rightful beneficiary and amount). A malicious or non-conforming ERC-20 contract can cause: (1) fabricated holding credit not backed by real balance in the checking account, enabling further XCM instructions (e.g. `DepositAsset`) to pay out more than was actually collected, effectively creating unbacked value inside a single XCM program's holding register; and (2) permanent loss/lock of user funds when a deposit-side transfer silently fails to reach the beneficiary after being irrevocably withdrawn upstream.

### Likelihood Explanation
High feasibility for an unprivileged attacker: deploying arbitrary Solidity/PVM contracts via `pallet-revive` is a normal permissionless user action already exercised in existing tests (`withdraw_and_deposit_erc20s`, `smart_contract_does_not_return_bool_fails`, etc. in `cumulus/parachains/runtimes/assets/asset-hub-westend/tests/tests.rs`), which confirm the transactor is wired into the live Asset Hub Westend XCM executor and reachable via a standard `PolkadotXcm::execute` extrinsic [7](#0-6) . No governance, relayer, or privileged role is required — only crafting and deploying a non-standard ERC-20.

### Recommendation
- In `withdraw_asset_with_surplus`, read `balanceOf(checking_account)` before and after the `transfer` call, and credit `AssetsInHolding` only with the actual observed delta, not the requested `amount`.
- In `deposit_asset_with_surplus`, similarly read `balanceOf(beneficiary)` before/after and treat a mismatch as a hard failure so the XCM program errors rather than silently losing funds.
- Consider maintaining an allowlist of vetted ERC-20 contracts (mirroring the report's own recommendation) that are known not to implement fee-on-transfer, rebasing, or blocklist behavior, rejecting `Matcher::matches_fungibles` for unlisted contract addresses.

### Proof of Concept
1. Deploy a "FeeOnTransferToken" `pallet-revive` contract whose `transfer(to, value)` moves only `value * 99 / 100` to `to` (burning 1%) but always returns `true`.
2. Fund `sender` with this token and submit an XCM program via `PolkadotXcm::execute`:
   `withdraw_asset((AccountKey20{key: token, ..}, 1_000_000))` → `deposit_asset(AllCounted(1), attacker_beneficiary)`.
3. During `withdraw_asset_with_surplus`, the transactor calls `transfer(checking_account, 1_000_000)`, which actually delivers only `990_000` to `checking_account`, but the call returns `true`, so the executor credits `AssetsInHolding` with the full `1_000_000` [8](#0-7) .
4. During `deposit_asset_with_surplus`, the transactor calls `transfer(attacker_beneficiary, 1_000_000)` from `checking_account`, but `checking_account` only actually holds `990_000` real tokens — this either reverts (visible loss of the discrepancy henceforth) or, depending on the contract's own accounting, can under/over transfer while still returning `true`, leaving the checking account permanently short by the fee amount across repeated calls, silently accumulating an unbacked deficit that can eventually make legitimate withdrawals fail or drain the checking account's real balance, i.e., cause fund loss/lock for other users of this shared checking account, exactly as in `L1StandardBridge.sol`.

### Citations

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L150-169)
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

**File:** prdoc/stable2506/pr_7762.prdoc (L1-19)
```text
# Schema: Polkadot SDK PRDoc Schema (prdoc) v1.0.0
# See doc at https://raw.githubusercontent.com/paritytech/polkadot-sdk/master/prdoc/schema_user.json

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
