### Title
ERC20Transactor mints fictitious XCM asset holdings by trusting a self-deployed contract's `transfer()` return value without verifying it is a genuine registered ERC20 asset - (File: cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs)

### Summary
The external report shows `LendingPool.redeem()` blindly trusting an attacker-supplied `AToken` contract's `balanceOf`/conversion return values to compute how much real liquidity to release, with no check that the contract is the legitimate, registered AToken for a reserve. The same broken invariant — "trust a caller-chosen contract's return value as if it represented real, backed value, without checking it against a canonical registry" — exists in `ERC20Transactor::withdraw_asset_with_surplus` / `deposit_asset_with_surplus` [1](#0-0) , whose `Matcher` (`ERC20Matcher`) derives the target `H160` contract address directly from the caller-supplied XCM `Location`'s `AccountKey20` bytes with no registry check [2](#0-1) .

### Finding Description
`ERC20Matcher` is defined as:
```rust
pub type ERC20Matcher =
    MatchedConvertedConcreteId<H160, u128, IsLocalAccountKey20, AccountKey20ToH160, TryConvertInto>;
```
`AccountKey20ToH160::convert` accepts *any* `Location` of the shape `(0, [AccountKey20{key}])` and returns `key` directly as the asset's contract address [3](#0-2) . There is no check that `key` corresponds to a real, registered, bridged ERC20 token — any 20-byte address the caller writes into the XCM `Asset.id` location is accepted.

`ERC20Transactor::withdraw_asset_with_surplus` then:
1. Calls `Matcher::matches_fungibles(what)` to get `(asset_id, amount)` straight from the caller-controlled `Asset` — `asset_id` is whatever contract address the attacker put in the `Location`, `amount` is whatever value the attacker chose [4](#0-3) .
2. Performs a `bare_call` from the signer's own account to that address, invoking `transfer(checking_account, amount)` [5](#0-4) .
3. Trusts only the ABI-decoded boolean return value of the call. If it decodes to `true`, it mints an `AssetsInHolding` credit for the full caller-chosen `amount`, backed by nothing but the contract's say-so [6](#0-5) .

There is no verification that:
- the target contract is a real Snowbridge-bridged ERC20 asset registered anywhere,
- the `transfer` call actually reduced any real balance,
- the returned `amount` corresponds to any state change at all.

An attacker can deploy their own pallet-revive contract at an address of their choosing (any address is legal for `AccountKey20`) whose `transfer(address,uint256)` selector unconditionally returns `true` without touching any storage. They then submit an ordinary signed XCM/`pallet_xcm::transfer_assets`-style extrinsic specifying `Asset { id: AssetId(Location::new(0, [AccountKey20{ key: <attacker_contract> }])), fun: Fungible(<any amount>) }`. The transactor calls the attacker's fake contract, gets `true`, and manufactures `AssetsInHolding` credit for the requested amount with zero real backing — precisely the "control the amount to redeem/withdraw via a specially crafted external contract" pattern from the report. This credit is exactly what the XCM executor uses to satisfy subsequent `DepositAsset`/`DepositReserveAsset`/`InitiateReserveWithdraw` instructions in the same program, letting the attacker move or represent a fabricated asset elsewhere in the executor's accounting, and — because Asset-Hub's ERC20 representation is precisely how Snowbridge-bridged Ethereum tokens are modeled on-chain — any downstream logic (fees, reserve transfers, bridging-out flows) that treats a successful `withdraw_asset`/credited `AssetsInHolding` as proof of genuine backed value is deceived into acting on value that was never actually withdrawn from anyone.

### Impact Explanation
This lets an unprivileged, ordinary signed user forge XCM asset holdings for an arbitrary "ERC20" asset id/amount of their choosing without ever depositing or holding real value, by exploiting the total absence of asset-registry validation in `Matcher`/`ERC20Transactor`. This directly matches the required impact categories: forged/mis-bound asset acceptance and unauthorized value creation in the XCM executor's holding register, which can propagate into downstream reserve/teleport/deposit logic and cause value-conservation violations (fabricated credit "settled" as if backed).

### Likelihood Explanation
Likelihood is high for any deployment enabling `ERC20Transactor` with `ERC20Matcher` (or an equivalently unrestricted `MaybeEquivalence` converter): no privileged actor, relayer, or governance action is required — a normal signed account can deploy a trivial contract via `pallet_revive::instantiate_with_code` and submit a standard XCM extrinsic. The only requirement is that this transactor/matcher pairing is wired into a runtime's `XcmConfig::AssetTransactors` (confirmed present in `asset-hub-westend`'s `xcm_config.rs`), and that nothing upstream in the XCM barrier/filter rejects unregistered `AccountKey20` locations before reaching the transactor.

