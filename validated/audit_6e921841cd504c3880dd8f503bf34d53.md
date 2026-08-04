### Title
`accumulate-and-forward` pallet permanently burns protocol revenue on `on_idle` before confirming cross-chain settlement, trapping funds with no recovery path - (File: `substrate/frame/accumulate-and-forward/src/lib.rs`, `polkadot/xcm/xcm-builder/src/forwarder.rs`)

### Summary
The `pallet-accumulate-and-forward::on_idle` hook periodically forwards accumulated chain revenue (tx fees, dust, coretime revenue) by invoking `TeleportForwarderForAccountId32::forward`, which executes a local XCM `WithdrawAsset`/teleport-out program and treats `Outcome::Complete` as final success — emitting `Event::ForwardSucceeded` and irreversibly removing the funds from the local accumulation account. `Outcome::Complete` only confirms that the *local* leg of the XCM program (withdraw, burn/check-out, send) executed; it gives no guarantee that the remote `InitiateTransfer`/teleport-in and final `DepositAsset` on the destination chain actually settle. This mirrors the root cause in the external report: local state ("repaid"/"forwarded") is finalized based on one side of a cross-chain operation without validating that the corresponding remote-side accounting update actually completed.

### Finding Description
`on_idle` reads the accumulation account's spendable balance and calls the configured `Forwarder`: [1](#0-0) 

