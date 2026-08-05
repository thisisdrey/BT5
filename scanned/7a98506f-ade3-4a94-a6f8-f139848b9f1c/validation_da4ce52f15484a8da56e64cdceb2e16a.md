# Finding

### Title
ERC20 XCM asset transactor never verifies the "asset" is a deployed contract before treating transfer calls as authoritative settlement - (File: `cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs`)

### Summary
The reported bug is a single broken invariant: a `deposit()`-style balance-crediting function trusts that a low-level transfer call ("succeeded, didn't revert") proves real value moved, without ever checking that the target token actually has deployed code. `SafeTransferLib` never checked "has code"; the caller (`Settlement.deposit`) never checked it either, so balances could be credited for tokens that don't exist yet.

The same missing check exists in the `Erc20AssetTransactor` used by `pallet-revive`-enabled Asset Hub runtimes (`cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs`). XCM lets any account reference an "ERC20 asset" purely as `Location { AccountKey20 { key, network } }` — an arbitrary 20-byte value chosen by the message author, with no on-chain registration step and no `asset_exists()`-style guard. The transactor's `withdraw_asset_with_surplus` / `deposit_asset_with_surplus` treat a `pallet_revive::bare_call` to that address as authoritative proof of a real ERC20 `transfer`, but `pallet-revive` explicitly defines (see `prdoc/stable2412/pr_5664.prdoc`, "Calling an address without associated code is a balance transfer") that calling an address with no code is *not* a reverted/failed call — it is silently treated as a **plain native balance transfer of the call's `value` parameter**, with the call's `input_data` (the encoded `IERC20::transferCall`) completely ignored.

