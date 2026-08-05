### Title
`ERC20Transactor::withdraw_asset_with_surplus` treats non-conforming ERC20 tokens (empty/void `transfer` return data) as failed even though the on-chain transfer already succeeded, silently draining user tokens into the checking account with no XCM credit - ([File: cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs])

### Summary
The external report's core broken invariant is: code assumes every ERC20 token strictly returns a `bool` from `transfer()`/`transferFrom()`, and blindly trusts/decodes that return value without accounting for non-conforming tokens that return no data (void) on success. This causes either reverts-that-aren't-reverts or, worse, state advancing based on a decode that doesn't reflect the real transfer outcome. `ERC20Transactor` in this repository reproduces exactly this assumption when bridging ERC20 tokens through XCM on `pallet-revive`.

### Finding Description
`ERC20Transactor::withdraw_asset_with_surplus` executes an ERC20 `transfer` from the user's account to a `TransfersCheckingAccount` via `pallet_revive::Pallet::<T>::bare_call`, then decodes the returned data strictly as a `bool` using `IERC20::transferCall::abi_decode_returns_validate`: [1](#0-0) 

The logic branches only on two outcomes of a *non-reverted* call: `did_revert()` (explicit failure) or successful ABI decode of the return bytes as `bool` (`Ok(true)`/`Ok(false)`). There is no handling for the well-known non-conforming ERC20 case — tokens (historically USDT-class, BNB-class contracts, and many others) whose `transfer()`/`transferFrom()` execute the balance update correctly but return no data at all (a "void" return) instead of an ABI-encoded `bool`. In that case:
- `result` is `Ok(...)` because the call did not revert; the on-chain ERC20 balance transfer from the user to `checking_address` genuinely happened.
- `return_value.did_revert()` is `false`.
- `IERC20::transferCall::abi_decode_returns_validate(&return_value.data)` fails to decode (empty/short data can't be validated as a 32-byte `bool`), producing `Err(XcmError::FailedToTransactAsset("ERC20 contract result couldn't decode"))`.

This is the structural analog to the reported Vault bug: the report's Vault called `transfer()`/`transferFrom()` directly and assumed a certain call semantics (bool return), breaking for non-conforming tokens. Here, `ERC20Transactor` similarly hard-codes strict bool-decoding as the sole criterion for "did the transfer succeed," despite the real on-chain effect (fund movement) already being final and irreversible by the time the decode happens.

The `deposit_asset_with_surplus` path has the mirrored issue in the other direction: [2](#0-1) 

### Impact Explanation
For `withdraw_asset_with_surplus`, once the underlying `transfer` call executes without reverting, the token has already moved out of the user's account into `TransfersCheckingAccount` — this is irreversible ERC20 state, external to the XCM executor. If the ABI-decode of the return data subsequently fails (as it will for any non-conforming ERC20 registered/matched by `Matcher::matches_fungibles` that returns void on success), the function returns an `Err`, meaning the XCM executor treats the withdrawal as if it never happened: no `AssetsInHolding` credit is created, and the surrounding XCM program (e.g., `DepositAsset` to a beneficiary elsewhere, or forwarding across chains) never executes for that asset. The user's real tokens are stuck in `TransfersCheckingAccount` with no XCM-visible credit and no corresponding `AssetsInHolding` entry to reclaim or redirect them — a permanent user-fund lock exactly matching the "permanent user-fund or bridge-state lock" impact category, without requiring any malicious peer, relayer, governance actor, or privileged action. This also degrades legitimate public XCM execution for any non-conforming ERC20 registered for bridging, since users lose funds simply by using a supported/registered token whose ABI doesn't perfectly match the strict `bool` return assumption.

### Likelihood Explanation
Likelihood depends entirely on whether tokens matched by `Matcher::matches_fungibles` (i.e., registered as ERC20 assets bridgeable via XCM on this transactor) include or could include non-conforming implementations. This is realistic: the whole reason ecosystems maintain SafeERC20-style wrappers is that a meaningful fraction of widely-used ERC20 tokens do not strictly conform to returning `bool`. Any deployment that permits third-party or foreign ERC20 contracts (rather than a strictly curated, audited allow-list of fully EIP-20-conformant tokens) to be registered for this transactor is exposed. The trigger requires no special privilege — an ordinary user sending a withdrawal/transfer instruction involving such a token is sufficient.

### Recommendation
Do not treat "successful strict-bool decode" as the only success signal. Follow the same OZ `SafeERC20` semantics already referenced in the external report: treat a non-reverted call as successful if either (a) `return_value.data` is empty (void return, conforming to the common non-standard pattern) or (b) it decodes to `true`; only treat `Ok(false)` or actual revert as failure. Concretely, adjust both `withdraw_asset_with_surplus` and `deposit_asset_with_surplus` in `cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs` to accept empty return data as success (mirroring `SafeERC20._callOptionalReturnBool`/`safeTransfer` behavior), and/or restrict `Matcher`/asset registration to strictly EIP-20-conformant contracts if changing the decode logic is not feasible.

### Proof of Concept
1. Deploy (or register via `Matcher::matches_fungibles`) a non-conforming ERC20 contract whose `transfer(address,uint256)` performs the balance update and emits `Transfer` but returns no data (mirrors classic USDT/BNB-style behavior), analogous to `MyTokenFake.sol`/`MyTokenExpensive.sol` fixtures already present in this repo as examples of non-standard-return test tokens: [3](#0-2) 
2. Submit an XCM program that invokes `WithdrawAsset` for this token via `ERC20Transactor::withdraw_asset_with_surplus`, followed by `DepositAsset` to some beneficiary.
3. Observe: `bare_call` executes the ERC20 `transfer`, funds move from `who` to `TransfersCheckingAccount` (verifiable via `balanceOf`), `did_revert()` is `false`, but `abi_decode_returns_validate` fails on the non-standard/empty return data, causing `withdraw_asset_with_surplus` to return `Err(XcmError::FailedToTransactAsset("ERC20 contract result couldn't decode"))`.
4. The XCM executor aborts the asset leg as failed; no `AssetsInHolding` is created, so the subsequent `DepositAsset` never fires and the user's tokens remain trapped in `TransfersCheckingAccount` with no code path to credit or return them via the failed XCM instruction. [4](#0-3)

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

**File:** substrate/frame/revive/fixtures/contracts/MyTokenFake.sol (L1-21)
```text
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

contract MyTokenFake {
    mapping(address account => uint256) private _balances;

    uint256 private _totalSupply;

    constructor(uint256 total) {
        // We mint `total` tokens to the creator of this contract, as
        // a sort of genesis.
        _mint(msg.sender, total);
    }

    function transfer(address to, uint256 value) public virtual returns (uint256) {
        address owner = msg.sender;
        _transfer(owner, to, value);
        return 1243657816489523;
    }

    function _transfer(address from, address to, uint256 value) internal {
```