The only implementation of `Forwarder` shipped in-tree is `TeleportForwarderForAccountId32`, which builds a local XCM program that withdraws the asset, teleports it out via `InitiateTransfer`, and executes it synchronously: [2](#0-1) 

The code's own doc comment acknowledges the gap: local execution success is committed, but "any destination-side rejection results in trapped assets at the destination with no automatic recovery path": [3](#0-2) 

Because `forward()` returns `Ok(())` purely from `Outcome::Complete` of the *local* `prepare_and_execute` call, the pallet's `on_idle` immediately treats the transfer as fully settled and emits `ForwardSucceeded`, discarding the funds from local accounting forever — exactly like `repayBorrowInternal()` marking a borrow as repaid using only the same-chain state while the cross-chain leg (`_updateRepaymentState()`) has not yet been confirmed. There is no mechanism (receipt, callback, or `QueryResponse`) that reconciles the local “burned/forwarded” state with actual remote settlement. If the remote hop (`InitiateTransfer` teleport-in, `DepositAsset` to the DAP staging account) fails for any reason — e.g. an XCM version mismatch, the destination trusted-teleporter filter rejecting the reanchored asset, the destination staging account lacking existential deposit, or the destination-side weight limit being exceeded — the withdrawn/burned funds are trapped at the destination with `pallet_xcm::Event::AssetsTrapped`, and the local chain has already permanently reduced its accumulation account / total issuance accounting with no path to retry or recover.

### Impact Explanation
This hook runs unconditionally on every parachain configured with `pallet_accumulate_and_forward` (BridgeHub, Collectives, Coretime, People, and the Westend relay chain itself, per their runtime configs), forwarding real protocol revenue (transaction fees, dust, coretime revenue) every `TransferPeriod` blocks. A single misconfiguration or transient failure on the destination side (Asset Hub/staging pallet) causes that period's entire accumulated balance to be irreversibly lost/trapped, and — because there is no halting or backoff of the routine beyond the `ForwardFailed` path for outright send failures — a persistent destination-side rejection condition (e.g., broken teleport trust filter after a runtime upgrade, or destination account issues) will repeat this loss every `TransferPeriod` indefinitely, draining system-chain revenue with no recovery. This matches the "permanent user-fund or bridge-state lock" impact category.

### Likelihood Explanation
The `on_idle` forwarding is fully automatic (no privileged or attacker action needed) and runs on every configured chain at a fixed cadence. The failure condition only requires a destination-side rejection of the remote XCM leg (version skew, filter mismatch, ED/asset issues at the staging location) — none of which require a malicious relayer, validator, or governance actor; it can occur from ordinary configuration drift or transient destination-chain state, and the vulnerable code path (treating local `Outcome::Complete` as final settlement) always executes on the hot path with no idempotency/rollback safeguard beyond the local `with_transaction`.

### Recommendation
Do not treat local `Outcome::Complete` as final settlement for accounting/event purposes. Either (a) keep the withdrawn funds in a pending/escrow state and only finalize (burn from source accounting / emit `ForwardSucceeded`) upon a confirmed remote receipt (e.g., XCM `ReportTransactStatus`/`QueryResponse` from the destination), or (b) make the destination-side deposit failure-safe by having it route to a reserve/parking account that this pallet can retry/reclaim from, and detect+recover `AssetsTrapped` events programmatically rather than relying on the documented "no automatic recovery path."

### Proof of Concept
1. Configure `TeleportForwarderForAccountId32` on a system chain per the existing runtime wiring (e.g. BridgeHubWestend) as shown in [4](#0-3) .
2. Cause the destination (`AssetHubLocation`/DAP staging location) to reject the incoming teleport-in/`DepositAsset` — e.g. force a mismatched `IsTeleporter`/trusted-teleporter filter update on the destination, or drop the staging account below ED so `DepositAsset` fails at destination.
3. Accumulate revenue on the source chain past `MinTransferAmount`, then advance to a block where `block % TransferPeriod == 0`.
4. Observe `on_idle` executes `TeleportForwarderForAccountId32::forward`; the local `WithdrawAsset`/burn/check-out succeeds and is committed (`Outcome::Complete`), `Event::ForwardSucceeded` fires, and the funds vanish from local accounting.
5. On the destination chain, observe `pallet_xcm::Event::AssetsTrapped` for the forwarded amount with no automated recovery, repeating every subsequent `TransferPeriod` while the destination-side condition persists — permanently and continuously losing protocol revenue.

### Citations

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

**File:** polkadot/xcm/xcm-builder/src/forwarder.rs (L58-106)
```rust
	fn forward(source: AccountId, amount: Balance) -> Result<(), ()> {
		let dest = Dest::get();
		let asset = Asset { id: AssetId(NativeAsset::get()), fun: Fungible(amount.into()) };
		let beneficiary: Location = StagingLocation::get().into_location();

		let remote_xcm = Xcm(vec![DepositAsset { assets: Wild(AllCounted(1)), beneficiary }]);

		// The XCM flow: `ReceiveTeleportedAsset → AliasOrigin(source) → UnpaidExecution →
		// DepositAsset`. `preserve_origin: true` causes `InitiateTransfer` to prepend
		// `AliasOrigin(source_location)` to the remote XCM.
		let xcm: Xcm<XcmConfig::RuntimeCall> = Xcm(vec![
			UnpaidExecution { weight_limit: WeightLimit::Unlimited, check_origin: None },
			DescendOrigin(Junction::AccountId32 { network: None, id: source.into() }.into()),
			WithdrawAsset(asset.into()),
			InitiateTransfer {
				destination: dest,
				remote_fees: None,
				preserve_origin: true,
				assets: BoundedVec::truncate_from(alloc::vec![AssetTransferFilter::Teleport(
					Wild(AllCounted(1))
				),]),
				remote_xcm,
			},
		]);

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

**File:** cumulus/parachains/runtimes/bridge-hubs/bridge-hub-westend/src/lib.rs (L577-590)
```rust
impl pallet_accumulate_and_forward::Config for Runtime {
	type Currency = Balances;
	type PalletId = AccumulateForwardPalletId;
	type Forwarder = xcm_builder::TeleportForwarderForAccountId32<
		xcm_config::XcmConfig,
		testnet_parachains_constants::westend::locations::AssetHubLocation,
		xcm_config::WestendLocation,
		DapStagingLocation,
	>;
	type TransferPeriod = ForwardPeriod;
	type MinTransferAmount = MinForwardAmount;
	type BlockNumberProvider = RelaychainDataProvider<Runtime>;
	type WeightInfo = weights::pallet_accumulate_and_forward::WeightInfo<Runtime>;
}
```
