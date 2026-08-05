## Analysis

I found a strong local analog. The core broken invariant from the report — value is irrecoverably moved out of an account with no function or path to move it back — reappears in `pallet-accumulate-and-forward`'s XCM forwarding adapter.

### The mechanism

`pallet_accumulate_and_forward::Pallet::on_idle` periodically empties the accumulation account by calling `T::Forwarder::forward`, which for system parachains is implemented by `TeleportForwarderForAccountId32::forward`: [1](#0-0) 

The forwarder withdraws/teleports the funds via XCM in a single local transaction: [2](#0-1) 

The adapter's own doc comment admits the flaw: *"Once the local executor reports success, the message is queued and any destination-side rejection results in trapped assets at the destination with no automatic recovery path."* [3](#0-2) 

`with_transaction` only rolls back on **local** execution failure (`Outcome::Complete` vs. anything else); it has no visibility into whether the remote `DepositAsset` in `remote_xcm` actually lands: [4](#0-3) 

Once `Outcome::Complete` is returned locally, `on_idle` commits and fires `ForwardSucceeded`, permanently burning the source balance/total issuance — but the remote leg (`InitiateTransfer` → `DepositAsset` to the DAP staging account) is not guaranteed to succeed. If it fails on the destination (e.g., a stale/misconfigured staging account, an unfunded-below-ED staging account failing to be created, a filter/barrier rejecting the `UnpaidExecution`, or a version/weight mismatch), the teleported asset is trapped by the destination's XCM executor's standard asset-trap mechanism, with **no code path in this pallet (or its config) to claim or recover it** — precisely mirroring the DAO report's "deposits sent, no function to withdraw."

### Title
Accumulate-and-Forward pallet permanently burns tokens locally before remote settlement is confirmed, permanently trapping funds on destination-side XCM failure - (File: `polkadot/xcm/xcm-builder/src/forwarder.rs`)

### Summary
`TeleportForwarderForAccountId32::forward`, used by `pallet-accumulate-and-forward::on_idle` to periodically empty accumulated native-token balances (transaction fees, dust, coretime revenue) from system parachains, advances local settlement (burns the source balance, reduces total issuance, emits `ForwardSucceeded`) as soon as the **local** XCM program returns `Outcome::Complete`. This only confirms that the local `WithdrawAsset`/`InitiateTransfer` instructions executed; it gives no guarantee that the remote `DepositAsset` in the appended `remote_xcm` actually credits the destination staging account. Any destination-side rejection traps the teleported assets with no automatic recovery, permanently locking the funds.

### Finding Description
`forward()` builds an XCM program `[UnpaidExecution, DescendOrigin(source), WithdrawAsset(asset), InitiateTransfer{..., remote_xcm: [DepositAsset]}]` and executes it locally via `XcmExecutor::prepare_and_execute` inside `with_transaction`. The transaction is committed only based on the **local** `Outcome`: [4](#0-3) 

`Outcome::Complete` here means the local chain finished processing its own instructions, including successfully *sending* the follow-up message — it does not mean the remote chain executed `DepositAsset` successfully. The pallet's `on_idle` treats this local success as final settlement: [1](#0-0) 

If the remote-side `DepositAsset` fails — for example the destination staging account (`DapStagingLocation`) is not pre-funded with the existential deposit and the deposit amount doesn't create the account, or `UnpaidExecution` is rejected by a barrier, or `AllowedTeleport`/asset filters differ from expectations, or remote weight is insufficient despite `Weight::MAX` being requested locally — the teleported asset lands in the destination's asset trap. Claiming a trap requires issuing a `ClaimAssets` XCM instruction from the exact trapping origin (here `AliasOrigin(source_location)`, i.e., the sending-chain's accumulation account identity) with the exact ticket, which this pallet has no logic to construct or trigger — matching the report's root cause: **funds are moved to a location where no function exists to move them out**.

This directly violates the required pivot: *"Message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically"* — here, local burn/settlement (`ForwardSucceeded`, total-issuance reduction) advances before remote settlement is confirmed.

### Impact Explanation
Impact is high: this affects real value (transaction fees, dust removal, coretime revenue) on system parachains, configured to run automatically via `on_idle` on `bridge-hub-westend`, `collectives-westend`, and other system chains. A destination-side failure permanently burns local supply while assets are trapped remotely and unclaimable through any pallet-provided path — a genuine permanent fund/bridge-state lock, not merely a griefing/DoS.

### Likelihood Explanation
This runs unconditionally and periodically (`TransferPeriod`) for as long as `available_funds >= MinTransferAmount`, with no attacker action required — it is an automatic hook. Any transient destination-side misconfiguration, unfunded staging account, filter mismatch, or barrier rejection (all plausible operational states, not requiring a malicious actor) triggers the trap. The pallet's own doc comments already flag this as a known gap ("no automatic recovery path"), indicating the condition is realistically reachable rather than theoretical.

### Recommendation
Do not treat local `Outcome::Complete` as final settlement. Either:
- Require a confirmation/receipt mechanism from the destination (e.g., XCM query/response) before finalizing the local burn and emitting `ForwardSucceeded`, or
- Add an explicit, permissionless recovery path (mirroring the `reclaim_bounty_funds` pattern already used elsewhere in this codebase) that can issue `ClaimAssets` with the correct origin/ticket to recover trapped assets back to the accumulation or treasury account, or
- At minimum, ensure staging/destination accounts are guaranteed pre-funded/existing via genesis and add monitoring/alerting plus a documented, tested claim procedure, rather than relying on "no automatic recovery path."

### Proof of Concept
1. Deploy a system parachain with `pallet-accumulate-and-forward` configured with `TeleportForwarderForAccountId32` targeting a destination `DapStagingLocation` account that is *not* pre-funded with the destination chain's existential deposit (a plausible operational state — the pallet doc itself calls out the pre-funding requirement only for the *local* accumulation account, not the remote staging account).
2. Let fees/dust/coretime revenue accumulate above `MinTransferAmount` in the accumulation account.
3. At the next `TransferPeriod` boundary, `on_idle` calls `forward()`, which withdraws/teleports the funds; local execution reports `Outcome::Complete` (message successfully queued), so total issuance is reduced and `ForwardSucceeded` is emitted.
4. On the destination chain, `DepositAsset` to the unfunded staging account fails (below-ED account creation failure), and the XCM executor traps the teleported asset with origin `AliasOrigin(source_location)`.
5. No entity in the pallet, forwarder, or destination runtime configuration issues `ClaimAssets` for that origin/ticket — the funds are permanently stuck, matching the reported bug pattern of value being moved with no function available to retrieve it.

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
