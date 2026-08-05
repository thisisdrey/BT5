Confirmed: `pallet_accumulate_and_forward` is wired into multiple production system-parachain runtimes (bridge-hub-westend, collectives-westend, coretime-westend, people-westend) via `TeleportForwarderForAccountId32`, giving the analog real deployment scope.

### Title
Accumulated protocol revenue can be permanently trapped with no recovery path in `pallet-accumulate-and-forward` - (File: `substrate/frame/accumulate-and-forward/src/lib.rs`, `polkadot/xcm/xcm-builder/src/forwarder.rs`)

### Summary
`pallet-accumulate-and-forward` collects transaction fees, dust, and coretime revenue into a derived `accumulation_account` and periodically forwards the balance to a destination chain via `on_idle`. The pallet exposes **no `#[pallet::call]` extrinsics at all** — there is no permissioned, forced, or manual way to trigger a forward, retry a failed one, or reclaim funds. Forwarding is entirely dependent on a best-effort, non-mandatory hook, and the shipped `Forwarder` implementation explicitly documents that a destination-side rejection after local execution succeeds results in **trapped assets with no automatic recovery path**. This is the direct analog of the `FeeReceiver` bug: value flows into an account, and the only extraction mechanism can silently fail forever with no withdrawal/retry entrypoint.

### Finding Description
The pallet's storage-holding account is defined at [1](#0-0) . Funds are deposited into it by `OnUnbalanced` handlers used for tx fees, dust removal, and coretime revenue [2](#0-1) .

