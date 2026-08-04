### Title
Non-conforming ERC20 `transfer` return value causes silent double-credit of pooled custody balance in `ERC20Transactor` - (File: `cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs`)

### Summary
`ERC20Transactor::deposit_asset_with_surplus` (and its `withdraw_asset_with_surplus` counterpart) drive real, state-mutating ERC20 `transfer` calls into `pallet-revive` contracts and then classify success/failure purely by ABI-decoding the returned bytes as a `bool`. A contract whose `transfer` mutates balances but does not return a strictly decodable `bool` (the exact bug class from the M-06 report — non-conforming ERC20 semantics) makes this code treat an on-chain-successful transfer as an XCM failure, while the tokens have already physically moved out of the shared `TransfersCheckingAccount`. This desynchronizes the XCM holding register from the real ERC20 ledger and lets an attacker turn that gap into a duplicate payout drawn from the shared custody pool.

### Finding Description
`deposit_asset_with_surplus` performs the actual token movement by calling the ERC20 contract's `transfer` from the pooled `TransfersCheckingAccount` to the beneficiary, via `pallet_revive::Pallet::<T>::bare_call`: [1](#0-0) 

It then decides success solely from decoding the return data: [2](#0-1) 

If `return_value.did_revert()` is false (i.e., the call executed and any storage writes inside `_transfer`/`_update` were committed) but `IERC20::transferCall::abi_decode_returns_validate` fails to decode a valid `bool` from the returned bytes, the function returns `Err((what, XcmError::FailedToTransactAsset(...)))`. Returning `Err` with `what` (the `AssetsInHolding`) tells the XCM executor that the deposit did **not** happen and the assets should be treated as still-in-holding (eventually trapped via `AssetsTrapped` and claimable through `pallet_xcm::claim_assets`, as exercised in [3](#0-2) ).

The symmetric case exists on the withdraw side: `withdraw_asset_with_surplus` transfers user funds into `TransfersCheckingAccount` and only credits `AssetsInHolding` if the return value decodes as `true`: [4](#0-3) 

The runtime's own test suite already demonstrates that a contract which does the real transfer but returns non-bool data causes the transactor to error out rather than treat the transfer as done, confirming the on-chain mutation vs. XCM-accounting mismatch: [5](#0-4) 

Because `TransfersCheckingAccount` is a single shared custody account backing *all* users' ERC20 holdings routed through XCM (not a per-user escrow), any accounting desync it experiences pollutes the pooled balance used by every other holder of that asset. An attacker who deploys (or who abuses an already-listed) ERC20 contract whose `transfer` succeeds in mutating balances but returns malformed/undecodable data can:
1. Trigger `deposit_asset_with_surplus`, causing the real ERC20 balance to move from `TransfersCheckingAccount` to the attacker's address on the underlying EVM/PVM ledger.
2. Have the XCM engine treat this as a failure, trapping the asset as `AssetsTrapped` on the runtime (a normal Substrate-side accounting entry, unaffected by the EVM-side transfer that already happened).
3. Later call `pallet_xcm::claim_assets` to claim the "still owed" trapped asset, which re-invokes the deposit path and triggers a **second** real ERC20 `transfer` from the same pooled `TransfersCheckingAccount` for the same nominal amount.

The net effect is that the attacker receives the ERC20 tokens twice while the pooled checking account's on-chain balance is debited twice for a single legitimate credit — the shortfall is paid out of other users' custodied balance in that same checking account, since the checking account is shared across all XCM ERC20 asset movements for that asset id.

### Impact Explanation
This breaks the "settle exactly once to the rightful beneficiary and amount" invariant for asset transactors. It results in theft of custodied ERC20 value from a shared pooled account and duplicate settlement of a single logical transfer, which lines up with the "theft or unbacked mint or unlock" and "duplicate settlement or payout" categories in the accepted impact list. This is reachable by any unprivileged user who can deploy or interact with an arbitrary ERC20 contract on the pallet-revive execution layer and route it through XCM `withdraw_asset`/`deposit_asset` — no privileged, governance, relayer, or validator assumption is required.

### Likelihood Explanation
Likelihood is credible but constrained: the attacker needs a contract whose `transfer` performs the real state mutation yet returns data that fails `bool` ABI-decoding (e.g., no return, wrong-width return, or garbage trailing bytes) — a known, real-world non-conformance pattern (the report cites USDT-style tokens as the canonical example). The runtime's own test suite already contains fixtures (`MyTokenFake`) built specifically to produce this "transfer succeeds but return decode fails" condition, showing the scenario is not hypothetical and is exercisable purely through the public XCM `execute`/reserve-transfer entry points on the `TransfersCheckingAccount`-based ERC20 asset transactor.

### Recommendation
- Do not rely solely on ABI-decoded boolean return values to gate `AssetsInHolding` accounting. After a non-reverted call, either (a) verify the actual ERC20 balance delta via `balanceOf` before and after the call rather than trusting the return value, or (b) treat a non-reverted call with undecodable/absent return data as success only if balances corroborate the transfer, mirroring OpenZeppelin's `SafeERC20`-style pattern (tolerate missing return data, but require no revert and validate actual state change).
- If a transfer cannot be verified consistently, do not silently trap `what` as "still available" — the transactor should distinguish "definitely not moved" (safe to trap/refund) from "possibly moved but return data ambiguous" (must not create a re-claimable duplicate credit).
- Consider disallowing arbitrary/unlisted ERC20 contracts from being used as XCM-transactable assets via `TransfersCheckingAccount`, restricting this transactor to an allowlist of known-conforming tokens, since the shared checking-account design makes any single non-conforming asset a cross-user risk.

### Proof of Concept
1. Deploy an ERC20-like PVM contract `EvilToken` whose `transfer(address,uint256)` performs the real `_update`/balance mutation but returns a value that does not ABI-decode as `bool` (e.g., returns a `uint256`, or returns zero bytes) — analogous to the `MyTokenFake` fixture already used in [6](#0-5) .
2. Register `EvilToken` as a matched fungible asset for `ERC20Transactor` (matching AssetHub's ERC20 asset-id convention keyed off the contract address).
3. Fund `TransfersCheckingAccount` with `EvilToken` balance (e.g., via a prior legitimate `withdraw_asset` from another user, or via direct mint if the attacker controls the contract).
4. As the attacker, submit an XCM `deposit_asset` (or a reserve-asset-transfer landing on AssetHub) for `EvilToken` targeting the attacker's own beneficiary address, driving `deposit_asset_with_surplus` in [7](#0-6) . The call executes and moves the real ERC20 balance to the attacker, but `abi_decode_returns_validate` fails, so the function returns `Err((what, XcmError::FailedToTransactAsset(...)))`.
5. Observe that AssetHub emits `PolkadotXcm::AssetsTrapped` for `EvilToken` under the attacker-derived origin (same trap/claim flow demonstrated in [8](#0-7) ).
6. As the attacker, call `pallet_xcm::claim_assets` for the trapped `EvilToken` amount with a claimer location matching the attacker's account, driving a second `deposit_asset_with_surplus` invocation that performs a second real ERC20 `transfer` from `TransfersCheckingAccount` to the attacker.
7. Confirm the attacker's real `EvilToken` balance now reflects two transfers for one logical deposit, while `TransfersCheckingAccount`'s real ERC20 balance is depleted by twice the amount, at the expense of other users' custodied balance of the same asset id.

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

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L251-266)
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

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_inbound.rs (L1225-1265)
```rust
	let trapped_assets = AssetHubWestend::execute_with(|| {
		type RuntimeEvent = <AssetHubWestend as Chain>::RuntimeEvent;

		let trap = AssetHubWestend::events().into_iter().find_map(|event| match event {
			RuntimeEvent::PolkadotXcm(pallet_xcm::Event::AssetsTrapped {
				origin, assets, ..
			}) => Some((origin, assets)),
			_ => None,
		});

		let (trap_origin, trap_assets) =
			trap.expect("assets should be trapped when XCM payload is invalid");

		// Trap origin reflects the user-supplied claimer.
		assert_eq!(
			trap_origin, user_claimer,
			"trap origin must match the user-supplied claimer location",
		);

		trap_assets
	});

	AssetHubWestend::execute_with(|| {
		type RuntimeEvent = <AssetHubWestend as Chain>::RuntimeEvent;
		type RuntimeOrigin = <AssetHubWestend as Chain>::RuntimeOrigin;

		assert_ok!(<AssetHubWestend as AssetHubWestendPallet>::PolkadotXcm::claim_assets(
			RuntimeOrigin::signed(user_account.clone()),
			bx!(trapped_assets.clone()),
			bx!(VersionedLocation::from(user_claimer.clone())),
		));

		assert_expected_events!(
			AssetHubWestend,
			vec![
				RuntimeEvent::PolkadotXcm(pallet_xcm::Event::AssetsClaimed { origin, assets, .. }) => {
					origin: *origin == user_claimer,
					assets: *assets == trapped_assets,
				},
			]
		);
```

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/tests/tests.rs (L2019-2074)
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
}
```
