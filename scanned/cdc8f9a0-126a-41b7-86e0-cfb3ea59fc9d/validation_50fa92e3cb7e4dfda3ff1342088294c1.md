### Title
Strict sequential per-channel nonce in Snowbridge Inbound Queue V1 permanently jams the lane on any unconvertible message - (File: bridges/snowbridge/pallets/inbound-queue/src/lib.rs)

### Summary
`pallet-inbound-queue` (Snowbridge V1) enforces a strictly sequential, per-`ChannelId` nonce for messages relayed from Ethereum, exactly like the vulnerable `BridgedGovernor.lzReceive` pattern in the external report. The nonce is only advanced when the *entire* extrinsic succeeds. Because a real, unprivileged Ethereum-side event can legitimately encode a `VersionedMessage`/`Command` that permanently fails `ConvertMessage::convert` (e.g. `CannotReanchor`, `InvalidToken`, `InvalidDestination`), that specific nonce can never be committed. Since the pallet demands an exact `nonce == last + 1` match with no way to skip or force-advance, the channel becomes permanently unable to deliver any later message — an irrecoverable stuck lane, identical in root cause to the reported bug.

### Finding Description
`submit()` in `bridges/snowbridge/pallets/inbound-queue/src/lib.rs` performs, in order: verify proof, decode envelope, look up channel, then: [1](#0-0) 
This enforces `envelope.nonce == nonce + 1` exactly, mirroring `BridgedGovernor`'s `origin.nonce == _lastNonce + 1`.

Later in the same extrinsic, the payload is decoded and converted to XCM: [2](#0-1) 

`do_convert` delegates to `T::MessageConverter::convert`, whose `MessageToXcm::convert_send_native_token` implementation can permanently fail for a legitimately-encoded message, independent of any relayer or attacker malice: [3](#0-2) 
`ConvertAssetId::maybe_convert` returning `None` yields `InvalidToken`, and `reanchor` failing yields `CannotReanchor` — both are deterministic functions of the message content emitted by the Ethereum Gateway contract, not of relayer behavior.

Because FRAME wraps each dispatchable in an implicit storage transaction, any `Err` returned after the `Nonce::<T>::try_mutate` call (including `ConvertMessage`/`InvalidPayload`/`Send` errors from later steps) rolls back the whole extrinsic, **including the nonce increment**. Consequently `Nonce<T>` for that channel never advances past the value preceding the bad message. Since the check is `envelope.nonce != nonce.saturating_add(1)`, no message with a higher nonce for that channel can ever be accepted afterward — the lane is stuck forever, exactly like the `BridgedGovernor` case where a non-executable message with the exact-next nonce could never be superseded.

Unlike `pallet-bridge-messages`' `InboundLane::receive_message` (`bridges/modules/messages/src/inbound_lane.rs`), which advances `last_delivered_nonce` regardless of dispatch success (confirmed by test `receive_messages_accepts_single_message_with_invalid_payload`), and unlike the newer `inbound-queue-v2` pallet, which replaced sequential nonce enforcement with an unordered `SparseBitmap` presence check (`ensure!(!Nonce::<T>::get(nonce), ...); Nonce::<T>::set(nonce);` in `process_message`), the V1 inbound queue still has the strict, unrecoverable ordering: [4](#0-3) 
There is no `force_set_nonce`, admin override, or governance recovery path in `pallet-inbound-queue` to skip a stuck nonce.

### Impact Explanation
Once a single message on a channel fails conversion permanently, that channel is unable to deliver any further Ethereum→Polkadot message forever — this is a permanent bridge-state lock/stall of bridge processing on that channel, matching the "permanent user-fund or bridge-state lock" and "public underpriced work that ... stalls bridge processing" categories in the impact gate. All subsequent legitimate token transfers, asset registrations, and native token sends over that channel become permanently undeliverable, and any locked/escrowed assets awaiting delivery on the Polkadot side remain unredeemable.

### Likelihood Explanation
Triggering this does not require a malicious relayer, validator, or governance actor — it only requires that some ordinary Ethereum-side user (or the Gateway contract under normal, non-adversarial operation) emits a message whose command content deterministically fails one of the documented `ConvertMessageError` branches (`InvalidToken` via unregistered `TokenId`, `CannotReanchor` via an asset location that cannot be reanchored to the Ethereum universal location, or `InvalidDestination` for `SendNativeToken` to a non-`AccountId32` destination). Since the emitted event's nonce is fixed by Ethereum-side ordering and cannot be resubmitted with different content, and Substrate's default extrinsic-transactional rollback discards the nonce increment on any later error, the failure condition is reachable through unprivileged, everyday usage of the bridge rather than requiring any privileged or off-chain-trust-breaking action.

### Recommendation
Apply the same fix already used in `inbound-queue-v2`: decouple "message accepted for processing" from "message executed successfully". Either:
1. Commit the nonce advance in a separate, always-succeeding storage write before attempting decode/convert/send, using `frame_support::storage::with_transaction` explicitly scoped only around the conversion+send steps so a conversion failure doesn't roll back the nonce increment (mirroring `InboundLane::receive_message`'s pattern of always registering the nonce as delivered even when the inner dispatch fails), or
2. Switch to an unordered/presence-based nonce (as done in `inbound-queue-v2`'s `SparseBitmapImpl`) instead of strict `nonce == last + 1`, and additionally emit a `MessageRejected`-style event/dead-letter path for permanently unconvertible messages so relayers and users have visibility without blocking the lane.

### Proof of Concept
1. Deploy `pallet-inbound-queue` for a channel with `ChannelId = C`, current `Nonce<T>[C] = N`.
2. Craft (or have the Ethereum Gateway legitimately emit) an event with `nonce = N+1` whose `VersionedMessage` decodes to `Command::SendNativeToken { token_id, destination: Destination::ForeignAccountId32 { .. }, .. }`.
3. Because `SendNativeToken` requires `Destination::AccountId32` and any other destination variant is rejected in `convert_send_native_token`: [5](#0-4) 
the `submit()` call returns `Err(Error::ConvertMessage(InvalidDestination))` after the nonce `try_mutate` already ran; the whole extrinsic (including that mutate) is rolled back by FRAME's transactional dispatch wrapper, so `Nonce<T>[C]` remains `N`.
4. Any relayer resubmitting the *real* Ethereum event for nonce `N+1` (whose payload is fixed by the emitted log and cannot be altered) will always fail the same way — the channel can never accept nonce `N+2, N+3, ...` because `envelope.nonce != nonce.saturating_add(1)` will never hold again for that channel.
5. Result: permanent denial of message delivery for channel `C`, confirmed by the sequential‐nonce check at: [1](#0-0)

### Citations

**File:** bridges/snowbridge/pallets/inbound-queue/src/lib.rs (L256-267)
```rust
			// Verify message nonce
			<Nonce<T>>::try_mutate(envelope.channel_id, |nonce| -> DispatchResult {
				if *nonce == u64::MAX {
					return Err(Error::<T>::MaxNonceReached.into());
				}
				if envelope.nonce != nonce.saturating_add(1) {
					Err(Error::<T>::InvalidNonce.into())
				} else {
					*nonce = nonce.saturating_add(1);
					Ok(())
				}
			})?;
```

**File:** bridges/snowbridge/pallets/inbound-queue/src/lib.rs (L283-301)
```rust
			// Decode payload into `VersionedMessage`
			let message = VersionedMessage::decode_all(&mut envelope.payload.as_ref())
				.map_err(|_| Error::<T>::InvalidPayload)?;

			// Decode message into XCM
			let (xcm, fee) = Self::do_convert(envelope.message_id, message.clone())?;

			tracing::info!(
				target: LOG_TARGET,
				?xcm,
				?fee,
				"💫 xcm decoded"
			);

			// Burning fees for teleport
			Self::burn_fees(channel.para_id, fee)?;

			// Attempt to send XCM to a dest parachain
			let message_id = Self::send_xcm(xcm, channel.para_id)?;
```

**File:** bridges/snowbridge/primitives/inbound-queue/src/v1.rs (L414-436)
```rust
	) -> Result<(Xcm<()>, Balance), ConvertMessageError> {
		let network = Ethereum { chain_id };
		let asset_hub_fee_asset: Asset = (Location::parent(), asset_hub_fee).into();

		let beneficiary = match destination {
			// Final destination is a 32-byte account on AssetHub
			Destination::AccountId32 { id } => {
				Ok(Location::new(0, [AccountId32 { network: None, id }]))
			},
			// Forwarding to a destination parachain is not allowed for PNA and is validated on the
			// Ethereum side. https://github.com/Snowfork/snowbridge/blob/e87ddb2215b513455c844463a25323bb9c01ff36/contracts/src/Assets.sol#L216-L224
			_ => Err(ConvertMessageError::InvalidDestination),
		}?;

		let total_fee_asset: Asset = (Location::parent(), asset_hub_fee).into();

		let asset_loc =
			ConvertAssetId::maybe_convert(token_id).ok_or(ConvertMessageError::InvalidToken)?;

		let mut reanchored_asset_loc = asset_loc.clone();
		reanchored_asset_loc
			.reanchor(&GlobalAssetHubLocation::get(), &EthereumUniversalLocation::get())
			.map_err(|_| ConvertMessageError::CannotReanchor)?;
```

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs (L219-226)
```rust
			let (nonce, relayer_fee) = (message.nonce, message.relayer_fee);

			// Verify the message has not been processed
			ensure!(!Nonce::<T>::get(nonce), Error::<T>::InvalidNonce);

			// Mark message as received
			Nonce::<T>::set(nonce);

```
