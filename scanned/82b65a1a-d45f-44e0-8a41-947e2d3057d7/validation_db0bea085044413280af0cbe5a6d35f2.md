## Title
ERC20 asset transactor credits/debits XCM holding by nominal `amount` instead of measured `balanceOf()` delta, allowing fee‑on‑transfer/rebasing tokens to desync the shared checking account and lock user funds - (File: `cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs`)

### Summary
`ERC20Transactor::withdraw_asset_with_surplus` and `ERC20Transactor::deposit_asset_with_surplus` move funds by calling the target ERC20 contract's `transfer()` and, upon a `true` boolean return, unconditionally credit/debit the XCM holding register with the *requested* `amount` rather than the amount actually received by (or removed from) the shared `TransfersCheckingAccount`. This is exactly the broken invariant identified in the VTVL report: trusting a nominal transfer amount instead of measuring the pre/post `balanceOf()` delta.

### Finding Description
`withdraw_asset_with_surplus` performs: [1](#0-0) 

It calls `IERC20::transferCall{ to: checking_address, value: amount }` on the arbitrary ERC20 contract identified by `asset_id` (matched via `Matcher::matches_fungibles`), and if the ABI-decoded return value is `true`, it constructs `AssetsInHolding::new_from_fungible_credit(..., Erc20Credit(amount))` — crediting the XCM holding register with the full nominal `amount`, regardless of how many tokens the `TransfersCheckingAccount` actually received.

Symmetrically, `deposit_asset_with_surplus` debits the checking account by transferring `amount` out to the beneficiary and treats a `true` return as full success: [2](#0-1) 

Neither function reads `balanceOf(TransfersCheckingAccount)` before and after the transfer to compute the actual delta, unlike the mitigation pattern described by the report's judge (`balBefore`/`balAfter`/`actualBalChange`). For any ERC20 contract that is fee-on-transfer, deflationary, or rebasing — which this generic transactor does not filter out, since `Matcher::matches_fungibles` only maps an asset `Location` to a contract address and amount, with no constraint that the token must be a "vanilla" fixed-supply ERC20 — the actual tokens moved into/out of `TransfersCheckingAccount` diverge from the nominal `amount` recorded in the XCM `AssetsInHolding`.

Because `TransfersCheckingAccount` is a single shared account (a `Get<AccountId>` parameter of the transactor) used for every `withdraw`/`deposit` XCM operation involving assets matched by this transactor, a shortfall introduced by one fee-on-transfer withdrawal understates the real balance available to satisfy subsequent, unrelated deposits for other users of the same registered ERC20 asset.

### Impact Explanation
This falls under "theft or unbacked mint or unlock" / "permanent user-fund or bridge-state lock": the XCM holding register can carry more "credited" ERC20 balance than the checking account actually holds. When a later, unrelated user's deposit is processed, `deposit_asset_with_surplus` will attempt to `transfer(amount)` out of `TransfersCheckingAccount`, which can fail (`return_value` is `false` or the call reverts) because the real on-chain balance is insufficient — this is precisely the "insufficient balance at withdraw time" DoS/fund-lock pattern from the source report. The affected XCM assets end up trapped (via the executor's asset-trap mechanism) or the whole XCM operation fails, causing loss/lock of funds for a user who did nothing wrong, using shared runtime infrastructure they don't control.

### Likelihood Explanation
This requires only that an unprivileged actor register/hold (or that the runtime configuration exposes) a fee-on-transfer or rebasing ERC20 contract behind this transactor's `Matcher`, then perform a normal, permissionless XCM transfer using that asset — no malicious relayer, validator, governance action, or key compromise is needed. Since `ERC20Transactor` is explicitly built to handle "ERC20 tokens" generically via `pallet-revive`/EVM contracts rather than a fixed, audited set of standard tokens, and the code path never verifies actual balance deltas, the precondition (a non-standard ERC20 being transactable) is realistic for any deployment that wires arbitrary/foreign ERC20 contracts into this transactor.

### Recommendation
In both `withdraw_asset_with_surplus` and `deposit_asset_with_surplus`, read `balanceOf(TransfersCheckingAccount)` (or the destination address) immediately before and after the `transfer()` call, and use the measured delta — not the requested `amount` — both for the value credited into `Erc20Credit`/`AssetsInHolding` and for validating that the operation fully succeeded. If the measured delta is less than the requested `amount`, the operation should fail atomically (or the AssetsInHolding entry should reflect only the delta actually moved) so that the shared `TransfersCheckingAccount`'s recorded balance never diverges from its real on-chain balance.

### Proof of Concept
1. Configure `ERC20Transactor` (via its `Matcher`) to accept an ERC20 contract that deducts a transfer fee (or rebases down) on `transfer()`, e.g. `transfer(to, amount)` moves `amount * 99 / 100` to `to`.
2. User A executes an XCM `WithdrawAsset` for `amount = 1000` of this token from their account. `withdraw_asset_with_surplus` calls `transfer(checking_account, 1000)`, which actually delivers `990` tokens to `TransfersCheckingAccount`, but the ERC20 `transfer` call still returns `true`.
3. The code credits `Erc20Credit(1000)` into the XCM holding register — 10 tokens more than `TransfersCheckingAccount` actually received.
4. User B later performs an unrelated XCM `DepositAsset` for the same token, requesting `1000` tokens from the (now under-collateralized) `TransfersCheckingAccount`. `deposit_asset_with_surplus` calls `transfer(beneficiary, 1000)`, which reverts or returns `false` because `TransfersCheckingAccount`'s real balance (`990` plus whatever other legitimate credits/debits occurred) is insufficient.
5. User B's XCM deposit fails (`XcmError::FailedToTransactAsset`), their assets are trapped/lost in the destination chain's holding register, while User A's original tokens remain stuck inside the shared checking account — reproducing the VTVL "insufficient balance at withdraw → fund lock" scenario inside Polkadot's XCM ERC20 transactor.

### Citations

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L166-203)
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
				if is_success {
					tracing::trace!(target: "xcm::transactor::erc20::withdraw", "ERC20 contract was successful");
					Ok((
						AssetsInHolding::new_from_fungible_credit(
							what.id.clone(),
							Box::new(Erc20Credit(amount)),
						),
						surplus,
					))
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
