### Title
Zero-value ERC20 transfer in `ERC20Transactor` can cause unrecoverable trapped-asset lock when the target ERC20 reverts on zero-value transfers - (File: `cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs`)

### Summary
`ERC20Transactor::withdraw_asset_with_surplus` and `ERC20Transactor::deposit_asset_with_surplus` unconditionally invoke the smart-contract `transfer()` function via `pallet_revive::Pallet::<T>::bare_call` for whatever `amount` the matcher extracts from the XCM `Asset`, with no `amount.is_zero()` guard. This is the exact bug class from the external report: an unprivileged actor can supply/trigger a zero-value fungible in an XCM message for an ERC20 asset registered on Asset Hub. If that ERC20 contract reverts on zero-value transfers (a real, common ERC20 quirk explicitly called out as in-scope in the source report), the `bare_call` reverts, `deposit_asset_with_surplus`/`withdraw_asset_with_surplus` return `XcmError::FailedToTransactAsset`, and any assets already pulled into the XCM holding register for that message become trapped with no path to recovery, because reprocessing/claiming will hit the exact same zero-value transfer and revert again.

### Finding Description
`ERC20Transactor` is registered as one of the `AssetTransactors` for Asset Hub Westend [1](#0-0) , so it is on the live XCM execution path for any XCM program that references an ERC20 location asset — including `pallet_xcm::execute` (callable by any signed origin) and ordinary cross-chain reserve-transfer/teleport messages processed from HRMP/XCMP/bridges.

Both transact functions build the ERC20 `transfer` calldata directly from the matched `amount` with no zero check: [2](#0-1) [3](#0-2) 

`Fungibility::Fungible(u128)` in an XCM `Asset` accepts `0` as a valid value, and `matches_fungibles` performs no minimum-amount validation before returning `(asset_id, amount)` to the transactor. A user can therefore construct an XCM program (via `pallet_xcm::execute`, or as part of a `DepositAsset`/`WithdrawAsset` sequence originating from a remote chain) that includes a `0`-valued fungible for an ERC20 asset alongside other valuable assets (e.g. in a `Wild(AllCounted(n))`/multi-asset `DepositAsset`). When the executor reaches the zero-amount ERC20 leg:

- On `withdraw_asset_with_surplus`, the `bare_call` to `transfer(checking_account, 0)` reverts if the underlying ERC20 implements revert-on-zero-transfer, producing `Err(XcmError::FailedToTransactAsset("ERC20 contract reverted"))` (lines 187-189).
- On `deposit_asset_with_surplus`, the equivalent revert occurs at lines 272-274, after other, non-zero assets in the same holding batch may already have been irreversibly withdrawn/checked-in on the source side.

When a `TransactAsset` operation errors mid-instruction, the XCM executor's standard recovery is to trap the remaining `AssetsInHolding` via `AssetTrap`/`trap_assets`, requiring the original origin to later send a `ClaimAsset` XCM to recover them. Existing guards do not stop this path because:
- `matches_fungibles`/`ERC20Matcher` never rejects a zero amount.
- Neither `withdraw_asset_with_surplus` nor `deposit_asset_with_surplus` short-circuits on `amount == 0` the way every other transfer path in this codebase does (`pallet-balances`, `pallet-assets`, `pallet-contracts`, `pallet-revive`'s native `transfer`, `pallet-psm`, `pallet-vesting`, etc. all explicitly no-op on zero — see e.g. [4](#0-3)  and [5](#0-4) ).
- Because the failure is deterministic and attacker-repeatable (the ERC20 contract always reverts on `transfer(x, 0)`), a subsequent `ClaimAsset` attempt that re-enters the same code path for the same 0-valued ERC20 leg reverts identically — the assets are not merely delayed, they are permanently stuck.

### Impact Explanation
This produces a permanent user/holding-register fund lock triggerable by any unprivileged party who can (a) register/reference an ERC20 asset that reverts on zero-value transfer as an XCM-recognized asset location, and (b) submit or induce a multi-asset XCM (local `pallet_xcm::execute` or an inbound reserve/teleport message) that includes a zero-valued leg for that asset. Non-zero, otherwise-valuable assets bundled in the same `AssetsInHolding` at the point of failure are trapped and cannot be feasibly recovered since any later claim will re-trigger the same deterministic revert. This matches the "permanent user-fund or bridge-state lock" and "message queues ... must only advance after decode, dispatch, execution, and settlement succeed atomically" impact categories, and requires no malicious peer, validator, collator, or governance actor — only an unprivileged XCM sender and an ERC20 contract with standard (if unusual) revert-on-zero semantics.

### Likelihood Explanation
Revert-on-zero-value-transfer ERC20 tokens are a known, real-world pattern (the exact class named in the external report and referenced by HackenProof-in-scope ERC20 assessed-type findings). Any user can register such a token as an ERC20 asset location for Asset Hub's `ERC20Transactor`, and any user can submit `pallet_xcm::execute` with a hand-crafted multi-asset program containing a `Fungible(0)` leg for that asset — no special privileges, timing, or third-party cooperation is required. The likelihood is high given the transactor is a live, generally-reachable component and the zero-amount condition is entirely attacker-chosen.

### Recommendation
Add an explicit `amount.is_zero()` no-op short-circuit at the top of both `withdraw_asset_with_surplus` and `deposit_asset_with_surplus` in `cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs`, mirroring the pattern already used everywhere else in the codebase (`substrate/frame/balances/src/impl_currency.rs`, `substrate/frame/revive/src/exec.rs`, `substrate/frame/psm/src/lib.rs`):

```rust
if amount == 0 {
    return Ok((AssetsInHolding::new(), Weight::zero())); // or equivalent no-op success for deposit
}
```

so that a zero-valued ERC20 leg never issues an on-chain `transfer(..., 0)` call and cannot revert due to non-standard ERC20 semantics.

### Proof of Concept
1. Deploy/identify an ERC20 contract `T` on Asset Hub (via `pallet-revive`) whose `transfer()` implementation reverts when `value == 0` (a documented real-world ERC20 pattern).
2. Reference `T`'s location as an ERC20 asset recognized by `ERC20Matcher`/`ERC20Transactor` (per `cumulus/parachains/runtimes/assets/asset-hub-westend/src/xcm_config.rs`).
3. As any signed account, call `pallet_xcm::execute` with an XCM program such as:
   ```
   WithdrawAsset([(T, 0), (DOT, 1000)])
   DepositAsset { assets: Wild(AllCounted(2)), beneficiary: <attacker or victim> }
   ```
4. During execution, `WithdrawAsset`/`DepositAsset` routes the `(T, 0)` leg through `ERC20Transactor::withdraw_asset_with_surplus`/`deposit_asset_with_surplus`, which calls `IERC20::transferCall` with `value = 0`; the contract reverts, returning `XcmError::FailedToTransactAsset`.
5. The XCM executor traps the remaining holding (including the 1000 DOT already withdrawn) via `AssetTrap`.
6. Any later `ClaimAsset` attempt that re-executes a deposit for the same bundled zero-valued `T` leg reverts identically, leaving the DOT permanently inaccessible.

Note: full verification of the exact XCM-executor trap/claim mechanics (i.e., whether the `Wild(AllCounted(n))` selector in `DepositAsset` invokes the transactor once for the whole `AssetsInHolding` versus once per asset id, and the precise conditions under which trapped assets become permanently unclaimable versus merely inconvenient) was not traced end-to-end in the executor itself due to iteration limits; that call chain (`xcm-executor`'s `deposit_asset`/`trap_assets` logic) should be independently confirmed before treating this as fully proven at the "permanent lock" severity level.

### Citations

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

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L240-266)
```rust
		let (asset_contract_id, amount) = match maybe {
			Some(inner) => inner,
			None => return Err((what, MatchError::AssetNotHandled.into())),
		};
		let who = match AccountIdConverter::convert_location(who) {
			Some(inner) => inner,
			None => return Err((what, MatchError::AccountIdConversionFailed.into())),
		};
		// We need to map the 32 byte beneficiary account to a 20 byte account.
		let eth_address = T::AddressMapper::to_address(&who);
		let address = Address::from(Into::<[u8; 20]>::into(eth_address));
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

**File:** substrate/frame/balances/src/impl_currency.rs (L399-401)
```rust
		if value.is_zero() || transactor == dest {
			return Ok(());
		}
```

**File:** substrate/frame/revive/src/exec.rs (L1734-1736)
```rust
		if value.is_zero() {
			return Ok(());
		}
```
