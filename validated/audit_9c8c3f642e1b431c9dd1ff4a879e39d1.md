### Title
Unvalidated Ethereum `chain_id` embedded in inbound message payload is trusted to construct the bridge's cross-consensus origin - ([File: bridges/snowbridge/primitives/inbound-queue/src/v1.rs])

### Summary
The Snowbridge V1 inbound-queue pallet verifies the *Ethereum event proof* and the *Gateway contract address* of an inbound message, but never validates the `chain_id: u64` field carried **inside** the message payload itself (`MessageV1.chain_id`). This field is fully attacker/relayer-influenced data decoded from the message body, yet it is used, unchecked, to construct the `NetworkId::Ethereum { chain_id }` value that becomes the `GlobalConsensus` origin of the resulting XCM program (via `UniversalOrigin`), and to derive the token/asset location. This is the same broken invariant as the GorplesCoin `redeem()` bug: a "chain of origin" identifier is taken from message payload data and used to authorize downstream state changes without confirming it matches the one true, protocol-supported chain id.

### Finding Description
In `bridges/snowbridge/pallets/inbound-queue/src/lib.rs::submit()`, only two checks bind the message to a specific chain/context: [1](#0-0) 
these check the storage/event proof and the `GatewayAddress`/`ChannelId` — but not the `chain_id` field that will later be decoded from `envelope.payload` as part of `VersionedMessage::V1(MessageV1 { chain_id, command })`.

That payload is then handed to `do_convert` → `T::MessageConverter::convert`, which for every command variant does: [2](#0-1) 
and, for example in `convert_register_token`, builds the cross-consensus origin directly from the untrusted `chain_id`: [3](#0-2) 
There is no comparison of `chain_id` against a fixed, pallet-configured Ethereum network id (there is no `EthereumNetwork: Get<NetworkId>` style check anywhere in `submit()`, `do_convert()`, or `convert()`). The same unchecked value is reused in `convert_send_token` and `convert_send_native_token` to build the `network` used for `UniversalOrigin(GlobalConsensus(network))`, and to derive `EthereumLocationsConverterFor::from_chain_id(&chain_id)` (the sovereign-account owner for freshly created assets), and the asset's `Location` (`GlobalConsensus(network)` / `AccountKey20` under that network).

Exactly like the GorplesCoin `_fromChain` parameter, the `chain_id` here is decoded from message calldata and never checked against the one chain id the protocol is supposed to support — the check is implicitly assumed to be enforced "upstream" (by the header/finality verifier and gateway address check), but neither of those actually inspects or pins the embedded `chain_id` value.

### Impact Explanation
Because `UniversalOrigin(GlobalConsensus(network))` grants the resulting XCM program a specific, privileged bridge-derived origin, and `EthereumLocationsConverterFor::from_chain_id(&chain_id)` derives the asset-owner account from that same field, a message whose embedded `chain_id` does not match the network the relay/verifier actually secures would:
- create/register assets under a `Location` keyed by an arbitrary `chain_id` (distinct from the configured `EthereumNetwork`), producing a foreign-asset entry that does not correspond to any registered/expected bridge network, and
- derive a different owner/sovereign account (`EthereumLocationsConverterFor::from_chain_id`) than the canonical bridge sovereign, and
- reanchor/represent tokens under an unintended `GlobalConsensus(Ethereum{chain_id})` location that could collide with or be confused for a legitimately configured chain id in other parts of the runtime (e.g. `EthereumLocation`/`GlobalAssetHubLocation` parameter types configured elsewhere assume one specific id).

This falls squarely in the "forged or mis-bound proof or state acceptance" / "unauthorized origin escalation" impact category for BridgeHub/Snowbridge, since the bridge's cross-consensus origin binding is not pinned to the chain id it is supposed to represent.

### Likelihood Explanation
The `chain_id` is fully attacker-controlled payload data from the relayer's perspective (it's just bytes inside the event log data, not derived from the storage/header proof), and `submit()` is a public, unsigned-origin-gated (but permissionless-relayer) extrinsic. Exploitation only requires constructing/relaying a message whose event log passes the existing `Verifier`/`GatewayAddress` checks (both of which are orthogonal to the payload's `chain_id` field) — no validator, governance, or privileged actor is needed.

### Recommendation
Bind the embedded `chain_id` to the pallet's single configured Ethereum network id before it is used to build `NetworkId::Ethereum`/`UniversalOrigin`/asset locations, e.g. add a `T::EthereumNetwork: Get<NetworkId>` (or `Get<u64>`) associated type in `Config`, and in `submit()`/`convert()` `ensure!(chain_id == T::EthereumNetwork::get(), Error::<T>::InvalidNetwork)` — mirroring the existing `GatewayAddress` check pattern — before decoding/dispatching the command.

### Proof of Concept
1. Assume the Gateway contract emits (or a relayer submits) an event whose ABI-encoded payload decodes to `VersionedMessage::V1(MessageV1 { chain_id: <arbitrary_u64>, command: RegisterToken { token, fee } })`.
2. The event log still passes `T::Verifier::verify` (valid inclusion proof for the real Ethereum chain being tracked) and `envelope.gateway == T::GatewayAddress::get()`.
3. `submit()` proceeds to `do_convert` → `MessageToXcm::convert` → `convert_register_token(message_id, chain_id, token, fee)`, at: [3](#0-2) 
which builds `network = Ethereum { chain_id }` from the attacker/payload-controlled value, with no comparison against the pallet's actual configured Ethereum network id anywhere in the call path from `submit()` through `convert()`.
4. The resulting XCM's `UniversalOrigin(GlobalConsensus(network))` and asset `Location`/owner account are thus derived from an unverified `chain_id`, rather than the one chain id the bridge is deployed to represent — the same "missing verification of chain of origin" defect reported for GorplesCoin's `redeem()`.

### Citations

**File:** bridges/snowbridge/pallets/inbound-queue/src/lib.rs (L242-254)
```rust
			T::Verifier::verify(&event.event_log, &event.proof)
				.map_err(|e| Error::<T>::Verification(e))?;

			// Decode event log into an Envelope
			let envelope =
				Envelope::try_from(&event.event_log).map_err(|_| Error::<T>::InvalidEnvelope)?;

			// Verify that the message was submitted from the known Gateway contract
			ensure!(T::GatewayAddress::get() == envelope.gateway, Error::<T>::InvalidGateway);

			// Retrieve the registered channel for this message
			let channel =
				T::ChannelLookup::lookup(envelope.channel_id).ok_or(Error::<T>::InvalidChannel)?;
```

**File:** bridges/snowbridge/primitives/inbound-queue/src/v1.rs (L179-204)
```rust
	fn convert(
		message_id: H256,
		message: VersionedMessage,
	) -> Result<(Xcm<()>, Self::Balance), ConvertMessageError> {
		use Command::*;
		use VersionedMessage::*;
		match message {
			V1(MessageV1 { chain_id, command: RegisterToken { token, fee } }) => {
				Ok(Self::convert_register_token(message_id, chain_id, token, fee))
			},
			V1(MessageV1 { chain_id, command: SendToken { token, destination, amount, fee } }) => {
				Ok(Self::convert_send_token(message_id, chain_id, token, destination, amount, fee))
			},
			V1(MessageV1 {
				chain_id,
				command: SendNativeToken { token_id, destination, amount, fee },
			}) => Self::convert_send_native_token(
				message_id,
				chain_id,
				token_id,
				destination,
				amount,
				fee,
			),
		}
	}
```

**File:** bridges/snowbridge/primitives/inbound-queue/src/v1.rs (L242-253)
```rust
	) -> (Xcm<()>, Balance) {
		let network = Ethereum { chain_id };
		let xcm_fee: Asset = (Location::parent(), fee).into();
		let deposit: Asset = (Location::parent(), CreateAssetDeposit::get()).into();

		let total_amount = fee + CreateAssetDeposit::get();
		let total: Asset = (Location::parent(), total_amount).into();

		let bridge_location = Location::new(2, GlobalConsensus(network));

		let owner = EthereumLocationsConverterFor::<[u8; 32]>::from_chain_id(&chain_id);
		let asset_id = Self::convert_token_address(network, token);
```
