## Finding: Unbacked ERC-20 asset credit trusted without on-chain balance verification in XCM ERC20 Transactor

### Title
Unbacked-mint via `ERC20Transactor` trusting a malicious contract's `transfer()` return value as proof of value movement - (File: `cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs`)

### Summary
The Fractional V2 report's core broken invariant is: **a value-moving primitive is only backed by an interaction with a caller-supplied, arbitrary-code contract, and the protocol trusts that interaction's outcome to update its own internal accounting** (there, `IFERC1155.setApprovalFor`/state flags; here, `IERC20.transfer`/`AssetsInHolding`). The local analog is `ERC20Transactor`, the `TransactAsset` implementation used by the XCM executor to move "ERC-20" assets (any `pallet-revive` contract address resolvable via `Matcher::matches_fungibles`) in/out of XCM's holding register.

### Finding Description
`ERC20Transactor::withdraw_asset_with_surplus` performs an `IERC20.transfer` call into the asset's contract address via `pallet_revive::Pallet::<T>::bare_call` and, if the call does not revert and the ABI-decoded boolean return is `true`, unconditionally mints an `AssetsInHolding` credit for the requested `amount`: [1](#0-0) 

The type used for this credit is explicitly documented as **not enforcing real balance constraints**: [2](#0-1) 

Because the "asset id" for this transactor is simply a contract address (any `pallet-revive` contract can be targeted, since the caller/attacker fully controls what asset location resolves to via the pallet's `Matcher`), an attacker can deploy a contract that implements the `IERC20` ABI but whose `transfer()` function unconditionally returns `true` without moving any real value or checking real balances at all. Since `ERC20Transactor` never independently verifies a `balanceOf` delta on the checking account, it will credit `AssetsInHolding` with an arbitrary, attacker-chosen amount that has **zero real backing**. The corresponding `deposit_asset_with_surplus` path has the mirrored trust assumption: [3](#0-2) 

This fake credit is a first-class `AssetsInHolding` value inside the XCM executor's holding register, the same register instructions like `ExchangeAsset`/`DepositAsset` operate on when swapping through `pallet-asset-conversion` pools or moving assets to a beneficiary. Once minted into holding, this bogus credit is fungible with real economic value for any instruction that treats holding contents at face value (e.g., swapping it via an asset-conversion pool that pairs the malicious "ERC20" with DOT/USDT, or depositing it to another account entangled with a pool/vault that accepts this asset id as collateral).

### Impact Explanation
This matches the Polkadot SDK Impact Gate's "theft or unbacked mint or unlock": an unprivileged attacker who deploys an ordinary `pallet-revive` contract (no relayer, validator, governance, or admin role needed) can conjure `AssetsInHolding` credit for an asset id with no real value behind it, purely because the runtime trusts an untrusted contract's self-reported success return value instead of verifying an actual balance movement. Any onward use of that fake credit (liquidity-pool swaps, deposits accepted at face value by another pallet/precompile) converts fictitious backing into extraction of real, pooled value — a direct asset-conservation violation.

### Likelihood Explanation
High. No privileged role or malicious peer/validator/relayer is required — only a standard `pallet-revive` contract deployment (permissionless) and a `pallet_xcm::execute`/XCM-precompile call (also permissionless, reachable from a signed EOA or another contract via the `IXcm` precompile shown in `polkadot/xcm/pallet-xcm/precompiles/src/lib.rs`). The transactor's own comment confirms the design assumption ("actual balance constraints are enforced by the ERC20 smart contract itself") is not paired with any independent verification by the transactor, so nothing currently guards against a non-conforming/malicious contract at this call site.

### Recommendation
`ERC20Transactor::withdraw_asset_with_surplus`/`deposit_asset_with_surplus` should not trust the boolean return of `transfer()` alone. Verify the actual balance delta on the checking account (read `balanceOf` before and after the call) and only mint/burn `AssetsInHolding` credit equal to the observed real delta, following Checks-Effects-Interactions: read pre-state, perform the interaction, then re-check post-state before crediting holding.

### Proof of Concept
1. Deploy a `pallet-revive` contract `FakeERC20` implementing `IERC20` whose `transfer(address,uint256)` always `return true` and performs no storage accounting.
2. Ensure `Matcher::matches_fungibles` in the configured `ERC20Transactor` resolves an XCM `Asset` location to `FakeERC20`'s address (standard AccountKey20-style asset matching used for ERC-20 XCM assets on Asset Hub, per `withdraw_and_deposit_erc20s` test flow in `cumulus/parachains/runtimes/assets/asset-hub-westend/tests/tests.rs`).
3. Call `pallet_xcm::execute` (or the `IXcm.execute` precompile) with `WithdrawAsset((FakeERC20Location, HUGE_AMOUNT))` — `withdraw_asset_with_surplus` calls `transfer(checking_account, HUGE_AMOUNT)` on `FakeERC20`, gets `true`, and credits `HUGE_AMOUNT` of `Erc20Credit` into XCM holding with no real balance ever moved.
4. Chain a `BuyExecution`/`ExchangeAsset` instruction against a `pallet-asset-conversion` pool seeded with a small amount of real DOT paired against `FakeERC20`, swapping the fabricated `HUGE_AMOUNT` credit for real DOT, draining the pool.

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

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L185-203)
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
```

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L225-266)
```rust
	fn deposit_asset_with_surplus(
		what: AssetsInHolding,
		who: &Location,
		_context: Option<&XcmContext>,
	) -> Result<Weight, (AssetsInHolding, XcmError)> {
		tracing::trace!(
			target: "xcm::transactor::erc20::deposit",
			?what, ?who,
		);
		defensive_assert!(what.len() == 1, "Trying to deposit more than one asset!");
		// Check we handle this asset.
		let maybe = what
			.fungible_assets_iter()
			.next()
			.and_then(|asset| Matcher::matches_fungibles(&asset).ok());
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
