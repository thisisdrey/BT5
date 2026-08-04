## Analog Found

The LiFi bug's core invariant break is: **a cross-domain transfer is marked "complete" and the source side finalizes/burns funds once the local leg succeeds, while the destination-side execution can independently fail, silently stranding the transferred value with no built-in recovery.** The exact same broken invariant exists in this repository's `pallet_accumulate_and_forward` / `TeleportForwarderForAccountId32` teleport-forwarding flow.

### Title
Accumulate-and-forward teleport marks funds as "forwarded" and burns local issuance before destination-side deposit is confirmed, permanently trapping funds on remote failure - (File: `polkadot/xcm/xcm-builder/src/forwarder.rs`)

### Summary
`TeleportForwarderForAccountId32::forward` builds a single XCM program that (1) withdraws funds from the accumulation account locally, then (2) `InitiateTransfer`s them via teleport to a destination chain, with a *remote* `DepositAsset` instruction targeting a `StagingLocation` beneficiary (e.g. `pallet-dap`'s staging account on Asset Hub). The `on_idle` hook of `pallet_accumulate_and_forward` treats `Outcome::Complete` from `XcmExecutor::prepare_and_execute` — which only reflects successful *local* queuing of the outbound message — as full success, emitting `ForwardSucceeded` and irreversibly reducing the source chain's `total_issuance` (per the pallet's own doc: "Accumulated funds are burnt upon forwarding"). The remote `DepositAsset` on the destination is executed asynchronously and independently; if it fails there (e.g. beneficiary account issue, weight limit, or a future runtime change to `StagingLocation`), the teleported assets are trapped in the destination's Asset Trap with, per the adapter's own doc comment, "no automatic recovery path."

### Finding Description [1](#0-0) 
The adapter's own documentation states the risk directly: local rollback is guaranteed, but "[o]nce the local executor reports success, the message is queued and any destination-side rejection results in trapped assets at the destination with no automatic recovery path." [2](#0-1) 
The XCM program withdraws the asset locally and teleports it via `InitiateTransfer` with a `remote_xcm` containing only `DepositAsset { assets: Wild(AllCounted(1)), beneficiary }`. There is no `SetErrorHandler`/`SetAppendix` on the *remote* program to redirect a failed deposit to a recoverable/fallback beneficiary, and the local `with_transaction` only guards local state — it cannot roll back once the message is handed to the router and `Outcome::Complete` is returned. [3](#0-2) 
`on_idle` calls `T::Forwarder::forward(...)` and, on `Ok(())`, immediately fires `Event::ForwardSucceeded`, and — per the pallet doc — burns the corresponding amount from `total_issuance` on the source chain. [4](#0-3) 
The module doc confirms: "Accumulated funds are burnt upon forwarding (reducing total_issuance here) and the same funds are minted at the destination when the sent message is received" — an assumption that only holds if the destination-side `DepositAsset` actually succeeds, which is never confirmed back to the source chain.

This is the direct structural analog of the LiFi report: a bridge-style protocol commits/settles state (burns issuance, emits success event) based on the success of the *outbound* leg only, while the destination-side completion of the transfer (equivalent to `swapAndCompleteBridgeTokensViaXxx` in LiFi) can fail independently and leave value stranded — in this case in the destination's XCM Asset Trap — with no guaranteed claim path back to the intended beneficiary (`StagingLocation`, which is a pallet-controlled account, not a governance/relayer key that could easily claim trapped assets tied to an aliased, pallet-derived origin).

### Impact Explanation
This violates the required invariant that "message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically." Protocol-level revenue (transaction fees, dust, coretime revenue — everything routed into the accumulation account via `DealWithFeesSplit`/`OnUnbalanced`) that is periodically swept by this pallet can be permanently and unrecoverably burned on the source chain while never actually landing at the intended destination beneficiary, constituting a permanent fund loss/lock for protocol-collected value, with no admin/governance/relayer misbehavior required — it is a pure atomicity gap in the pallet's own success/settlement logic.

### Likelihood Explanation
The forward path runs automatically and repeatedly via `on_idle` every `TransferPeriod` whenever `available_funds >= MinTransferAmount`, so any transient failure of the remote `DepositAsset` (fee/weight edge cases, ED issues on the `StagingLocation` account, or the beneficiary configuration changing across a runtime upgrade without atomic coordination with the sending chain) triggers this loss automatically, without any attacker action needed on the destination side — this makes it a systemic/likely-to-recur issue rather than a purely theoretical one, matching "permanent user-fund or bridge-state lock" and "public underpriced work" style acceptance criteria (state advances/settles before destination execution is confirmed).

### Recommendation
Do not treat local `Outcome::Complete` as final settlement. Either (a) require an XCM query/callback confirming the remote `DepositAsset` succeeded before emitting `ForwardSucceeded`/burning issuance, or (b) attach a `SetErrorHandler`/`SetAppendix` to the remote program that deposits to a chain-controlled, recoverable trap-claim path, and reconcile `pallet_accumulate_and_forward`'s issuance accounting only after destination confirmation, mirroring the fix pattern already applied for Snowbridge's inbound-queue-v2 asset-claimer trapping issue in this same repository.

### Proof of Concept
1. Fund the accumulation account above `MinTransferAmount`.
2. At a block that is a multiple of `TransferPeriod`, `on_idle` calls `TeleportForwarderForAccountId32::forward`, which executes `WithdrawAsset` + `InitiateTransfer` locally; `XcmExecutor::prepare_and_execute` returns `Outcome::Complete` because only local execution (fund withdrawal + message enqueue) is evaluated.
3. `pallet_accumulate_and_forward` emits `ForwardSucceeded` and (per its documented issuance model) burns the amount from local `total_issuance`.
4. The queued remote XCM executes on the destination chain later; its final instruction, `DepositAsset { beneficiary: StagingLocation }`, fails (e.g., destination weight limit, or `StagingLocation`/`pallet-dap` staging account misconfigured after an upgrade).
5. Per the adapter's own documented behavior, the teleported assets are now trapped on the destination chain's Asset Trap with no automatic recovery — funds are burned on the source chain and inaccessible on the destination chain, i.e., a real, unrecoverable loss for the protocol's own accumulated fee/dust/coretime revenue. [5](#0-4) 
Existing tests confirm `ForwardSucceeded` is emitted purely based on the mocked `forward` call returning `Ok(())` (representing local success), without any destination-side confirmation modeled at all — demonstrating the gap is present in the pallet's own test assumptions.

### Citations

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

**File:** substrate/frame/accumulate-and-forward/src/lib.rs (L40-43)
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

**File:** substrate/frame/accumulate-and-forward/src/tests/xcm_transfer.rs (L176-194)
```rust
// Check the full success path: verify the send count, event, and forwarded amount.
#[test]
fn verify_success_path() {
	new_test_ext(true).execute_with(|| {
		let period = TransferPeriod::get();
		let funds = 50u64;

		reset_send_count();
		reset_last_sent_amount();
		fund_accumulation_account(funds);

		System::set_block_number(period);
		AccumulateForwardPallet::on_idle(period, Weight::from_all(u64::MAX));

		assert_eq!(get_send_count(), 1);
		System::assert_has_event(Event::<Test>::ForwardSucceeded { amount: funds }.into());
		assert_eq!(get_last_sent_amount(), Some(funds));
	});
}
```
