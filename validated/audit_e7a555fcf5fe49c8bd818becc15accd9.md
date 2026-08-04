## Title
Snowbridge Inbound Queue V2 permanently marks a message `Nonce` as consumed and pays the relayer reward before the cross-chain asset transfer to Asset Hub is guaranteed to succeed - ([File: bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs])

### Summary
`Pallet::process_message` in the Snowbridge V2 inbound queue sets the message `Nonce` bitmap and pays out the relayer reward/tip in the *same* extrinsic that only *enqueues* an XCM message to Asset Hub — it never waits for, or checks, whether that XCM actually executes successfully on the destination chain. This mirrors the reported Wormhole/NEAR bug class: "mark work as done" and "actually perform the work" are decoupled across an asynchronous boundary (here: cross-chain XCMP delivery instead of NEAR receipts), and once the marker is set there is no retry path.

### Finding Description
`process_message` does the following, in order: [1](#0-0) 

1. `ensure!(!Nonce::<T>::get(nonce), ...)` then immediately `Nonce::<T>::set(nonce)` — the nonce bitmap is a permanent, one-way "used" flag (`SparseBitmapImpl`) with no un-set/retry path.
2. It then calls `T::MessageProcessor::process_message`, which (for the real runtime wiring, `XcmMessageProcessor`) only **converts the message to XCM and hands it to `SendXcm`/`Sender::deliver`** — i.e., it queues the message into XCMP for delivery to Asset Hub: [2](#0-1) 

`Sender::deliver(ticket)` only guarantees the message is *queued*; the actual `ReserveAssetDeposited`/`WithdrawAsset`/`DepositAsset` instructions that move value to the beneficiary run **asynchronously, in a different block, on a different chain** (Asset Hub) when it services its XCMP queue.
3. Back in `process_message`, once `MessageProcessor::process_message` returns `Ok` (meaning only "successfully queued", not "successfully executed remotely"), the pallet pays the relayer fee/tip and emits `MessageReceived`: [3](#0-2) 

Because the `Nonce` flag and the reward payment are finalized on BridgeHub the moment the XCM is merely *enqueued*, and the actual fund movement happens later on Asset Hub with no callback to BridgeHub, any failure on the Asset Hub side (insufficient `execution_fee` to cover `PayFees` for the appended, sender-supplied `remote_xcm` instructions, a malformed `remote_xcm` that fails to `DepositAsset` to the intended beneficiary, asset-hub-side barrier rejection, etc.) leaves:
- the `Nonce` permanently consumed (no way to ever resubmit that Ethereum event again — `ensure!(!Nonce::<T>::get(nonce), Error::<T>::InvalidNonce)` will reject it forever), and
- the relayer already paid.

The converter itself acknowledges this asynchronous failure mode by inserting an `AssetClaimer` hint before `PayFees` "in case the fees are not enough": [4](#0-3) 

but the claimer defaults only apply if `message.claimer` decodes correctly; the fallback claimer is the bridge's own sovereign account on Asset Hub, not the original beneficiary: [5](#0-4) 

and the arbitrary `remote_xcm` supplied in the message (`Payload::Raw`) is appended verbatim to the end of the program and executed with whatever assets are present in holding at that point: [6](#0-5) 

If that trailing arbitrary XCM fails to deposit to the correct beneficiary (e.g. mis-specified asset filter, insufficient weight, or Asset Hub-side XCM changes), the assets are trapped in the AssetTrap on Asset Hub while BridgeHub has already durably recorded the transfer as "received" and rewarded the relayer — with the `Nonce` map giving no mechanism to detect or reprocess the failure.

This is the direct on-chain analog of the reported bug: "marking as used" (Nonce set + reward paid) happens in a receipt/extrinsic that is decoupled from the actual execution of the transfer (remote XCM execution on Asset Hub), and the pallet has no atomic guarantee tying the two together.

### Impact Explanation
This falls under "permanent user-fund or bridge-state lock" and "duplicate settlement or payout" in the required impact set: a user's Ethereum-locked funds can never be minted/represented on the Polkadot side because the nonce can never be resubmitted, while the relayer collects payment regardless of whether the transfer completed. Unlike the messages pallet (`bridges/modules/messages`), which explicitly models "delivered" vs "dispatch result" separately for future-proofing retries at the application layer, the V2 inbound queue collapses "delivered/queued for XCM" and "irrevocably settled" into a single boolean per nonce with no compensating action.

### Likelihood Explanation
No privileged actor is required: the Ethereum-side message content (assets, `execution_fee`, `remote_xcm`, `claimer`) is set by whoever calls the permissionless Gateway contract on Ethereum, and any unprivileged relayer can then call the permissionless `submit` extrinsic on BridgeHub with a valid light-client proof for that event. An attacker (or an ordinary user who mis-estimates `execution_fee`/`remote_xcm` weight, or omits `claimer`) can trigger the failure mode with a legitimately-verified message; no forging of proofs or governance/admin action is needed. The condition ("insufficient execution_fee relative to destination-side XCM weight" or "remote_xcm doesn't deposit to the intended beneficiary") is plausible and reachable through normal message construction, making this a realistically triggerable path rather than a purely theoretical one.

### Recommendation
- Do not treat a message as fully settled (Nonce permanently consumed + reward paid) based solely on successful `SendXcm`/enqueue. Consider either: (a) requiring a delivery/execution receipt from Asset Hub before finalizing reward payment, or (b) enforcing that the fallback claimer always resolves to the message's actual beneficiary/origin rather than the bridge's sovereign account, and validating that `execution_fee` covers the full weight of `remote_xcm` execution before accepting the message, so failures can't silently trap funds.
- Consider decoupling "nonce accepted for processing" from "reward eligible" the way `pallet-bridge-messages` decouples delivery from dispatch-level result, so a downstream execution failure can be observed and, where possible, remediated instead of silently discarding the nonce forever.

### Proof of Concept
Conceptual PoC (cannot be executed here, but derivable directly from the code path above):
1. Attacker (as the Ethereum-side sender, which is a permissionless role) emits a Gateway event with a `Message` whose `execution_fee` is deliberately set lower than the weight cost of executing the appended `remote_xcm` on Asset Hub, and with `claimer: None` (or a claimer that doesn't map back to the true beneficiary).
2. Any relayer calls `EthereumInboundQueueV2::submit` with a valid beacon-light-client proof for this event; `process_message` is invoked:
   - `Nonce::<T>::set(nonce)` is executed (see `bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs:225`).
   - `XcmMessageProcessor::process_message` → `process_xcm` → `send_xcm` succeeds because it only validates/delivers the *envelope*, not the eventual execution (`bridges/snowbridge/primitives/inbound-queue/src/v2/processor.rs:54-73`).
   - Relayer reward/tip is paid (`lib.rs:234-239`) and `MessageReceived` is emitted.
3. Later, on Asset Hub, the delivered XCM executes: `PayFees` cannot cover the weight of the appended `remote_xcm`, or the `DepositAsset` filter in `remote_xcm` fails to match the beneficiary, so the transfer instructions abort; already-deposited assets are trapped in the AssetTrap with no usable claimer.
4. The original Ethereum sender's funds are now unrecoverable on the Polkadot side: `Nonce::<T>::get(nonce)` is permanently `true`, so `ensure!(!Nonce::<T>::get(nonce), Error::<T>::InvalidNonce)` rejects any resubmission of the same event forever, and the relayer has already been paid for a transfer that never completed.

### Citations

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs (L215-245)
```rust
		pub fn process_message(relayer: T::AccountId, message: Message) -> DispatchResult {
			// Verify that the message was submitted from the known Gateway contract
			ensure!(T::GatewayAddress::get() == message.gateway, Error::<T>::InvalidGateway);

			let (nonce, relayer_fee) = (message.nonce, message.relayer_fee);

			// Verify the message has not been processed
			ensure!(!Nonce::<T>::get(nonce), Error::<T>::InvalidNonce);

			// Mark message as received
			Nonce::<T>::set(nonce);

			let message_id = T::MessageProcessor::process_message(relayer.clone(), message)
				.map_err(|e| match e {
					MessageProcessorError::ProcessMessage(e) => e,
					MessageProcessorError::ConvertMessage(e) => Error::<T>::from(e).into(),
					MessageProcessorError::SendMessage(e) => Error::<T>::from(e).into(),
				})?;

			// Pay relayer reward
			let tip = Tips::<T>::take(nonce).unwrap_or_default();
			let total_tip = relayer_fee.saturating_add(tip);
			if total_tip > 0 {
				T::RewardPayment::register_reward(&relayer, T::DefaultRewardKind::get(), total_tip);
			}

			// Emit event with the message_id
			Self::deposit_event(Event::MessageReceived { nonce, message_id });

			Ok(())
		}
```

**File:** bridges/snowbridge/primitives/inbound-queue/src/v2/processor.rs (L54-73)
```rust
	pub fn process_xcm(
		who: T::AccountId,
		message: Message,
	) -> Result<XcmHash, MessageProcessorError> {
		// Convert the message to XCM
		let xcm = Converter::convert(message).map_err(|error| {
			tracing::error!(target: LOG_TARGET, ?error, "XCM conversion failed with error");
			MessageProcessorError::ConvertMessage(error)
		})?;

		// Forward XCM to a target location
		let dest = TargetLocation::get();
		let message_id = Self::send_xcm(dest.clone(), &who, xcm.clone()).map_err(|error| {
			tracing::error!(target: LOG_TARGET, ?error, ?dest, ?xcm, "XCM send failed with error");
			MessageProcessorError::SendMessage(error)
		})?;

		// Return the message_id
		Ok(message_id)
	}
```

**File:** bridges/snowbridge/primitives/inbound-queue/src/v2/converter.rs (L130-143)
```rust
		let claimer = message
			.claimer
			// Get the claimer from the message,
			.and_then(|claimer_bytes| Location::decode(&mut claimer_bytes.as_ref()).ok())
			// or use the Snowbridge sovereign on AH as the fallback claimer.
			.unwrap_or_else(|| {
				Location::new(
					0,
					[AccountId32 {
						network: Some(LocalNetwork::get()),
						id: bridge_owner.clone().into(),
					}],
				)
			});
```

**File:** bridges/snowbridge/primitives/inbound-queue/src/v2/converter.rs (L386-394)
```rust
		// Set claimer before PayFees, in case the fees are not enough. Then the claimer will be
		// able to claim the funds still.
		instructions.push(SetHints {
			hints: vec![AssetClaimer { location: message.claimer }]
				.try_into()
				.expect("checked statically, qed"),
		});

		instructions.push(PayFees { asset: message.execution_fee.clone() });
```

**File:** bridges/snowbridge/primitives/inbound-queue/src/v2/converter.rs (L413-425)
```rust
		// If the message origin is not the gateway proxy contract, set the origin to
		// the original sender on Ethereum. Important to be before the arbitrary XCM that is
		// appended to the message on the next line.
		if message.origin != GatewayProxyAddress::get() {
			instructions.push(DescendOrigin(
				AccountKey20 { key: message.origin.into(), network: None }.into(),
			));
		}

		// Add the XCM sent in the message to the end of the xcm instruction
		instructions.extend(message.remote_xcm.0);

		Ok(instructions.into())
```
