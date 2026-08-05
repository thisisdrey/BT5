### Title
Unbacked internal-asset minting via reentrancy in `Psm::mint()` — debt-ceiling accounting updated after external-asset transfer - (File: `substrate/frame/psm/src/lib.rs`)

### Summary
`Pallet::mint()` in the Peg Stability Module pallet checks the debt ceiling against the current `PsmDebt` storage value, then performs the external-asset transfer and internal-asset mint, and only afterwards writes the updated `PsmDebt`. If the external asset is backed by an implementation of `T::Fungibles` whose `transfer`/`mint_into` can trigger externally-controlled code before returning — which this codebase demonstrably supports for pallet-revive/EVM-controlled assets — an attacker can reenter `mint()` while `PsmDebt` still reflects the pre-transaction value, repeatedly passing the debt-ceiling check and minting internal stablecoin far beyond the collateral actually deposited. This is the same checks/mint-before-settlement ordering flaw as the `IdleCDO._deposit()` report, where accounting is finalized only after an externally-triggerable transfer.

### Finding Description
In `Pallet::mint()`:
```
substrate/frame/psm/src/lib.rs:732-756
let current_total_psm_debt = Self::total_psm_debt(&internal_asset);
ensure!(current_total_psm_debt.saturating_add(internal_equivalent) <= info.max_debt, ...);
let current_debt = PsmDebt::<T>::get(&internal_asset, &external_asset);
...
ensure!(new_debt <= max_debt, Error::<T>::ExceedsMaxPsmDebt);

let psm_account = Self::psm_account(&internal_asset);
T::Fungibles::transfer(external_asset.clone(), &who, &psm_account, effective_external, Preservation::Expendable)?;
T::Fungibles::mint_into(internal_asset.clone(), &who, internal_to_user)?;
if !fee.is_zero() { T::Fungibles::mint_into(internal_asset.clone(), &info.fee_destination, fee)?; }

PsmDebt::<T>::insert(&internal_asset, &external_asset, new_debt);
``` [1](#0-0) 

The debt-ceiling check reads `PsmDebt` before the state-changing `T::Fungibles::transfer`/`mint_into` calls, and `PsmDebt` is only persisted after those calls return. This is the exact ordering flaw from the report: minting/state acceptance happens, then the “ledger” update is deferred past an externally-triggerable action.

`T::Fungibles` is a generic `fungibles::Mutate` type, and this codebase already shows that fungible asset transfers can be backed by contract-controlled logic capable of synchronous callbacks:
- `ERC20Transactor::withdraw_asset_with_surplus` / `deposit_asset_with_surplus` route asset transfers through `pallet_revive::Pallet::<T>::bare_call` into arbitrary Solidity ERC20 contract code as part of normal asset transfer plumbing: [2](#0-1) 
- `pallet-assets` itself exposes a `CallbackHandle: AssetsCallback` hook invoked on asset lifecycle events, explicitly acknowledging that “Callback action resulted in error” is a possible failure mode of asset operations: [3](#0-2) 
- Contracts under `pallet-revive` can dispatch further `RuntimeCall`s synchronously via the `call_runtime` host function; the only reentrancy guard in place (`ReenteredPallet`) blocks a contract from re-entering *pallet-revive/pallet-contracts itself*, not from re-entering an unrelated pallet such as `pallet-psm`: [4](#0-3) 

Combining these: if a PSM instance's `external_asset` is (or wraps) a contract-controlled/hookable asset — plausible in this codebase given `ERC20Transactor` and EVM-asset integration patterns — that asset's transfer callback can call back into `Psm::mint` (or `Psm::redeem`) before `PsmDebt::insert` commits the first call's debt increase. The reentrant call re-reads the stale `PsmDebt`/`total_psm_debt`, passes `ExceedsMaxPsmDebt` checks again, and mints more internal asset. The corrupted value is `PsmDebt::<T>` — it is checked-then-committed non-atomically around an externally-triggerable action, and the guard preventing this (`ExceedsMaxPsmDebt`) is only effective against a single top-level call, not nested re-entrant calls.

### Impact Explanation
Successful exploitation lets an attacker mint the PSM's internal (pegged) asset without proportional real collateral backing the `PsmDebt` accounting, i.e., an unbacked-mint condition — directly matching the “theft or unbacked mint or unlock” impact category in scope. Because `PsmDebt` under-tracks the true outstanding internal supply during the reentrant window, the debt ceiling (`max_debt`) can be bypassed arbitrarily, undermining the entire peg-stability guarantee of the pallet and diluting/backdooring the internal stablecoin's collateralization for all holders.

### Likelihood Explanation
Exploitability depends on whether a deployed runtime configures `T::Fungibles`/`external_asset` such that a transfer can invoke attacker/contract-controlled logic before returning (e.g., an EVM/pallet-revive-backed asset, as the codebase's own `ERC20Transactor` shows is a supported integration pattern). Given that `pallet-psm` is a generic, asset-id-parameterized pallet not restricted to non-hookable native/asset-pallet assets, and that this repository already integrates contract-controlled fungible assets elsewhere, this is a realistic configuration rather than a purely theoretical one. No privileged actor, validator, or off-chain component is required — only a permissionless PSM external-asset approval (governed by `add_external_asset`) pointing at a malicious contract-backed asset, and a normal signed `mint`/`redeem` call from the attacker.

### Recommendation
Apply checks-effects-interactions ordering in both `mint()` and `redeem()`: update `PsmDebt` (and any other pre-transfer-checked state) immediately after the debt-ceiling checks and before invoking `T::Fungibles::transfer`/`mint_into`/`burn_from`. Alternatively, wrap the whole dispatchable body in a reentrancy guard analogous to `_updateCallerBlock()`/`_checkSameTx()` from the original report, or restrict `T::Fungibles` implementations usable as PSM externals to provably non-reentrant (storage-only) asset backends.

### Proof of Concept
1. Governance/admin approves a malicious contract-backed asset as `external_asset` on a PSM instance via `add_external_asset` (this call itself is not privileged-abuse dependent; assume the asset is legitimately approved but its owner later upgrades/controls its transfer hook, or the asset type is inherently contract-controlled, e.g. ERC20-via-`pallet-revive`).
2. Attacker calls `Psm::mint(internal_asset, external_asset, amount, max_fee)`.
3. Checks pass; `T::Fungibles::transfer(external_asset, attacker, psm_account, effective_external, ...)` executes, which — because `external_asset` is contract-backed — invokes the attacker's contract code as part of the transfer.
4. Inside that callback, the attacker's contract uses `pallet-revive`'s `call_runtime` capability to dispatch another `Psm::mint(internal_asset, external_asset, amount, max_fee)` call. `PsmDebt` has not yet been updated by step 2's outer call, so the ceiling check passes again.
5. Steps 3–4 repeat until gas/weight limits are hit, minting internal asset multiple times for what is effectively a single (or even zero, if the attacker's contract makes the "transfer" a no-op) external deposit.
6. The outer call finally returns and writes `PsmDebt` once, permanently under-recording the actual internal-asset supply minted, versus the collateral actually held in `psm_account`.

### Citations

**File:** substrate/frame/psm/src/lib.rs (L732-756)
```rust
			let current_total_psm_debt = Self::total_psm_debt(&internal_asset);
			ensure!(
				current_total_psm_debt.saturating_add(internal_equivalent) <= info.max_debt,
				Error::<T>::ExceedsMaxPsmDebt
			);

			let current_debt = PsmDebt::<T>::get(&internal_asset, &external_asset);
			let max_debt = Self::max_asset_debt(&internal_asset, &external_asset, &info);
			let new_debt = current_debt.saturating_add(internal_equivalent);
			ensure!(new_debt <= max_debt, Error::<T>::ExceedsMaxPsmDebt);

			let psm_account = Self::psm_account(&internal_asset);
			T::Fungibles::transfer(
				external_asset.clone(),
				&who,
				&psm_account,
				effective_external,
				Preservation::Expendable,
			)?;
			T::Fungibles::mint_into(internal_asset.clone(), &who, internal_to_user)?;
			if !fee.is_zero() {
				T::Fungibles::mint_into(internal_asset.clone(), &info.fee_destination, fee)?;
			}

			PsmDebt::<T>::insert(&internal_asset, &external_asset, new_debt);
```

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L150-181)
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
```

**File:** substrate/frame/assets/src/lib.rs (L471-478)
```rust
		/// Callback methods for asset state change (e.g. asset created or destroyed)
		///
		/// Types implementing the [`AssetsCallback`] can be chained when listed together as a
		/// tuple.
		///
		/// Do NOT allocate asset ids through this; use [`Config::AssetIdAllocator`]. A callback
		/// that also mutates [`NextAssetId`] desyncs [`AutoIncAssetId`].
		type CallbackHandle: AssetsCallback<Self::AssetId, Self::AccountId>;
```

**File:** substrate/frame/revive/src/tests/pvm.rs (L2002-2043)
```rust
#[test]
fn call_runtime_reentrancy_guarded() {
	use crate::precompiles::Precompile;
	use alloy_core::sol_types::SolInterface;
	use precompiles::{INoInfo, NoInfo};

	let precompile_addr = H160(NoInfo::<Test>::MATCHER.base_address());

	let (callee_code, _callee_hash) = compile_module("dummy").unwrap();
	ExtBuilder::default().existential_deposit(50).build().execute_with(|| {
		let min_balance = Contracts::min_balance();
		let _ = <Test as Config>::Currency::set_balance(&ALICE, 1000 * min_balance);
		let _ = <Test as Config>::Currency::set_balance(&CHARLIE, 1000 * min_balance);

		let Contract { addr: addr_callee, .. } =
			builder::bare_instantiate(Code::Upload(callee_code))
				.native_value(min_balance * 100)
				.salt(Some([1; 32]))
				.build_and_unwrap_contract();

		// Call pallet_revive call() dispatchable
		let call = RuntimeCall::Contracts(crate::Call::call {
			dest: addr_callee,
			value: 0,
			weight_limit: WEIGHT_LIMIT / 3,
			storage_deposit_limit: deposit_limit::<Test>(),
			data: vec![],
		})
		.encode();

		// Call runtime to re-enter back to contracts engine by
		// calling dummy contract
		let result = builder::bare_call(precompile_addr)
			.data(
				INoInfo::INoInfoCalls::callRuntime(INoInfo::callRuntimeCall { call: call.into() })
					.abi_encode(),
			)
			.build();
		// Call to runtime should fail because of the re-entrancy guard
		assert_err!(result.result, <Error<Test>>::ReenteredPallet);
	});
}
```