### Finding Description
`Erc20AssetTransactor::withdraw_asset_with_surplus` and `deposit_asset_with_surplus` (`cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs:150-307`) match any `Asset` whose `AssetId` resolves to a 20-byte key (`Matcher::matches_fungibles`) and then invoke: [1](#0-0) 

with `dest = asset_id` — an address supplied entirely by the XCM message's own `Asset.id` field, never checked for `AccountInfo::is_contract` / existence before use. `pallet_revive::exec.rs` and `lib.rs` confirm the semantics that a call to such a "codeless" address is deliberately converted into a plain balance transfer rather than an error: [2](#0-1) [3](#0-2) 

and the change is documented as intentional in the corresponding prdoc: [4](#0-3) 

Because the call's `value` parameter is hard-coded to `U256::zero()` in the transactor, invoking a non-existent "ERC20 asset" address always succeeds trivially as a zero-value transfer — it does not revert and the `IERC20::transferCall` payload is discarded rather than executed. The transactor then treats "did not revert" as sufficient evidence that `amount` tokens were locked (`withdraw_asset_with_surplus`) or unlocked (`deposit_asset_with_surplus`) and mints a corresponding `AssetsInHolding` credit that the XCM executor subsequently deposits into a real beneficiary account via `DepositAsset`: [5](#0-4) 

Unlike `pallet-assets`, which gates every increase with `Asset::<T, I>::get(&id)` returning `UnknownAsset` if the id was never created (`substrate/frame/assets/src/functions.rs:143-146`), or `fungibles::Inspect::asset_exists` (`substrate/frame/support/src/traits/tokens/fungibles/regular.rs:121`), the ERC20 transactor has **no equivalent existence check** — it relies entirely on the outcome of a `bare_call`, and `pallet-revive`'s own documented fallback for codeless targets removes the one signal (a revert/failure) that would normally indicate "this token doesn't exist."

### Impact Explanation
An XCM program (from a user's own `pallet-xcm::execute`, or from a sibling/relay chain reserve-transfer path that is configured to trust the local `IsReserve` filter for this transactor) referencing an `Erc20` asset id that has not been deployed as a contract can cause the transactor to record a successful withdraw/deposit without any real value ever moving. This is a false-state-acceptance / unbacked-credit pattern directly analogous to the reported bug: the settlement ledger (here, the XCM holding register and any beneficiary account reached via `DepositAsset`) is updated as if `amount` units of a real asset were transferred, when in fact the "asset" is an address with no code and the transfer of `amount` never happened. If subsequently used with pools (`pallet_asset_conversion_precompiles::AssetConversion`, wired in the same runtime, `cumulus/parachains/runtimes/assets/asset-hub-westend/src/lib.rs:1372-1394`) or forwarded to another chain, this could be leveraged into a real economic loss for the chain or counterparties trusting the reported "ERC20" balance.

### Likelihood Explanation
Exploitation only requires an unprivileged user to submit an XCM program (locally via `PolkadotXcm::execute`, or the same construction embedded in a reserve-transfer) specifying an `Asset` with `id = Location(AccountKey20{ key: <any 20 bytes>, network })` where `key` is not yet an existing contract address. No privileged, governance, relayer, or validator role is needed — this is a pure public-entrypoint path (`pallet-xcm::execute`/`transfer_assets`) gated only by ordinary transaction fees.

### Recommendation
In `Erc20AssetTransactor::withdraw_asset_with_surplus` and `deposit_asset_with_surplus`, check `AccountInfo::<T>::is_contract(&asset_contract_id)` (or an equivalent "asset exists" predicate) before performing the `bare_call`, and reject the asset with `XcmError::AssetNotHandled`/`FailedToTransactAsset` if the target address has no code, mirroring the check `pallet-assets` performs via `Asset::<T, I>::get(&id)`/`asset_exists`.

### Proof of Concept
1. On an Asset Hub runtime configured with `Erc20AssetTransactor` and `pallet-revive` (e.g. `asset-hub-westend-runtime`), pick an arbitrary 20-byte value `K` that is not the address of any deployed contract.
2. Submit `PolkadotXcm::execute` with a program equivalent to:
   ```
   WithdrawAsset((AccountKey20 { key: K, network: None }, AMOUNT))
   DepositAsset { assets: All, beneficiary: <attacker account> }
   ```
   executed as `origin = attacker`.
3. `withdraw_asset_with_surplus` calls `bare_call(attacker, K, value=0, data=IERC20::transferCall{...})`; since `K` has no code, `pallet-revive` treats this as a zero-value balance transfer (per `is_simple_transfer`/`address_runs_no_code`) and returns success with no reversion, regardless of `AMOUNT` or `attacker`'s real holdings of the "asset" `K`.
4. The executor credits `AssetsInHolding` with `AMOUNT` of asset `K` and deposits it to the attacker's beneficiary location via the same transactor's `deposit_asset_with_surplus`, again succeeding trivially against the codeless address `K`.
5. The XCM outcome reports success and any downstream query (or a subsequent transfer of the same "asset" `K` to another chain/pool) treats the attacker as holding `AMOUNT` of asset `K`, without any real token contract ever having existed or moved value — a direct structural analog of crediting a `deposit()` balance for a token that was never deployed.

### Citations

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L168-181)
```rust
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

**File:** substrate/frame/revive/src/lib.rs (L2110-2125)
```rust
	/// Returns true when `tx` is a plain value transfer that executes no code at its destination.
	pub(crate) fn is_simple_transfer(tx: &GenericTransaction) -> bool {
		tx.to
			.map(|to| tx.has_simple_transfer_fields() && Self::address_runs_no_code(&to))
			.unwrap_or(false)
	}

	/// Returns true when a value transfer can target `address` without triggering any code
	/// execution: it is neither the runtime pallets address, a precompile, nor a contract.
	fn address_runs_no_code(address: &H160) -> bool {
		// TODO(eip-7702): also reject delegated (authorized) destinations once EIP-7702
		// delegations land, since a transfer to one executes the delegate's code.
		*address != RUNTIME_PALLETS_ADDR &&
			!exec::is_precompile::<T, ContractBlob<T>>(address) &&
			!<AccountInfo<T>>::is_contract(address)
	}
```

**File:** substrate/frame/revive/src/exec/tests.rs (L1564-1593)
```rust
#[test]
fn recursive_call_during_constructor_is_balance_transfer() {
	let code = MockLoader::insert(Constructor, |ctx, _| {
		let account_id = ctx.ext.account_id().clone();
		let addr =
			<<Test as Config>::AddressMapper as AddressMapper<Test>>::to_address(&account_id);
		let balance = ctx.ext.balance();

		// Calling ourselves during the constructor will trigger a balance
		// transfer since no contract exist yet.
		assert_ok!(ctx.ext.call(
			&Default::default(),
			&addr,
			(balance - 1).into(),
			vec![],
			ReentrancyProtection::AllowReentry,
			false
		));

		// Should also work with call data set as it is ignored when no
		// contract is deployed.
		assert_ok!(ctx.ext.call(
			&Default::default(),
			&addr,
			1u32.into(),
			vec![1, 2, 3, 4],
			ReentrancyProtection::AllowReentry,
			false
		));
		exec_success()
```

**File:** prdoc/stable2412/pr_5664.prdoc (L1-11)
```text
title: Calling an address without associated code is a balance transfer

doc:
  - audience: Runtime Dev
    description: |
     This makes pallet_revive behave like EVM where a balance transfer
     is just a call to a plain wallet.

crates:
  - name: pallet-revive
    bump: patch
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