### Recommendation
- Short term: Require `Matcher`/`AccountKey20ToH160` (or an equivalent converter used by `ERC20Transactor`) to check the extracted `H160` against an explicit on-chain registry of approved/bridged ERC20 asset contracts before treating a `transfer()` success as real value movement — mirroring how `pallet-assets`'s ERC20 precompile only recognizes addresses whose asset id exists in `pallet_assets::Asset` storage.
- Long term: Add integration tests that deploy a "fake ERC20" contract returning `true` unconditionally and assert that `withdraw_asset_with_surplus`/`deposit_asset_with_surplus` reject it unless the asset is registered, simulating the exact class of attack described in the external report.

### Proof of Concept
1. Attacker deploys a minimal pallet-revive contract `Fake` implementing only `function transfer(address,uint256) returns (bool) { return true; }` (no storage effects) at address `0xFAKE...`.
2. Attacker submits `pallet_xcm::transfer_assets` (or a custom XCM program) with:
   ```
   Asset { id: AssetId(Location::new(0, [AccountKey20 { network: None, key: 0xFAKE... }])), fun: Fungible(1_000_000_000_000) }
   ```
3. XCM executor invokes `ERC20Transactor::withdraw_asset_with_surplus`:
   - `Matcher::matches_fungibles` returns `(0xFAKE..., 1_000_000_000_000)` with no registry check [7](#0-6) .
   - `bare_call` invokes `Fake.transfer(checking_account, 1_000_000_000_000)`, which returns `true` without moving any balance [8](#0-7) .
   - `AssetsInHolding::new_from_fungible_credit` mints a credit of `1_000_000_000_000` for asset `0xFAKE...` with zero real backing.
4. The forged holding is now available to subsequent XCM instructions (`DepositAsset`, `DepositReserveAsset`, etc.) in the same program, letting the attacker "settle" a fabricated asset amount as if it were real.

Note: I was unable to fully trace whether any downstream instruction (e.g., Snowbridge outbound queue processing) treats this fabricated `Erc20Credit` as directly redeemable for real Ethereum-side tokens, since that would require inspecting the outbound queue / gateway message construction in more depth than the indexed context allowed. A Devin session with full repository/tooling access is recommended to confirm the exact end-to-end fund-loss path (e.g., whether bridging-out logic checks asset registration independently before emitting an unlock/mint instruction on Ethereum).

### Citations

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L150-216)
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

**File:** cumulus/parachains/runtimes/assets/common/src/lib.rs (L141-161)
```rust
/// Fallible converter from a location to a `H160` that matches any location ending with
/// an `AccountKey20` junction.
pub struct AccountKey20ToH160;
impl MaybeEquivalence<Location, H160> for AccountKey20ToH160 {
	fn convert(location: &Location) -> Option<H160> {
		match location.unpack() {
			(0, [AccountKey20 { key, .. }]) => Some((*key).into()),
			_ => None,
		}
	}

	fn convert_back(key: &H160) -> Option<Location> {
		Some(Location::new(0, [AccountKey20 { key: (*key).into(), network: None }]))
	}
}

/// [`xcm_executor::traits::MatchesFungibles`] implementation that matches
/// ERC20 tokens.
pub type ERC20Matcher =
	MatchedConvertedConcreteId<H160, u128, IsLocalAccountKey20, AccountKey20ToH160, TryConvertInto>;

```
