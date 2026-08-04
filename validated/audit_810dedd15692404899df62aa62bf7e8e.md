## Analysis Summary

I found a real local analog to the ERC4626 "trust the return value instead of verifying real output" bug class in `cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs`, which implements `TransactAsset` for XCM message execution against ERC20 contracts deployed on `pallet-revive`.

### Title
XCM `ERC20Transactor` credits/debits exact requested amount based solely on ERC20 `transfer` boolean return, without verifying actual balance delta - (File: `cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs`)

### Finding Description
`withdraw_asset_with_surplus` calls the token contract's `transfer` function and, if the ABI-decoded boolean return value is `true`, unconditionally mints an `AssetsInHolding` credit for the full requested `amount`: [1](#0-0) 

Likewise, `deposit_asset_with_surplus` treats a `true` return from the ERC20 `transfer` call as full success of the deposit, and returns `Ok(surplus)` without ever checking that the beneficiary's ERC20 balance actually increased by `amount`: [2](#0-1) 

This is the exact same broken invariant as the ERC4626 report: the accounting layer (here, XCM's `AssetsInHolding`/`Erc20Credit`, analogous to the vault's share/asset accounting) is derived purely from the external contract's self-reported return value, not from an independently observed balance change (e.g. `balanceOf` before/after). The doc comment on `Erc20Credit` even states this is intentional design: *"the actual balance constraints are enforced by the ERC20 smart contract itself rather than the runtime"*: [3](#0-2) 

Any ERC20 contract that is fee-on-transfer, rebasing, deflationary, or otherwise transfers less than `amount` while still returning `true` (a widely observed real-world token behavior, and exactly the vault-return-value mismatch the external report describes) will cause the runtime's XCM holding register to be credited/debited for an amount that does not match the real token movement.

### Impact Explanation
Because `withdraw_asset_with_surplus` mints an `Erc20Credit(amount)` into the XCM holding register based on the boolean return alone, a token contract that under-transfers (moves fewer tokens to the checking account than `amount` but returns `true`) lets the XCM executor believe it holds `amount` worth of value it never received. That inflated `AssetsInHolding` credit can then be routed through further XCM instructions (deposited elsewhere, exchanged, etc.), effectively manufacturing value out of thin air relative to the checking account's real ERC20 balance — a "theft or unbacked mint" class impact on the reserve/checking-account accounting for that asset. Symmetrically, on `deposit_asset_with_surplus`, a token that under-delivers to the beneficiary while returning `true` will make the runtime think a deposit fully succeeded when the beneficiary actually received less, silently misallocating funds to the wrong (lesser) amount while the XCM program proceeds as if it succeeded in full.

### Likelihood Explanation
This path is reachable from unprivileged XCM execution (any XCM message routed through this asset transactor for an ERC20-class asset) and does not require a malicious peer, validator, or relayer — only that the underlying token contract (which can be a normal, permissionlessly deployed pallet-revive contract) exhibits fee-on-transfer/deflationary/short-transfer semantics while still returning `true`. That is a known real-world ERC20 pattern (identical to the vault behavior flagged in the source report), so the precondition is plausible and does not require any privileged actor.

### Recommendation
Before crediting/debiting `amount`, snapshot the relevant account's ERC20 balance via `balanceOf` immediately before and after the `transfer` call and use the observed delta (not the boolean return value) as the amount credited to `Erc20Credit` / considered deposited. Optionally, additionally require the boolean return to be `true` as a sanity check, but never treat the return value alone as authoritative for the transferred amount.

### Proof of Concept
1. Deploy (or use an already-deployed) ERC20 contract on `pallet-revive` whose `transfer` implementation applies a transfer fee/burns a portion of `value` but still returns `true` (a standard "fee-on-transfer" ERC20 pattern).
2. Register this contract as an XCM-recognized asset routed through `ERC20Transactor`.
3. Submit an XCM message that withdraws `amount` of this asset from an account via `withdraw_asset_with_surplus`. The contract transfers `amount - fee` to the checking account but returns `true`.
4. Observe that `AssetsInHolding` is credited with the full `Erc20Credit(amount)` at line 200 of `erc20_transactor.rs`, even though the checking account's real ERC20 balance only increased by `amount - fee`.
5. Use that inflated holding in a subsequent `deposit_asset_with_surplus` (or other XCM asset transfer) to move/settle more value than the checking account actually possesses in the underlying ERC20 contract, demonstrating an accounting mismatch between on-chain XCM holdings and real ERC20 balances.

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

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L191-203)
```rust
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
