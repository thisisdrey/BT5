## Analysis

The Sherlock bug's core broken invariant: a value-transfer amount is *recorded into an accounting ledger* as if fully received, while the underlying token transfer mechanism can silently deliver less than that amount (fee-on-transfer / non-standard ERC20 behavior). The ledger and the real token balance then diverge, and later payouts settle against the (wrong) ledger value instead of what was actually received.

The same faulty-assumption pattern exists in `polkadot-sdk`'s `ERC20Transactor`, which XCM uses to move arbitrary Solidity ERC20 tokens (deployed on `pallet-revive`) into/out of a shared `TransfersCheckingAccount`.

### Title
XCM `ERC20Transactor` credits full requested amount into `AssetsInHolding` without verifying actual tokens received by the checking account, allowing fee-on-transfer/non-standard ERC20 tokens to drain the shared checking account - (File: `cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs`)

### Summary
`ERC20Transactor::withdraw_asset_with_surplus` calls an arbitrary ERC20 contract's `transfer(checking_account, amount)` and, on a `true` return, unconditionally mints an `Erc20Credit(amount)` into the XCM `AssetsInHolding` register [1](#0-0) . It never checks the ERC20 contract's `balanceOf(checking_account)` delta to confirm `amount` was actually received. `deposit_asset_with_surplus` later moves the same recorded `amount` out of the shared checking account to a beneficiary via another `transfer` call [2](#0-1) .

### Finding Description
`pallet-revive` supports arbitrary EVM bytecode/Solidity contracts (`AllowEVMBytecode` is enabled on Asset Hub) [3](#0-2) , and `ERC20Transactor` is the generic `TransactAsset` implementation used to move any matched ERC20 token through XCM by calling `IERC20::transfer` on the token contract itself: "the actual balance constraints are enforced by the ERC20 smart contract itself rather than the runtime" [4](#0-3) .

On withdraw, the code does:
```
let data = IERC20::transferCall { to: checking_address, value: EU256::from(amount) }.abi_encode();
... bare_call(...)
if is_success { Ok((AssetsInHolding::new_from_fungible_credit(what.id.clone(), Box::new(Erc20Credit(amount))), surplus)) }
``` [5](#0-4) 

This mirrors exactly the vulnerable pattern in the Sherlock report: the contract's boolean "success" return is trusted as proof that the *full* `amount` moved, but ERC20 `transfer` returning `true` says nothing about how much of `amount` the recipient (`checking_address`) actually received. Any ERC20 implementing a fee-on-transfer, rebasing, or deflationary-burn mechanism (all legal per ERC20, and explicitly a "weird ERC20" pattern the original report was accepted for) will deliver `amount - fee` to `checking_address` while `Erc20Credit(amount)` — the full amount — is placed into the XCM holding register.

Because `TransfersCheckingAccount` is a single shared account used across *all* ERC20 transfers routed through this transactor, this creates a systemic ledger/reality mismatch: every fee-on-transfer withdrawal under-funds the checking account relative to what `AssetsInHolding` claims has been received. Subsequent `deposit_asset_with_surplus` calls for *other* unrelated XCM programs will draw down the *same* shared checking account balance for their own (unrelated) full `amount`, silently consuming real balance that was contributed by other users' legitimate, non-deflationary transfers. This is not merely "recipient gets short-changed" (as in the original report) — it's "the recorded holding-register amount is authoritative for subsequent DepositAsset instructions," so an attacker can deploy a custom fee-on-transfer ERC20 (an unprivileged, permissionless action — anyone can register a foreign asset contract for XCM use), withdraw through this transactor to intentionally inflate the ledger vs. real balance, and either cause other users' deposits to fail (fund lock, since the checking account eventually runs short) or, if timed against other in-flight transfers of the same token, benefit from real balance contributed by other users' non-deflationary transfers.

### Impact Explanation
This breaks the "conserve value, settle exactly once to the rightful beneficiary and amount" invariant for contract-held value moved via XCM: the on-chain ERC20 balance of the shared `TransfersCheckingAccount` diverges from what the XCM holding register believes is available. This can (a) permanently lock user funds when `deposit_asset_with_surplus`'s `transfer` reverts because the checking account balance is insufficient for the over-credited state, or (b) allow one user's fee-on-transfer withdrawal to be effectively subsidized by balance contributed by unrelated legitimate transfers sharing the same account, i.e. cross-user fund commingling/loss without direct beneficiary consent.

### Likelihood Explanation
No privileged actor, relayer, validator, or admin is required. Any user can deploy an ERC20 contract with fee-on-transfer semantics on `pallet-revive` and register/use it as an asset matched by `Matcher::matches_fungibles`, then execute an XCM program (e.g. via `pallet-xcm::execute` or a reserve transfer) that triggers `withdraw_asset_with_surplus`/`deposit_asset_with_surplus`. The existing guard is only "did the contract call return `true`", which a fee-on-transfer ERC20 satisfies while still delivering less than `amount` — the guard does not stop this path.

### Recommendation
In `withdraw_asset_with_surplus`, read `balanceOf(checking_address)` before and after the `transfer` call and use the actual delta (not the requested `amount`) when constructing `Erc20Credit`. Symmetrically, in `deposit_asset_with_surplus`, only transfer/credit what was actually confirmed received, and propagate any shortfall back into `AssetsInHolding` (as a trapped/returned asset) rather than assuming full-amount success.

### Proof of Concept
1. Deploy an ERC20 contract on `pallet-revive` whose `transfer` implementation burns/deducts e.g. 5% of `value` from the recipient's credited balance while still returning `true`.
2. Register/match this contract as an asset usable by `ERC20Transactor` (via the configured `Matcher`).
3. Execute an XCM program that performs `WithdrawAsset` for `amount` of this token from the attacker's own account — `withdraw_asset_with_surplus` calls `transfer(checking_address, amount)`, the contract returns `true`, but `checking_address`'s real ERC20 balance only increases by `amount * 0.95`. The XCM holding register nonetheless records `Erc20Credit(amount)` (full amount).
4. Repeat with other unrelated legitimate (non-fee) ERC20 withdrawals/deposits sharing the same `TransfersCheckingAccount`; observe that cumulative real balance in `checking_address` falls behind the sum of amounts recorded as "credited" across processed XCM programs, causing later `deposit_asset_with_surplus` transfers to revert (fund lock) or to be paid out of balance that was actually contributed by other users' transfers.

### Citations

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L73-89)
```rust
/// A minimal imbalance tracking type that holds an ERC20 token amount.
///
/// This type implements the necessary imbalance accounting traits but does not perform
/// runtime-level balance enforcement. It's used to track ERC20 token amounts within XCM
/// asset holdings, where the actual balance constraints are enforced by the ERC20 smart
/// contract itself rather than the runtime.
struct Erc20Credit(u128);
impl UnsafeConstructorDestructor<u128> for Erc20Credit {
	fn unsafe_clone(&self) -> Box<dyn ImbalanceAccounting<u128>> {
		Box::new(Erc20Credit(self.0))
	}
	fn forget_imbalance(&mut self) -> u128 {
		let amount = self.0;
		self.0 = 0;
		amount
	}
}
```

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

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/src/lib.rs (L1372-1420)
```rust
impl pallet_revive::Config for Runtime {
	type Time = Timestamp;
	type Balance = Balance;
	type Currency = Balances;
	type RuntimeEvent = RuntimeEvent;
	type RuntimeCall = RuntimeCall;
	type RuntimeOrigin = RuntimeOrigin;
	type DepositPerItem = DepositPerItem;
	type DepositPerChildTrieItem = DepositPerChildTrieItem;
	type DepositPerByte = DepositPerByte;
	type WeightInfo = weights::pallet_revive::WeightInfo<Self>;
	type Precompiles = (
		ERC20<Self, InlineIdConfig<{ TRUST_BACKED_ASSETS_PRECOMPILE }>, TrustBackedAssetsInstance>,
		ERC20<Self, InlineIdConfig<{ POOL_ASSETS_PRECOMPILE }>, PoolAssetsInstance>,
		ERC20<
			Self,
			ForeignIdConfig<{ FOREIGN_ASSETS_PRECOMPILE }, Self, ForeignAssetsInstance>,
			ForeignAssetsInstance,
		>,
		XcmPrecompile<Self>,
		pallet_asset_conversion_precompiles::AssetConversion<{ ASSET_CONVERSION_PRECOMPILE }, Self>,
		VestingPrecompile<Self>,
	);
	type AddressMapper = pallet_revive::AccountId32Mapper<Self>;
	type RuntimeMemory = ConstU32<{ 128 * 1024 * 1024 }>;
	type PVFMemory = ConstU32<{ 512 * 1024 * 1024 }>;
	type AllowEVMBytecode = ConstBool<true>;
	type UploadOrigin = EnsureSigned<Self::AccountId>;
	type InstantiateOrigin = EnsureSigned<Self::AccountId>;
	type RuntimeHoldReason = RuntimeHoldReason;
	type CodeHashLockupDepositPercent = CodeHashLockupDepositPercent;
	type ChainId = ConstU64<420_420_421>;
	type NativeToEthRatio = ConstU32<1_000_000>; // 10^(18 - 12) Eth is 10^18, Native is 10^12.
	type FindAuthor = <Runtime as pallet_authorship::Config>::FindAuthor;
	type FeeInfo = pallet_revive::evm::fees::Info<Address, Signature, EthExtraImpl>;
	type MaxEthExtrinsicWeight = MaxEthExtrinsicWeight;
	type DebugEnabled = ConstBool<{ cfg!(revive_debug) }>;
	type AutoMap = ConstBool<true>;
	type GasScale = ConstU32<1000>;
	type OnBurn = Dap;
	type Deposit = pallet_revive::PGasDeposit<
		Runtime,
		Assets,
		AssetsHolder,
		AssetsFreezer,
		PGASAssetId,
		PGasRefundPercent,
	>;
}
```
