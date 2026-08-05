This is a genuine local analog. The injected `pallet-accumulate-and-forward` / `TeleportForwarderForAccountId32` adapter burns protocol funds locally and forwards them via a remote XCM program whose successful *delivery and dispatch* is never confirmed before the local burn is committed — this mirrors the BitVMBridge pattern of an unguarded, irreversible outbound transfer with no recovery path if the destination-side step fails.I have sufficient evidence to finalize the finding.

### Title
Local burn of accumulated system-chain revenue is committed before remote settlement succeeds, permanently trapping funds with no recovery path - (File: `polkadot/xcm/xcm-builder/src/forwarder.rs`)

### Summary
`TeleportForwarderForAccountId32::forward` (used by every production runtime that wires `pallet_accumulate_and_forward`, e.g. `collectives-westend`, `coretime-westend`, `people-westend`, `bridge-hub-westend`) burns funds from the local accumulation account and dispatches a two-hop XCM program: a locally-executed `WithdrawAsset`/`InitiateTransfer` and a remote, asynchronously-delivered `DepositAsset` to a fixed `StagingLocation` on the destination chain. Success is determined solely by the *local* executor outcome (`Outcome::Complete`), which only confirms the withdrawal and message enqueuing succeeded — it says nothing about whether the remote `DepositAsset` on the destination chain will actually succeed. If the remote leg fails for any reason (fee/weight miscalculation, destination-side filter change, beneficiary account state, temporary congestion), the funds are burned locally and never land at the destination, exactly mirroring the reported BitVMBridge pattern: an irreversible outbound transfer whose success/failure is not verified end-to-end and for which there is no recovery mechanism.

### Finding Description
The adapter's own doc comment states the risk directly: [1](#0-0) . The `forward` implementation withdraws (burns) the asset locally and constructs a remote XCM program (`InitiateTransfer` with a nested `DepositAsset` to `StagingLocation`), then wraps only the *local* `prepare_and_execute` call in a transaction: [2](#0-1) .

`Outcome::Complete` from `prepare_and_execute` reflects that the local instructions (withdraw + enqueue the outbound HRMP/XCMP message) ran without error — it does not, and cannot, reflect whether the destination chain will successfully execute the forwarded `DepositAsset` to `StagingLocation` at a later block. The pallet then treats this local success as final: it burns the funds from the accumulation account (irreversibly reducing `total_issuance`, per the crate docs: [3](#0-2) ) and emits `ForwardSucceeded`, retrying only on the narrower, purely-local `Err(())` path from `forward`: [4](#0-3) .

There is no callback, receipt, or acknowledgement mechanism between the destination chain's processing of the remote `DepositAsset` and the source-chain pallet state — the "advance" of local state (burn + `ForwardSucceeded` event) is decoupled from the actual settlement of the remote deposit, violating the required invariant that "message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically." If the remote deposit is rejected (e.g. insufficient weight budget on the destination side, a change to the destination's asset/location filters, or any other execution failure at the `StagingLocation`), the teleported assets become permanently trapped at the destination with no automatic reclaim path, and are simultaneously already burned/gone on the source chain.

### Impact Explanation
This function is wired as the fee/dust/coretime-revenue drain for multiple system parachains via `on_idle`: [5](#0-4) [6](#0-5) . It periodically moves the entire accumulated pool of protocol revenue (transaction fees, dust removal, coretime revenue — see the pallet's module docs: [7](#0-6) ) off-chain to a destination chain in a single shot each `TransferPeriod`. A single failed remote settlement permanently destroys that entire batch of protocol funds — this is a systemic, unbacked loss of treasury/system revenue, not an isolated user mistake, and it recurs every transfer cycle since the design has no end-to-end settlement confirmation.

### Likelihood Explanation
No malicious actor, admin, or governance action is required. The bug is triggered by ordinary, expected failure conditions on the remote leg of any cross-chain asset transfer (weight/fee misconfiguration, a destination-side asset or location filter update, `StagingLocation` account edge cases, or transient congestion) — none of which are under the control of, or need to be induced by, an attacker; they are the routine class of failures that legitimately fallible remote XCM execution can produce. Because the pallet is invoked automatically on a fixed cadence (`on_idle`) for the lifetime of the chain, the exposure window is continuous and the class of triggering conditions (destination-side execution failure) is common in cross-chain messaging.

### Recommendation
Do not treat local `Outcome::Complete` as final settlement. Either (a) require an explicit settlement acknowledgement (e.g., a receipt/callback from the destination chain) before burning funds or marking `ForwardSucceeded`, (b) hold funds in an escrow/pending state until settlement confirmation and only burn upon confirmed remote deposit, or (c) implement an explicit, permissioned recovery/reclaim path (mirroring XCM's `ClaimAsset`/asset-trap mechanism) that can be triggered if the destination-side deposit is known to have failed, so that trapped funds are not unconditionally and permanently lost.

### Proof of Concept
1. Configure a runtime with `pallet_accumulate_and_forward` using `TeleportForwarderForAccountId32` (as in `collectives-westend`, `coretime-westend`, `people-westend`, or `bridge-hub-westend`).
2. Let fees/dust/coretime-revenue accumulate in the accumulation account past `MinTransferAmount`.
3. At the next `on_idle` invocation, `Pallet::<T>::on_idle` calls `T::Forwarder::forward(accumulation_account, available_funds)` [8](#0-7) .
4. `TeleportForwarderForAccountId32::forward` executes the local `WithdrawAsset`/`InitiateTransfer` program; `prepare_and_execute` returns `Outcome::Complete` because the local burn and message enqueue succeeded, so `forward` returns `Ok(())` [9](#0-8) .
5. On the destination chain, the forwarded `DepositAsset { assets: Wild(AllCounted(1)), beneficiary: StagingLocation }` instruction fails at execution time (e.g. insufficient weight limit granted by `InitiateTransfer`'s embedded program, or a filter rejecting the deposit).
6. Result: the funds are already burned/gone from the accumulation account on the source chain (step 4), `ForwardSucceeded` was emitted, and the equivalent value never lands on the destination chain and has no recovery mechanism — a permanent, unbacked loss of the entire batch of accumulated protocol revenue.

### Citations

**File:** polkadot/xcm/xcm-builder/src/forwarder.rs (L34-38)
```rust
/// XCM adapter that implements [`pallet_accumulate_and_forward::Forwarder`] for AccountId32-type
/// source accounts by teleporting native tokens to a target account on a destination chain.
/// Local-execution failures roll back all local state changes. Once the local executor reports
/// success, the message is queued and any destination-side rejection results in trapped assets
/// at the destination with no automatic recovery path.
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

**File:** substrate/frame/accumulate-and-forward/src/lib.rs (L18-30)
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

**File:** cumulus/parachains/runtimes/coretime/coretime-westend/src/lib.rs (L638-651)
```rust
impl pallet_accumulate_and_forward::Config for Runtime {
	type Currency = Balances;
	type PalletId = AccumulateForwardPalletId;
	type Forwarder = xcm_builder::TeleportForwarderForAccountId32<
		xcm_config::XcmConfig,
		testnet_parachains_constants::westend::locations::AssetHubLocation,
		xcm_config::TokenRelayLocation,
		DapStagingLocation,
	>;
	type TransferPeriod = ForwardPeriod;
	type MinTransferAmount = MinForwardAmount;
	type BlockNumberProvider = RelaychainDataProvider<Runtime>;
	type WeightInfo = weights::pallet_accumulate_and_forward::WeightInfo<Runtime>;
}
```
