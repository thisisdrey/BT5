### Title
Accumulate-and-forward pallet burns funds locally before remote execution succeeds, permanently trapping value with no recovery path - (File: `polkadot/xcm/xcm-builder/src/forwarder.rs`)

### Summary
`pallet_accumulate_and_forward` periodically sweeps fees/dust/coretime-revenue that have piled up in a local "accumulation account" and teleports them to a destination chain via `TeleportForwarderForAccountId32::forward` [1](#0-0) . The pallet's own docs state the accounting model explicitly: "Accumulated funds are burnt upon forwarding (reducing `total_issuance` here) and the same funds are minted at the destination when the sent message is received" [2](#0-1) . This is structurally the same "burn now, credit later" pattern flagged in the external report: the source-side burn (an irreversible supply reduction) is treated as settled as soon as local execution completes, while the corresponding mint/settlement on the other side is a separate, unguaranteed step.

### Finding Description
`TeleportForwarderForAccountId32::forward` builds and locally executes an XCM program that withdraws/teleports the asset out (burning it on the source chain, since teleport-out reduces local `total_issuance`) and then queues a remote program (`AliasOrigin(source)` + `DepositAsset`) to run at the destination [3](#0-2) . The implementation's own doc comment concedes the atomicity gap: "Local-execution failures roll back all local state changes. Once the local executor reports success, the message is queued and any destination-side rejection results in trapped assets at the destination with no automatic recovery path." [4](#0-3) 

Because `preserve_origin: true` is set on `InitiateTransfer`, the destination program is prefixed with `AliasOrigin(source_location)` before the caller-supplied `remote_xcm` (`DepositAsset`) runs [5](#0-4) . The source location is a `PalletId`-derived internal accumulation account — an address the destination chain's `Aliasers`/origin-conversion configuration has no special reason to recognize or trust. If the destination's alias/origin policy does not authorize aliasing from that particular sovereign/derived account (which is the common, restrictive default for aliasing filters), the remote program fails at `AliasOrigin`, *after* `ReceiveTeleportedAsset` has already minted the teleported value into the destination's XCM holding register. The remaining `DepositAsset` instruction never runs, the asset ends up trapped (`AssetsTrapped`) at the destination, and — per the code's own comment — there is no automatic recovery path.

Meanwhile, on the source chain, the burn already happened and was committed (`with_transaction` only rolls back on *local* execution failure, not on remote rejection) [6](#0-5) , so `total_issuance` permanently reflects the burned amount while the minted value at the destination is stuck in an unclaimable trap, exactly mirroring the reported bug class: an accounting invariant (issued supply corresponds to circulating/claimable value) is broken because a burn is settled unconditionally while the matching credit is contingent on a step that is not guaranteed to succeed, and for which no rightful party has the means to claim.

### Impact Explanation
This breaks the "settle exactly once, atomically" invariant for bridge/message value transfer: value is destroyed on one side of the pipe without a guaranteed, atomic credit on the other side. Depending on destination-side alias/origin configuration, this can result in permanent, unrecoverable loss of the swept funds (fees, dust, coretime revenue) for every forward cycle that hits this condition, not merely a one-off griefing scenario, and the pallet is designed to run automatically and repeatedly via `on_idle` on live system chains (Westend Bridge Hub, Collectives, Coretime, People, and the relay chain itself, per the various runtime configs wiring in `TeleportForwarderForAccountId32`).

### Likelihood Explanation
No malicious actor, validator, or governance action is required — this is a pure protocol/config interaction triggered by the pallet's normal periodic `on_idle` operation once `MinTransferAmount` accumulates. It manifests whenever the destination's XCM configuration does not authorize `AliasOrigin` for the specific sovereign-derived source account used by the accumulation pallet (a plausible and easy-to-hit misconfiguration/version-mismatch scenario across the several system-chain runtimes that wire this pallet in, each independently configuring `Dest`, `NativeAsset`, and `StagingLocation`). The severity is amplified because the pallet's own author-written doc comment already acknowledges the exact failure mode and states there is no automatic recovery.

### Recommendation
Do not commit the local burn/teleport-out until destination-side settlement is confirmed, or make the remote program self-recovering: avoid `AliasOrigin` dependence on an unauthorized/untrusted derived account, or add a `SetAppendix`/`SetErrorHandler` in the remote XCM that deposits trapped assets to a claimable, chain-known beneficiary (e.g., directly to the destination's known staging location without requiring alias authorization) instead of relying on `preserve_origin`. At minimum, ensure the destination chain's `Aliasers` configuration is verified to authorize the specific accumulation-account origin before this Forwarder is wired into any runtime, and add an automated recovery/claim path (e.g., a permissioned `claim_assets` call keyed to the known trapped-asset hash) so that a destination-side rejection cannot result in irrecoverable fund loss.

### Proof of Concept
1. Deploy two chains, A (source, running `pallet_accumulate_and_forward` with `TeleportForwarderForAccountId32`) and B (destination), where B's XCM executor `Aliasers` type does not authorize `AliasOrigin` requests originating from A's `AccumulateForwardPalletId`-derived account.
2. Accumulate ≥ `MinTransferAmount` in A's accumulation account (e.g., via transaction fees routed through `DealWithFeesSplit`).
3. Let `on_idle` fire at a `TransferPeriod` boundary; `forward()` runs the local program (`WithdrawAsset` + `InitiateTransfer` with `Teleport`), which succeeds locally, burning the amount from A's `total_issuance`, and queues the remote program to B [7](#0-6) .
4. On B, `ReceiveTeleportedAsset` mints the value into the XCM holding register, then `AliasOrigin(source)` is rejected by B's origin/alias policy, causing the remainder of the program (`DepositAsset`) to never execute.
5. Observe `AssetsTrapped` on B and confirm `total_issuance` on A remains permanently reduced by the forwarded amount, with the corresponding value now unclaimable on B — reproducing the existing unit test's confirmation that local rollback works only for *local* failures [8](#0-7)  while providing no equivalent guarantee/rollback for *remote* rejections.

### Citations

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

**File:** polkadot/xcm/xcm-builder/src/tests/forwarder.rs (L169-200)
```rust
/// Verify that when the XCM router fails (simulating a mid-program failure after `WithdrawAsset`
/// has debited the source account), `forward` rolls back all storage changes via
/// `with_transaction`. Both the source balance and total issuance must remain unchanged.
#[test]
fn forward_rolls_back_balance_and_issuance_on_xcm_failure() {
	let source: AccountId = AccountId32::from([1u8; 32]);
	let initial_balance = 1_000u128;

	new_test_ext(source.clone(), initial_balance).execute_with(|| {
		let initial_issuance = Balances::total_issuance();

		ROUTER_SHOULD_FAIL.with(|f| f.set(true));
		let result = TeleportForwarderForAccountId32::<
			TestXcmConfig,
			AssetHubLocation,
			HereLocation,
			AccumulationInterior,
		>::forward(source.clone(), 500u128);
		ROUTER_SHOULD_FAIL.with(|f| f.set(false));

		assert!(result.is_err(), "expected forward to fail when router fails");
		assert_eq!(
			Balances::balance(&source),
			initial_balance,
			"source balance must be unchanged after XCM failure"
		);
		assert_eq!(
			Balances::total_issuance(),
			initial_issuance,
			"total issuance must be unchanged after XCM failure"
		);
	});
```
