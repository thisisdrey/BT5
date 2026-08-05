## Title
Accumulate-and-Forward teleport can burn pooled funds on-source while destination rejection permanently traps them with no refund path - ([File: polkadot/xcm/xcm-builder/src/forwarder.rs])

## Summary
`pallet_accumulate_and_forward` pools protocol-level inflows (tx fees, dust, coretime revenue) into an accumulation account and periodically forwards them via `TeleportForwarderForAccountId32::forward`, invoked from `on_idle`. The forwarder burns the funds locally (teleport-out, decrementing total issuance) and fires an XCM program to the destination that expects `ReceiveTeleportedAsset` + `AliasOrigin` + `DepositAsset` to succeed there. The adapter's own doc comment states plainly: *"Once the local executor reports success, the message is queued and any destination-side rejection results in trapped assets at the destination with no automatic recovery path."* This mirrors the WeVE finding's core defect: value is irrevocably destroyed on the source side of a two-phase cross-domain settlement before the counterpart phase is guaranteed to succeed, and there is no compensating refund mechanism if the second phase fails. [1](#0-0) [2](#0-1) 

## Finding Description
`Pallet::on_idle` reads the accumulation account's reducible balance and calls `T::Forwarder::forward(accumulation_account, available_funds)`, treating the outcome as a simple `Ok`/`Err`: [3](#0-2) 

The concrete `Forwarder` implementation, `TeleportForwarderForAccountId32::forward`, builds an XCM program that does `WithdrawAsset` (burns locally via teleport check-out) then `InitiateTransfer` with a `Teleport` filter, wrapped in a `with_transaction` block that only inspects `Outcome::Complete` vs. anything else for the *local* execution: [4](#0-3) 

`Outcome::Complete` from `prepare_and_execute` only certifies that local instruction processing (withdraw, burn/check-out, message enqueue for delivery) succeeded — it says nothing about whether the destination chain will actually process `ReceiveTeleportedAsset { AliasOrigin(source) ; UnpaidExecution ; DepositAsset }` successfully. Once the local XCM commits, the burn (decrement of total issuance) is final; the corresponding mint only happens if the destination processes the remote program without error: [5](#0-4) 

If the destination rejects the message for any operationally-realistic reason — e.g. `UnpaidExecution`'s barrier check fails, the `AliasOrigin` alias is not permitted for that origin under the destination's `Aliasers` config, the destination `IsTeleporter`/asset filter does not (yet) recognize the reanchored asset for that specific account context, `DepositAsset` fails because the beneficiary/staging location cannot receive it, or the destination is simply congested/halted — the XCM instruction there errors out. As documented, this manifests as trapped assets at the destination with *no automatic recovery path back to the source*: exactly the WeVE pattern of "burn confirmed, counterpart mint failed, no refund."

This differs from the ordinary local-execution guarantee provided by `FrameTransactionalProcessor`/`transactional_process` inside `xcm-executor`, which only rolls back the **local** holding/fees registers and storage changes for a single chain's instruction sequence within one transaction. It cannot and does not roll back a *remote* chain's rejection of a message that was already delivered, because that happens in a different block/consensus context entirely — there is no atomic two-phase commit across the teleport boundary. [6](#0-5) 

## Impact Explanation
This directly matches the required impact class "permanent user-fund ... lock" / broken settlement invariant: message queues and settlement state must only advance after execution succeeds atomically on both legs, but here the source-side burn (total-issuance decrement) is final and irreversible the moment local `Outcome::Complete` is returned, while destination-side success is not guaranteed and not verified before that commit. Funds pooled from protocol-level inflows (fees, dust, coretime revenue) — ultimately backing user-relevant chain economics — are burned with total-issuance impact and can become permanently unrecoverable trapped assets at the destination with `ForwardSucceeded` incorrectly emitted (the pallet only distinguishes `forward()` returning `Ok`/`Err`, and `Ok` is returned as soon as local execution completes, before destination settlement is known). [7](#0-6) 

## Likelihood Explanation
This is not contingent on a malicious relayer, validator, or governance actor — it fires automatically and unprivileged from `on_idle` every `TransferPeriod` whenever accumulated funds exceed `MinTransferAmount`. It requires no attacker at all: any transient destination-side condition (barrier misconfiguration, congestion, an `AliasOrigin`/`IsTeleporter` mismatch introduced by a runtime upgrade on either side, or the staging location temporarily rejecting deposits) triggers the loss. Because it runs unconditionally in normal chain operation with no acknowledgement/receipt loop back to the source, the loss is not merely theoretical — it is the explicitly documented behavior of the adapter itself.

## Recommendation
- Do not treat local `Outcome::Complete` as final settlement; require an explicit delivery/settlement confirmation (e.g. a `QueryResponse`/receipt XCM back to the source, or a two-phase reserve-based transfer instead of teleport) before finalizing burn/issuance changes, or delay the burn until confirmation.
- Alternatively, keep the burned funds' accounting reversible: retain a pending-forward record and re-mint locally if a bounded timeout elapses without destination confirmation.
- At minimum, monitor `AssetsTrapped` events/proofs at the destination and wire an automated (or governance-triggered) claim-and-return flow so trapped assets are not permanently unrecoverable, closing the gap acknowledged in the forwarder's own documentation.

## Proof of Concept
1. Fund the accumulation account above `MinTransferAmount` on the source chain (e.g. Collectives/Coretime/People/BridgeHub Westend, all of which wire `pallet_accumulate_and_forward` with `TeleportForwarderForAccountId32`). [8](#0-7) 
2. At a block that is a multiple of `TransferPeriod`, `on_idle` calls `Forwarder::forward`, which executes `WithdrawAsset` + `InitiateTransfer(Teleport(...))` locally; local execution reports `Outcome::Complete`, the funds are burned (total issuance reduced), `ForwardSucceeded` is emitted, and the XCM message is enqueued for delivery to `AssetHub`. [9](#0-8) 
3. On the destination (`AssetHub`), suppose the remote program `ReceiveTeleportedAsset → AliasOrigin(source) → UnpaidExecution → DepositAsset` fails any single check on arrival (e.g. `AliasOrigin` not permitted for that particular `source` under the destination's alias configuration, or `DepositAsset`'s beneficiary/staging account rejecting the deposit). The XCM executor there reports an incomplete/error outcome for that message.
4. Per the forwarder's own documented behavior, the assets are trapped at the destination with no automatic recovery path back to the source chain that burned them — the source's total issuance has already been permanently reduced and `ForwardSucceeded` was already recorded, with no corresponding `ForwardFailed`/refund path triggered, reproducing the WeVE "burn succeeds, counterpart mint fails, no refund" invariant break. [10](#0-9)

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

**File:** substrate/frame/accumulate-and-forward/src/lib.rs (L18-43)
```rust
//! # Accumulate-and-Forward Pallet
//!
//! Intercepts configurable token inflows (transaction fees, dust removal, coretime revenue) on
//! system parachains and gathers them in a local accumulation account for periodic forwarding
//! to a configurable destination.
//!
//! ## Usage
//!
//! - **Fees**: Use [`DealWithFeesSplit`] to split fees between accumulation and other handlers
//! - **Burns/Revenue**: Use the pallet as `OnUnbalanced<CreditOf>` handler (e.g., dust removal,
//!   coretime revenue)
//! Note: Direct calls to `pallet_balances::Pallet::burn()` extrinsic are not redirected to
//! the accumulation account — they still reduce total issuance directly.
//!
//! ## Setup
//!
//! The accumulation account must be pre-funded with at least the existential deposit.
//! For new chains, include the account in the balances genesis config.
//! For existing chains, fund it via a manual transfer.
//!
//! If the accumulation account is not pre-funded, deposits below ED will be silently burned.
//!
//! ## Total Issuance
//!
//! Accumulated funds are burnt upon forwarding (reducing `total_issuance` here) and the same
//! funds are minted at the destination when the sent message is received.
```

**File:** substrate/frame/accumulate-and-forward/src/lib.rs (L152-201)
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

			meter.consumed()
		}
```

**File:** polkadot/xcm/xcm-executor/src/lib.rs (L905-933)
```rust
	/// Execute `f` inside a transactional context that backs up and restores `holding` and
	/// `fees` on failure.
	fn transactional_process(
		&mut self,
		f: impl FnOnce(&mut Self) -> Result<(), XcmError>,
	) -> Result<(), XcmError> {
		self.transactional_process_with_custom_rollback(f, |_| {})
	}

	/// Like [`Self::transactional_process`], but also calls `on_rollback` when the
	/// transaction is rolled back.
	///
	/// NOTE: holding and fees registers are already automatically rolled back. Custom handler
	/// is for _extra_ rollback logic.
	fn transactional_process_with_custom_rollback(
		&mut self,
		f: impl FnOnce(&mut Self) -> Result<(), XcmError>,
		on_rollback: impl FnOnce(&mut Self),
	) -> Result<(), XcmError> {
		let mut backup_holding = BackupAssetsInHolding::safe_backup(&self.holding);
		let mut backup_fees = BackupAssetsInHolding::safe_backup(&self.fees);
		let result = Config::TransactionalProcessor::process(|| f(self));
		if Config::TransactionalProcessor::IS_TRANSACTIONAL && result.is_err() {
			backup_holding.restore_into(&mut self.holding);
			backup_fees.restore_into(&mut self.fees);
			on_rollback(self);
		}
		result
	}
```

**File:** cumulus/parachains/runtimes/collectives/collectives-westend/src/lib.rs (L698-711)
```rust
impl pallet_accumulate_and_forward::Config for Runtime {
	type Currency = Balances;
	type PalletId = AccumulateForwardPalletId;
	type Forwarder = xcm_builder::TeleportForwarderForAccountId32<
		xcm_config::XcmConfig,
		xcm_config::AssetHub,
		xcm_config::WndLocation,
		DapStagingLocation,
	>;
	type TransferPeriod = ForwardPeriod;
	type MinTransferAmount = MinForwardAmount;
	type BlockNumberProvider = RelaychainDataProvider<Runtime>;
	type WeightInfo = weights::pallet_accumulate_and_forward::WeightInfo<Runtime>;
}
```
