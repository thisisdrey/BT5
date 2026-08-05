Audit Report

## Title
Fee-on-transfer / reverting ERC20 tokens permanently strand user funds in the `TransfersCheckingAccount` of `ERC20Transactor` - (File: `cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs`)

## Summary
`ERC20Transactor::withdraw_asset_with_surplus` credits a purely in-memory `Erc20Credit(amount)` XCM imbalance into `AssetsInHolding` as soon as the withdraw-leg ERC20 `transfer` call to `TransfersCheckingAccount` returns `true`, without verifying that the checking account's real balance actually increased by `amount`. `deposit_asset_with_surplus` later attempts to forward exactly `amount` from the checking account to the beneficiary; for fee-on-transfer or otherwise non-standard ERC20 tokens where the checking account receives less than `amount` or where transferability degrades between the two legs, this second transfer fails, and the fictitious, unbacked `Erc20Credit` is trapped by the XCM executor while the real (reduced) token balance remains permanently stuck in `TransfersCheckingAccount` with no reconciliation mechanism.

## Finding Description
In `withdraw_asset_with_surplus`, the withdraw leg calls `IERC20::transfer(checking_address, amount)` via `pallet_revive::bare_call`, and on a `true` boolean return it unconditionally mints `Erc20Credit(amount)` into `AssetsInHolding`, with no read-back of `balanceOf(checking_address)` to confirm the actual amount received: [1](#0-0) 

`Erc20Credit` is explicitly documented and implemented as an accounting-only imbalance type disconnected from any real balance enforcement ("the actual balance constraints are enforced by the ERC20 smart contract itself rather than the runtime"): [2](#0-1) 

`deposit_asset_with_surplus` then transfers `amount` from the checking account to the beneficiary; if that call fails (revert, `false` return, or bad decode), the function returns `(what, XcmError::FailedToTransactAsset(..))`, where `what` is the still-unbacked `Erc20Credit`: [3](#0-2) 

The runtime wiring confirms `ERC20Transactor` is a live `AssetTransactor` on Asset Hub Westend, backed by `ERC20Matcher` and a fixed `ERC20TransfersCheckingAccount` (a pallet-derived account), with no visible per-asset allow-list gate at this transactor layer: [4](#0-3) 

For any ERC20 whose `transfer` does not preserve the exact requested amount end-to-end (fee-on-transfer, pausable, deny-list, transfer caps that trip between the two legs), the withdraw leg over-credits `Erc20Credit` relative to what the checking account actually holds, and the deposit leg's identical retry (via `ClaimAsset`) will deterministically fail again against the same reduced balance — there is no code path in this file that ever reconciles the trapped accounting entry with the real leftover ERC20 balance in `TransfersCheckingAccount`.

## Impact Explanation
This is a permanent lock of real user funds: tokens genuinely transferred into `TransfersCheckingAccount` become unrecoverable once the deposit leg fails for a token whose transfer semantics deviate from a naive full-amount, always-repeatable model. This matches the accepted "permanent user-fund … lock" impact and the "Balances … must conserve value and settle exactly once" pivot, since the `Erc20Credit` imbalance and the real on-chain ERC20 balance of `TransfersCheckingAccount` diverge with no path back to the depositor.

## Likelihood Explanation
Reachability requires an unprivileged user to route an XCM message (e.g., a reserve-asset withdraw/deposit) through `ERC20Transactor` for an ERC20 contract matched by `ERC20Matcher`, which is exposed as a general-purpose `AssetTransactor` on Asset Hub Westend rather than gated by an explicit asset-registration allow-list visible in this code path. Fee-on-transfer and denylist/pausable ERC20 tokens are common in practice, so an attacker or even an unwitting user interacting with such a token via XCM can trigger the fund lock without needing any privileged role, matching this repo's own tests that already exercise other transfer-failure branches (revert, non-bool return) but not the partial-transfer/fee-on-transfer case.

## Recommendation
- In `withdraw_asset_with_surplus`, measure the actual increase in `TransfersCheckingAccount`'s ERC20 balance (pre/post `balanceOf`) and credit `Erc20Credit` with that real delta rather than the requested `amount`.
- In `deposit_asset_with_surplus`, apply the same pre/post balance check pattern to detect partial transfers even when the call returns `true`.
- On any deposit-leg failure, avoid handing back an un-backed `Erc20Credit` as a generically trappable/reclaimable XCM asset; instead track a real per-account ledger for `TransfersCheckingAccount` and provide an explicit sweep/refund path tied to the actual remaining balance.
- Alternatively, require assets routed through `ERC20Transactor` to be verified as strictly amount-preserving (no fee-on-transfer, no mid-flight pausing) before being matched/accepted by `ERC20Matcher`.

## Proof of Concept
1. Deploy a fee-on-transfer ERC20 `T` (e.g., 5% fee on every `transfer`) reachable by `ERC20Matcher`.
2. Submit an XCM that does `WithdrawAsset(T, 1000)` from Alice — `withdraw_asset_with_surplus` calls `T.transfer(checking_account, 1000)`; the checking account's real balance increases by only 950, but `AssetsInHolding` is credited with `Erc20Credit(1000)` because the boolean return was `true`.
3. The XCM continues with `DepositAsset(T, 1000, beneficiary)` — `deposit_asset_with_surplus` calls `T.transfer(beneficiary, 1000)` from the checking account, which only holds 950, causing the ERC20 `transfer` to revert or return `false`.
4. `deposit_asset_with_surplus` returns `Err((what, XcmError::FailedToTransactAsset(..)))`; the XCM executor traps the `Erc20Credit(1000)` accounting entry.
5. Any subsequent `ClaimAsset` + `DepositAsset` retry hits the same 950-balance shortfall, permanently stranding the 950 real tokens in `TransfersCheckingAccount` with no recovery path for Alice.

### Citations

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L73-107)
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

impl UnsafeManualAccounting<u128> for Erc20Credit {
	fn saturating_subsume(&mut self, mut other: Box<dyn ImbalanceAccounting<u128>>) {
		let amount = other.forget_imbalance();
		self.0 = self.0.saturating_add(amount);
	}
}

impl ImbalanceAccounting<u128> for Erc20Credit {
	fn amount(&self) -> u128 {
		self.0
	}
	fn saturating_take(&mut self, amount: u128) -> Box<dyn ImbalanceAccounting<u128>> {
		let new = self.0.min(amount);
		self.0 = self.0 - new;
		Box::new(Erc20Credit(new))
	}
}
```

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L191-204)
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
				} else {
```

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L270-297)
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
```

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/src/xcm_config.rs (L213-246)
```rust
parameter_types! {
	/// Taken from the real gas and deposits of a standard ERC20 transfer call.
	pub const ERC20TransferGasLimit: Weight = Weight::from_parts(500_000_000_000, 10 * 1024 * 1024);
	pub const ERC20TransferStorageDepositLimit: Balance = 10_200_000_000;
	pub ERC20TransfersCheckingAccount: AccountId = PalletId(*b"py/revch").into_account_truncating();
	pub DapBufferAccount: AccountId = pallet_dap::Pallet::<Runtime>::buffer_account();
}

/// Transactor for ERC20 tokens.
pub type ERC20Transactor = assets_common::ERC20Transactor<
	// We need this for accessing pallet-revive.
	Runtime,
	// The matcher for smart contracts.
	assets_common::ERC20Matcher,
	// How to convert from a location to an account id.
	LocationToAccountId,
	// The maximum gas that can be used by a standard ERC20 transfer.
	ERC20TransferGasLimit,
	// The maximum storage deposit that can be used by a standard ERC20 transfer.
	ERC20TransferStorageDepositLimit,
	// We're generic over this so we can't escape specifying it.
	AccountId,
	// Checking account for ERC20 transfers.
	ERC20TransfersCheckingAccount,
>;

/// Means for transacting assets on this chain.
pub type AssetTransactors = (
	FungibleTransactor,
	FungiblesTransactor,
	ForeignFungiblesTransactor,
	UniquesTransactor,
	ERC20Transactor,
);
```
