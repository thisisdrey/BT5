### Title
Unbacked ERC20 credit minted into XCM holding due to trusting arbitrary contract return values without verifying real balance movement - ([File: cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs])

### Summary
`ERC20Transactor::withdraw_asset_with_surplus` in `cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs` (lines 150-216) mints an `Erc20Credit(amount)` into the XCM holding register purely on the basis of the boolean value returned by an externally-supplied `IERC20::transfer` call, without ever verifying that the ERC20 contract's real balance in the `TransfersCheckingAccount` actually increased by `amount`. This is the exact class of bug flagged in the external report: interacting with an external/potentially malicious token without independently checking that the claimed value transfer really happened, instead of trusting only the call's return status.

### Finding Description
The location matched by `Matcher::matches_fungibles(what)` (an `AccountKey20` location) is fully attacker-controlled: any XCM sender can specify an arbitrary `H160` address as the "ERC20 token" for `WithdrawAsset`/`DepositAsset` instructions via the public `PolkadotXcm::execute` entrypoint. `ERC20Transactor` is wired into `AssetTransactors` on AssetHub-Westend [1](#0-0) .

In `withdraw_asset_with_surplus`, the transactor calls the attacker-chosen contract's `transfer(checking_address, amount)` via `pallet_revive::Pallet::<T>::bare_call`, and if the call does not revert and the ABI-decoded return value is `true`, it unconditionally creates `AssetsInHolding::new_from_fungible_credit(what.id.clone(), Box::new(Erc20Credit(amount)))`: [2](#0-1) 

There is no read-back of the checking account's actual token balance before/after the call to confirm `amount` was really received. Because the "token" contract address is attacker-supplied, the attacker can deploy a trivial contract whose `transfer()` function unconditionally returns `true` (or returns `true` while moving `0` or an arbitrary lesser amount) regardless of real balance state — the classic "return value cannot be trusted" issue that `SafeERC20`-style checks are meant to guard against, except here the gap is not the *decoding* of the return value (which the code does validate strictly, see the passing `smart_contract_does_not_return_bool_fails` test) but the *lack of an independent balance-delta check* against the actual contract state.

The `Erc20Credit` produced is then a fully virtual, protocol-trusted `u128` amount inside `AssetsInHolding` that is indistinguishable, for the rest of XCM program execution, from a real, backed fungible credit. It can be used for anything the holding register supports within that program: `PayFees`, `DepositAsset` to a beneficiary, `ExchangeAsset` against a pool, or `InitiateTransfer`/reserve transfers to other chains — all without the checking account ever holding the real value the credit claims to represent.

The symmetric `deposit_asset_with_surplus` path has the same weakness: it calls `transfer(beneficiary, amount)` from the checking account and trusts the boolean return, again without verifying the checking account's real balance decreased by `amount`: [3](#0-2) 

Since the attacker fully controls the malicious contract's logic (including in the "deposit" leg), the contract can simply always return `true`, so `deposit_asset_with_surplus` will report success even though the checking account's real balance for that contract never held (or lost) any tokens.

### Impact Explanation
This breaks the "assets must conserve value" invariant for a public, unprivileged entrypoint (`pallet_xcm::execute`/dispatch of arbitrary XCM programs). An attacker needs no elevated origin, no relayer, no validator, and no governance action — they only need to deploy a contract on the same chain (via `pallet_revive`) and issue a self-authorized XCM program. The virtual, unbacked `Erc20Credit` amount is treated by the executor identically to a real, checking-account-backed fungible asset for the remainder of the program, which can allow the attacker to:
- Pay for delivery/execution fees with worthless credit that the fee handler treats as real value.
- Deposit "value" to a beneficiary that downstream logic (e.g. accounting, reward computation, or DEX interactions if this asset class is ever paired in a pool) treats as genuine backing.
- Chain the fake credit into further XCM instructions (`InitiateTransfer`, `ExchangeAsset`) that could extract real assets from liquidity or fee pools if those subsystems accept the XCM-reported deposit as proof of value received.

This matches the "theft/unbacked mint" and "balances must conserve value and settle exactly once to the rightful beneficiary and amount" impact categories in the Polkadot SDK Impact Gate.

### Likelihood Explanation
High likelihood of triggering the underlying flaw (any user can deploy a trivial malicious contract and issue `PolkadotXcm::execute` specifying that contract's address as the ERC20 asset location — no special conditions, peers, or privileged actors required). The severity of downstream damage depends on what else consumes the resulting virtual holding within the same or a forwarded XCM program (fees, deposits, reserve transfers, or exchanges); this is a real, provable gap in the transactor's own invariant enforcement, independent of any specific downstream consumer.

### Recommendation
In `withdraw_asset_with_surplus`, query the checking account's actual on-chain ERC20 balance for the asset contract immediately before and after the `bare_call`, and only mint `Erc20Credit` for the amount actually observed to have moved (min of claimed `amount` and the real balance delta), rather than trusting the boolean return value alone. Apply the same real-balance-delta verification symmetrically in `deposit_asset_with_surplus` before considering the deposit successful. This restores the "SafeERC20" principle of verifying real effects rather than trusting external contract return values for potentially adversarial tokens.

### Proof of Concept
1. Attacker deploys, via `pallet_revive`, a minimal Solidity-like contract `FakeERC20` whose `transfer(address,uint256)` always returns `true` (ABI-encoded `true`) and performs no internal balance accounting/storage writes (or moves `0` tokens).
2. Attacker (unprivileged, signed origin) submits `PolkadotXcm::execute` with an XCM program:
   - `WithdrawAsset` specifying `(AccountKey20 { key: FakeERC20_address }, amount)` — this invokes `ERC20Transactor::withdraw_asset_with_surplus`, which calls `FakeERC20.transfer(checking_address, amount)`, gets `true` back, and creates `Erc20Credit(amount)` in holding — with zero real balance change in `TransfersCheckingAccount`.
3. The program continues to use this manufactured `amount` credit, e.g. `DepositAsset` to a beneficiary account (attacker-controlled or otherwise), which invokes `deposit_asset_with_surplus`, calling `FakeERC20.transfer(beneficiary, amount)` from the checking account — again returning `true` unconditionally, with no real tokens ever having existed.
4. Result: the XCM program completes successfully, and any downstream logic that observed the `DepositAsset`/holding state (fee accounting, forwarding to another chain via reserve transfer, or pairing with a real asset in a liquidity pool) treats `amount` of "ERC20 token FakeERC20" as legitimately received/backed value, when in reality no value ever moved through the checking account. This demonstrates the unbacked-credit primitive; the ultimate extraction of real value depends on which other pallet/config consumes the resulting deposit event or holding as proof of backing.

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
