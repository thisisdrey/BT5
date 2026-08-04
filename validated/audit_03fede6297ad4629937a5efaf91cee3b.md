### Title
ERC20 Asset Transactor credits nominal amount into XCM holding without verifying actual tokens moved, enabling accounting mismatch and cross-chain fund loss for fee-on-transfer/deflationary tokens - (File: `cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs`)

### Summary
`ERC20Transactor::withdraw_asset_with_surplus` and `ERC20Transactor::deposit_asset_with_surplus` implement XCM's `TransactAsset` trait for ERC20 tokens deployed on `pallet-revive`. Both functions invoke the ERC20 contract's `transfer` function and only check the boolean return value; neither verifies the actual balance delta of the checking account or beneficiary via `balanceOf` before/after the call. This mirrors exactly the pattern flagged in the external report (ERC-20 transfers assumed to move the full stated amount, with no before/after balance check), and is a real, exploitable analog because Asset Hub's ERC20 support is a live, in-scope XCM asset-transfer path.

### Finding Description
In `withdraw_asset_with_surplus` (line ~150), the code performs `IERC20::transferCall { to: checking_address, value: amount }` from the user to the `TransfersCheckingAccount`, then, on `is_success == true`, unconditionally credits the XCM holding register with the full nominal `amount` via `Erc20Credit(amount)`: [1](#0-0) 

The comment on `Erc20Credit` itself acknowledges that "the actual balance constraints are enforced by the ERC20 smart contract itself rather than the runtime" — i.e., the runtime's XCM holding value is a *trusted claim*, not a verified fact: [2](#0-1) 

Symmetrically, `deposit_asset_with_surplus` (line ~225) calls `transfer(beneficiary, amount)` from the checking account and treats a `true` return as full success, again without checking `balanceOf` deltas: [3](#0-2) 

If the ERC20 contract implements a fee-on-transfer, deflationary, or rebasing mechanism (a normal, spec-compliant pattern for many real-world ERC20 tokens — the exact bug class from the external report), `transfer` returns `true` while moving fewer tokens than `amount`:
- On withdraw: the sender is debited the full `amount` from their own balance (correct), but the `TransfersCheckingAccount` receives less than `amount`. The XCM engine nonetheless treats the holding register as containing the full nominal `amount` (`Erc20Credit(amount)`), an overstatement of what the checking account actually holds.
- On deposit: when that (overstated) holding amount is later deposited to a beneficiary — potentially in the *same* XCM program (e.g., `withdraw_asset` → `deposit_asset` to a different beneficiary, or `refund_surplus` → `deposit_asset` back to sender) or in a follow-on remote-transfer flow — the transactor attempts to move the full nominal `amount` out of the checking account again. Because the checking account's real balance was never topped up by the fee amount that leaked to the token contract/burn address, repeated withdraw/deposit cycles progressively make the `TransfersCheckingAccount`'s actual ERC20 balance diverge from what the runtime's XCM bookkeeping assumes it holds.

Unlike the native `FungiblesAdapter`/`FungibleAdapter` fungible/asset accounting, which is enforced by an internal ledger (`pallet-balances`/`pallet-assets`) that cannot be manipulated externally, the ERC20 asset here is external, mutable state controlled by a third-party contract, and the transactor performs zero reconciliation against it. `pallet_revive`'s `impl_fungibles.rs` `mint_into`/`burn_from` — used by the ordinary `FungiblesAdapter` path for the same ERC20 asset type — has the identical flaw: it credits/returns the post-call `balance()` query result as if it reflects the transferred `amount`, again trusting the contract's transfer semantics rather than checking that the delta equals `amount`. [4](#0-3) 

Existing safeguards in the test suite (`smart_contract_not_erc20_will_error`, `smart_contract_does_not_return_bool_fails`) only cover contracts that revert or fail to return a bool — they do not cover a contract that returns `true` while moving less value than requested, which is a normal and valid ERC20 behavior for deflationary tokens, so these guards do not stop the path: [5](#0-4) 

### Impact Explanation
This breaks the "conserve value and settle exactly once to the rightful beneficiary and amount" invariant for asset transfers on Asset Hub. Concretely:
- The XCM holding register (and hence the amount reported as deposited to a beneficiary, refunded to a sender, or forwarded in a multi-hop reserve transfer) can diverge from the actual ERC20 balance moved.
- Over repeated use, the `TransfersCheckingAccount` becomes under-collateralized relative to the nominal amounts the runtime believes it can dispense, which can eventually cause deposit/refund legs of unrelated, honest users' XCM programs to fail (denial of service / fund lock for that XCM execution, since deposit into holding succeeded nominally but the physical transfer to the true beneficiary fails), or — depending on how the deficit accumulates versus surplus from any prior successful cycles — allow discrepancies where minted/credited XCM value does not match on-chain reality for this asset class.
- This is a public, unprivileged-entrypoint issue: any user can call `pallet_xcm::execute`/`transfer_assets` referencing a fee-on-transfer ERC20 as the asset, no admin/governance/relayer/malicious-validator involvement required.

### Likelihood Explanation
Likelihood is moderate-to-high in the sense that fee-on-transfer/deflationary ERC20 tokens are common in the wild (this is the exact premise of the source report), and any user can deploy or reference such a contract on `pallet-revive` and use it in an XCM program that exercises `ERC20Transactor::withdraw_asset_with_surplus`/`deposit_asset_with_surplus`. No special privileges, validator collusion, or relayer trust assumptions are needed — only a standard fee-on-transfer contract and a normal `pallet_xcm::execute`/`transfer_assets` call.

### Recommendation
Before crediting `Erc20Credit(amount)` in `withdraw_asset_with_surplus`, and before treating `deposit_asset_with_surplus` as fully successful, query `balanceOf` on the `TransfersCheckingAccount`/beneficiary before and after the `transfer` call and use the observed delta (not the requested `amount`) as the amount credited to the XCM holding register / considered settled. If the delta is less than requested, either fail the operation (`FailedToTransactAsset`) or credit only the actual delta so that XCM bookkeeping never diverges from the true on-chain balance. Apply the same before/after balance check in `pallet_revive`'s `impl_fungibles.rs` `mint_into`/`burn_from`.

### Proof of Concept
1. Deploy a spec-compliant ERC20 contract on Asset Hub's `pallet-revive` whose `transfer(to, value)` burns/retains e.g. 2% of `value` as a fee and forwards only 98% to `to`, returning `true` (this is a standard, non-malicious ERC20 pattern; see the existing `MyToken`/fixture pattern used in `withdraw_and_deposit_erc20s`, modified to include a fee).
2. Craft an XCM program via `pallet_xcm::execute` (as in `withdraw_and_deposit_erc20s`) that does `withdraw_asset` for `erc20_transfer_amount` of this token, followed by `deposit_asset` to a beneficiary.
3. Observe: `withdraw_asset_with_surplus` calls `transfer(checking_account, amount)`; checking account's real ERC20 balance increases by only 98% of `amount`, yet `Erc20Credit(amount)` credits the full `amount` into holding.
4. `deposit_asset_with_surplus` then calls `transfer(beneficiary, amount)` from the checking account for the full nominal amount — either this call fails once the checking account's accumulated shortfall exceeds its real balance (causing failed/DoS'd deposits for other unrelated users sharing the same checking account), or it succeeds by drawing down balance that should have been reserved for other pending nominal credits, silently propagating the shortfall to future executions.
5. Repeat across multiple XCM executions to demonstrate the checking account's real ERC20 balance progressively falls behind the sum of nominal amounts the runtime believes are backed, at which point subsequent honest users' deposits fail non-deterministically depending on execution order — a concrete instance of the "balance check before/after transfer" gap described in the source report, now affecting Asset Hub's XCM-ERC20 bridge rather than a third-party lending protocol.

### Citations

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L73-79)
```rust
/// A minimal imbalance tracking type that holds an ERC20 token amount.
///
/// This type implements the necessary imbalance accounting traits but does not perform
/// runtime-level balance enforcement. It's used to track ERC20 token amounts within XCM
/// asset holdings, where the actual balance constraints are enforced by the ERC20 smart
/// contract itself rather than the runtime.
struct Erc20Credit(u128);
```

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L185-207)
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

**File:** substrate/frame/revive/src/impl_fungibles.rs (L161-203)
```rust
impl<T: Config> fungibles::Mutate<<T as frame_system::Config>::AccountId> for Pallet<T> {
	fn burn_from(
		asset_id: Self::AssetId,
		who: &T::AccountId,
		amount: Self::Balance,
		_: Preservation,
		_: Precision,
		_: Fortitude,
	) -> Result<Self::Balance, DispatchError> {
		let checking_account_eth = T::AddressMapper::to_address(&Self::checking_account());
		let checking_address = Address::from(Into::<[u8; 20]>::into(checking_account_eth));
		let data =
			IERC20::transferCall { to: checking_address, value: EU256::from(amount) }.abi_encode();
		let ContractResult { result, weight_consumed, .. } = Self::bare_call(
			OriginFor::<T>::signed(who.clone()),
			asset_id,
			U256::zero(),
			TransactionLimits::WeightAndDeposit {
				weight_limit: WEIGHT_LIMIT,
				deposit_limit:
					<<T as pallet::Config>::Currency as fungible::Inspect<_>>::total_issuance(),
			},
			data,
			&ExecConfig::new_substrate_tx(),
		);
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

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/tests/tests.rs (L1971-2017)
```rust
#[test]
fn smart_contract_not_erc20_will_error() {
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

		let (code, _) = compile_module("dummy").unwrap();

		let Contract { addr: non_erc20_address, .. } = bare_instantiate(&sender, code)
			.transaction_limits(TransactionLimits::WeightAndDeposit {
				weight_limit: Weight::from_parts(500_000_000_000, 10 * 1024 * 1024),
				deposit_limit: Balance::MAX,
			})
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
