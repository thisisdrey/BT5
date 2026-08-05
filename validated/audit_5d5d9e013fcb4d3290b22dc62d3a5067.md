Audit Report

## Title
Accumulate-and-forward pallet treats local teleport queuing as final settlement while destination-side `DepositAsset` failure permanently traps accumulated treasury/fee revenue with no automatic recovery - ([File: polkadot/xcm/xcm-builder/src/forwarder.rs])

## Summary
`TeleportForwarderForAccountId32::forward` (backing `pallet_accumulate_and_forward`, wired into Westend relay chain and the collectives/coretime/people/bridge-hub-westend system-chain runtimes) burns the local balance and returns `Ok(())` as soon as the *local* XCM leg completes (`Outcome::Complete`), before the remote `DepositAsset` on the destination executes. If the remote deposit independently fails, the teleported value is destroyed on the source chain and trapped on the destination under an aliased origin that ordinary accounts cannot practically claim.

## Finding Description
`Pallet::on_idle` reads `available_funds` from the accumulation account and calls `T::Forwarder::forward`, committing to `ForwardSucceeded` purely based on the `Result<(), ()>` returned by the forwarder, with no destination confirmation and no retry state: [1](#0-0) 

`TeleportForwarderForAccountId32::forward` constructs an XCM that withdraws/teleports the native asset locally and sends `InitiateTransfer` (`preserve_origin: true`) with a `remote_xcm` ending in `DepositAsset { assets: Wild(AllCounted(1)), beneficiary }` to the destination. It wraps only the **local** `prepare_and_execute` call in `with_transaction`, committing as soon as `Outcome::Complete` is observed locally — i.e., once the local burn and message enqueue succeed, independent of what happens at the destination: [2](#0-1) 

The module's own doc comment confirms this design gap explicitly: "Once the local executor reports success, the message is queued and any destination-side rejection results in trapped assets at the destination with no automatic recovery path," and separately notes the adapter passes `Weight::MAX` and relies on the caller to bound weight, only being safe for internal `on_idle`-style hooks: [3](#0-2) 

The crate-level docs corroborate that total issuance is burnt locally on forward and is expected to be re-minted at the destination on receipt, with no accounting for the failure case: [4](#0-3) 

I verified that `pallet_xcm` does provide a generic, permissionless `claim_assets` extrinsic and an `AssetTrap`/`ClaimAssets` mechanism for recovering trapped assets: [5](#0-4)  However, this recovery path requires the caller's converted XCM origin to exactly match the trap's origin key, which here is the aliased remote location produced by `AliasOrigin(source_location)` (`preserve_origin: true`) — not a plain destination-chain account: [6](#0-5) [7](#0-6)  Reclaiming the funds therefore requires manually constructing and sending an XCM message from the exact origin chain/account that impersonates the aliased origin — not something available to an ordinary destination-chain user, and no such automated claim/retry flow is wired into this pallet or its forwarder.

This is a genuine violation of the "conserve value and settle exactly once" invariant for treasury-bound revenue: the source-side burn is unconditional and final (event `ForwardSucceeded` is emitted, and the pallet retains no pending/retry state), while the destination-side settlement is not guaranteed, creating a window where value is destroyed without being credited anywhere reachable by ordinary means.

## Impact Explanation
The affected pallet accumulates and periodically forwards protocol-level fee, dust-removal, and coretime revenue on production system chains (Westend relay, collectives-westend, coretime-westend, people-westend, bridge-hub-westend), confirmed by their runtime wiring of `pallet_accumulate_and_forward`/`TeleportForwarderForAccountId32`. A destination-side `DepositAsset` failure (e.g., beneficiary below existential deposit, barrier rejection, or remote execution running out of weight) permanently reduces local total issuance with no reachable, ordinary-user recovery path, matching the "permanent fund/bridge-state lock" and "conserve value / settle exactly once" impact categories for treasury-bound revenue.

## Likelihood Explanation
The vulnerable path executes automatically every `TransferPeriod` via `on_idle`, without any privileged or attacker action needed to trigger the forward itself; the risk materializes whenever the destination-side leg fails for any transient or configuration reason. The pallet authors' own doc comments in `forwarder.rs` acknowledge this exact failure mode as a known, currently-unmitigated design property of the adapter, and the finalization of `ForwardSucceeded` occurs strictly before any destination confirmation is possible, since the two legs are not connected by any tracked confirmation mechanism.

## Recommendation
1. Do not finalize local accounting (`ForwardSucceeded`, discarding retry state) based solely on local XCM completion; track pending forwards until destination-side confirmation (e.g., a `QueryResponse` or delivery/execution report) is received.
2. Avoid `UnpaidExecution` + `Weight::MAX` for the remote leg in favor of a bounded, deterministic weight limit so failures are detectable and can drive a retry rather than silently trapping funds.
3. Guarantee the destination beneficiary/staging account satisfies ED requirements before relying on `DepositAsset`, and/or add a permissioned or automated claim/retry mechanism specific to this pipeline so trapped assets do not require hand-crafting a `ClaimAsset` XCM from the exact aliased origin.

## Proof of Concept
1. Configure a system chain (e.g., coretime-westend) with `pallet_accumulate_and_forward` forwarding to the relay chain via `TeleportForwarderForAccountId32`, per the runtime wiring in `cumulus/parachains/runtimes/coretime/coretime-westend/src/xcm_config.rs`.
2. Accumulate fees/dust past `MinTransferAmount` in the accumulation account.
3. Arrange for the destination `StagingLocation` beneficiary to fail `DepositAsset` (e.g., below ED, or a weight/barrier condition at the destination).
4. At the next `TransferPeriod` boundary, `on_idle` invokes `forward()`: the local `WithdrawAsset` + teleport burn commits (`Outcome::Complete`), `Ok(())` is returned, and `Event::ForwardSucceeded` is deposited — while the remote `DepositAsset` subsequently fails and the assets land in the destination's asset trap under the aliased source origin.
5. Confirm the trapped assets cannot be recovered via `pallet_xcm::claim_assets` from an ordinary destination-chain account, since `ensure_origin` requires an exact match to the aliased remote-chain origin, per [8](#0-7) .

### Citations

**File:** substrate/frame/accumulate-and-forward/src/lib.rs (L40-44)
```rust
//! ## Total Issuance
//!
//! Accumulated funds are burnt upon forwarding (reducing `total_issuance` here) and the same
//! funds are minted at the destination when the sent message is received.

```

**File:** substrate/frame/accumulate-and-forward/src/lib.rs (L184-198)
```rust
			// Attempt to forward accumulated funds.
			match T::Forwarder::forward(accumulation_account, available_funds) {
				Ok(()) => {
					Self::deposit_event(Event::ForwardSucceeded { amount: available_funds });
				},
				Err(()) => {
					log::debug!(
						target: LOG_TARGET,
						"accumulate-forward transfer of {:?} failed at block {:?}",
						available_funds,
						block,
					);
					Self::deposit_event(Event::ForwardFailed { amount: available_funds });
				},
			}
```

**File:** polkadot/xcm/xcm-builder/src/forwarder.rs (L34-42)
```rust
/// XCM adapter that implements [`pallet_accumulate_and_forward::Forwarder`] for AccountId32-type
/// source accounts by teleporting native tokens to a target account on a destination chain.
/// Local-execution failures roll back all local state changes. Once the local executor reports
/// success, the message is queued and any destination-side rejection results in trapped assets
/// at the destination with no automatic recovery path.
///
/// NOTE: This adapter passes `Weight::MAX` to the XCM executor, relying on the call site to
/// enforce a weight budget before invoking it. It is designed to be called only from rate-limited
/// internal hooks such as `on_idle` and should never be wired to user-callable extrinsics.
```

**File:** polkadot/xcm/xcm-builder/src/forwarder.rs (L83-106)
```rust
		with_transaction(|| -> TransactionOutcome<Result<(), DispatchError>> {
			let outcome = XcmExecutor::<XcmConfig>::prepare_and_execute(
				Location::here(),
				xcm,
				&mut [0u8; 32],
				Weight::MAX,
				Weight::MAX,
			);

			match outcome {
				Outcome::Complete { .. } => TransactionOutcome::Commit(Ok(())),
				exec_error => {
					tracing::debug!(
						target: LOG_TARGET,
						?exec_error,
						"accumulate-forward: XCM execution failed"
					);

					TransactionOutcome::Rollback(Err(DispatchError::Other("XCM execution failed")))
				},
			}
		})
		.map_err(|_| ())
	}
```

**File:** polkadot/xcm/pallet-xcm/src/lib.rs (L1520-1531)
```rust
		/// Claims assets trapped on this pallet because of leftover assets during XCM execution.
		///
		/// - `origin`: Anyone can call this extrinsic.
		/// - `assets`: The exact assets that were trapped. Use the version to specify what version
		/// was the latest when they were trapped.
		/// - `beneficiary`: The location/account where the claimed assets will be deposited.
		#[pallet::call_index(12)]
		pub fn claim_assets(
			origin: OriginFor<T>,
			assets: Box<VersionedAssets>,
			beneficiary: Box<VersionedLocation>,
		) -> DispatchResult {
```

**File:** polkadot/xcm/pallet-xcm/src/lib.rs (L1532-1532)
```rust
			let origin_location = T::ExecuteXcmOrigin::ensure_origin(origin)?;
```

**File:** polkadot/xcm/xcm-executor/src/lib.rs (L1558-1564)
```rust
			ClaimAsset { assets, ticket } => {
				let origin = self.origin_ref().ok_or(XcmError::BadOrigin)?;
				self.ensure_can_subsume_assets(assets.len())?;
				let claimed = Config::AssetTrap::claim_assets(origin, &ticket, &assets, &self.context);
				self.holding.subsume_assets(claimed.ok_or(XcmError::UnknownClaim)?);
				Ok(())
			},
```

**File:** polkadot/xcm/xcm-executor/src/traits/drop_assets.rs (L73-82)
```rust
pub trait ClaimAssets {
	/// Claim any assets available to `origin` and return them in a single `AssetsInHolding` value,
	/// together with the weight used by this operation.
	fn claim_assets(
		origin: &Location,
		ticket: &Location,
		what: &Assets,
		context: &XcmContext,
	) -> Option<AssetsInHolding>;
}
```