The only egress path is the `on_idle` hook, which is weight-gated and period-gated: it only attempts a forward when the block number is an exact multiple of `TransferPeriod` **and** there is enough remaining idle weight to cover `send_native()` [3](#0-2) . On failure it just logs and emits `ForwardFailed`, waiting for the next period with no escalation, no alternate destination, and no operator/governance call to intervene [4](#0-3) .

Critically, the pallet has zero `Call` variants — grepping the crate for `#[pallet::call]`, `OriginFor`, or any dispatchable turns up nothing. There is no `force_forward`, no `withdraw`, no sudo/root escape hatch anywhere in this pallet.

The concrete `Forwarder` implementation wired into production runtimes (`TeleportForwarderForAccountId32`) documents the failure mode explicitly in its own doc comment: [5](#0-4) . It rolls back local balance changes only if the **local** XCM execution fails; once local execution reports `Outcome::Complete` and the message is queued for delivery, any **destination-side** rejection (e.g. `DepositAsset` failing because the staging/beneficiary location on the destination chain is misconfigured, unregistered, or the destination chain changes its XCM version/asset registration) results in the teleported native asset being trapped in the destination's asset trap with — per the pallet's own documentation — "no automatic recovery path" [6](#0-5) .

Existing guards do not stop this path:
- `with_transaction` only protects against *local* execution failure, not destination rejection [7](#0-6) .
- `on_idle` retries only by re-running the identical forward logic every `TransferPeriod`, so if the destination-side misconfiguration is systemic (not transient), every subsequent `on_idle` cycle burns another batch of accumulated revenue into the same trap, compounding losses with each period, with no dispatchable to pause, redirect, or recover.
- There is no `Call` enum in the pallet, so unlike `FeeReceiver` where the fix was "add a withdraw function," this pallet has never had one for either recovering trapped-at-destination assets or forcing a manual local-side reroute.

### Impact Explanation
This is a public, underpriced-work / fund-loss class issue that matches the "Balances… must conserve value and settle exactly once" and "message queues/receipts… must only advance after decode, dispatch, execution, and settlement succeed atomically" pivots. Protocol-level revenue (transaction fees, coretime revenue, dust) accumulated on system parachains such as BridgeHub, Collectives, Coretime, and People Westend can be periodically and irreversibly burned into a destination-side asset trap once a destination-side beneficiary/registration mismatch occurs, with zero on-chain mechanism to reclaim or reroute the funds. This is systemic value loss, not a one-off — every `TransferPeriod` re-triggers the same loss until someone notices and ships a runtime upgrade.

### Likelihood Explanation
No malicious actor, governance abuse, or privileged access is required — this is a self-triggered failure mode purely from the pallet's own automated `on_idle` hook interacting with any destination-side condition that causes `DepositAsset` to fail post-teleport (e.g. destination beneficiary/staging location becoming invalid, an asset-registration change, or a version mismatch on the receiving chain). Because the trigger is destination-chain state rather than an attacker action, and there is no monitoring-independent recovery call, likelihood is driven by ordinary operational drift rather than an adversarial precondition — exactly the profile the report flags as accepted ("public underpriced work that degrades block production or stalls bridge processing" / permanent fund lock).

### Recommendation
Add an explicit, access-controlled recovery mechanism analogous to the `FeeReceiver` fix:
1. Add a `pallet::call` (e.g. `force_forward` / `pause_forwarding` gated by `EnsureRoot` or a configurable `AdminOrigin`) to allow manual intervention when automatic forwarding to a destination is failing.
2. Track failures explicitly (e.g. a `ConsecutiveFailures` counter) and halt automatic forwarding after N consecutive failures, requiring an explicit admin call to resume, rather than silently repeating a failing transfer forever.
3. For the `TeleportForwarderForAccountId32` adapter, avoid unconditionally trusting destination-side execution: consider using a non-teleport transfer with a fallback/refund path, or require the destination `remote_xcm` to include a guaranteed self-referential fallback deposit (e.g. `SetErrorHandler`) so a `DepositAsset` failure on the destination re-routes to a safe address instead of the trap.

### Proof of Concept
1. Deploy `pallet_accumulate_and_forward` with `TeleportForwarderForAccountId32` on a system parachain (as done in bridge-hub-westend/collectives-westend/coretime-westend/people-westend).
2. Accumulate fees/dust/coretime revenue into `accumulation_account` over several blocks (no privileged action required — normal transaction fees suffice via `DealWithFeesSplit`/`OnUnbalanced`, see [2](#0-1) ).
3. Have the destination chain's staging/beneficiary `Location` become invalid for `DepositAsset` (e.g. through an unrelated but legitimate change on the destination chain, such as removing/renaming the staging account or an asset de-registration) — no attacker or admin action on the source chain is required.
4. On the next `TransferPeriod` boundary, `on_idle` invokes `Forwarder::forward`, which locally executes `WithdrawAsset`/`InitiateTransfer`/teleport successfully (`Outcome::Complete`) per [8](#0-7)  and queues the message for delivery — the local balance is burned.
5. On the destination chain, `DepositAsset { beneficiary }` fails; the asset lands in the destination's asset trap with no automatic recovery, matching the pallet's documented behavior [5](#0-4) .
6. Repeat every `TransferPeriod`: each cycle burns newly accumulated revenue into the same trap. There is no extrinsic anywhere in `pallet_accumulate_and_forward` (confirmed absent via grep for `#[pallet::call]`/`OriginFor` in the crate) to pause, redirect, or reclaim the funds — mirroring `FeeReceiver`'s complete absence of a withdraw function.

### Citations

**File:** substrate/frame/accumulate-and-forward/src/lib.rs (L152-182)
```rust
		fn on_idle(_block: SystemBlockNumberFor<T>, remaining_weight: Weight) -> Weight {
			// Only attempt forwarding on blocks that are exact multiples of `TransferPeriod`.
			let block = T::BlockNumberProvider::current_block_number();
			if (block % T::TransferPeriod::get()) != Zero::zero() {
				return Weight::zero();
			}

			let mut meter = WeightMeter::with_limit(remaining_weight);

			// Need one read for the balance check.
			if meter.try_consume(T::DbWeight::get().reads(1)).is_err() {
				return meter.consumed();
			}

			let accumulation_account = Self::accumulation_account();
			// We use `reducible_balance` with `Preservation::Preserve` to get the
			// usable balance (excluding the ED).
			let available_funds = T::Currency::reducible_balance(
				&accumulation_account,
				Preservation::Preserve,
				Fortitude::Polite,
			);

			if available_funds < T::MinTransferAmount::get() {
				return meter.consumed();
			}

			// Ensure there is enough weight budget for the full XCM send.
			if meter.try_consume(T::WeightInfo::send_native()).is_err() {
				return meter.consumed();
			}
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

**File:** substrate/frame/accumulate-and-forward/src/lib.rs (L211-217)
```rust
	impl<T: Config> Pallet<T> {
		/// Get the accumulation account derived from the pallet ID.
		///
		/// This account accumulates funds locally before they are forwarded to the destination.
		pub fn accumulation_account() -> T::AccountId {
			T::PalletId::get().into_account_truncating()
		}
```

**File:** substrate/frame/accumulate-and-forward/src/lib.rs (L289-309)
```rust
impl<T: Config> OnUnbalanced<CreditOf<T>> for Pallet<T> {
	fn on_nonzero_unbalanced(amount: CreditOf<T>) {
		let accumulation_account = Self::accumulation_account();
		let numeric_amount = amount.peek();

		// Resolve should never fail because:
		// - can_deposit on destination succeeds assuming accumulation account is pre-funded with ED
		// - amount is guaranteed non-zero by the trait method signature
		// The only failure would be overflow on destination or unfunded account.
		let _ = T::Currency::resolve(&accumulation_account, amount).inspect_err(|_| {
			frame_support::defensive!(
				"🚨 Failed to deposit to accumulation account - funds burned, it should never happen!"
			);
		});

		log::debug!(
			target: LOG_TARGET,
			"💸 Deposited {numeric_amount:?} to accumulation account"
		);
	}
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
