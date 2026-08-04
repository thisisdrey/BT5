### Title
Unbacked relayer reward: `relayer_fee` is a self-declared field trusted without any cross-check against transferred value - (File: bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs)

### Summary
The external report's core defect is that a UI component blindly renders/acts on a value (`simulationResult`) that is *self-declared by the less-trusted party* (the dapp) and used to drive a security-critical decision, without the trusted component (the snap) verifying it against ground truth. The Snowbridge inbound queue v2 pallet has the same broken invariant: the relayer reward amount (`message.relayer_fee`) is a field decoded straight out of the Ethereum `OutboundMessageAccepted` event payload — a value chosen by whoever calls `v2_sendMessage`/`v2_registerToken` on the Ethereum Gateway — and it is credited as a real reward with no on-chain check that it is backed by, or bounded by, the actual value (`message.value`) bridged over in the same message.

### Finding Description
`Message::try_from` decodes `relayer_fee` (and `execution_fee`, `value`) directly from the untrusted, attacker-influenceable Ethereum event payload: [1](#0-0) 

`Pallet::process_message` verifies the sending gateway address and the nonce (replay), converts/dispatches the message, and then pays out the reward using `relayer_fee` taken verbatim from the message, summed with any pre-registered tip, with **no check whatsoever relating `relayer_fee` to `message.value` or to any amount actually reserved/withdrawn for this message**: [2](#0-1) 

Looking at the XCM conversion path (`prepare`/`convert` in the message-to-XCM converter), only `message.value` and `message.execution_fee` are turned into on-chain asset transfers/`PayFees`/`ReserveAssetDeposited` instructions that move real ether-backed assets through the bridge; `relayer_fee` never appears there at all: [3](#0-2) [4](#0-3) 

So `relayer_fee` is completely decoupled from the asset-moving side of the bridge: it is purely a number in the event payload that the pallet trusts to credit a reward via `T::RewardPayment::register_reward`, exactly as the Snap UI trusted `simulationResult` strings supplied by the (less trusted) dapp to drive the user's sign/reject decision, without any independent verification or derivation from ground truth (here, the actual amount of ether locked/bridged, i.e. `message.value`).

The event log itself is authenticated by the beacon/receipt proof (`T::Verifier::verify`), so the *contents* of the payload are guaranteed to be what was emitted on Ethereum — but nothing in this Substrate-side code constrains what those contents may say. Whether the Solidity Gateway contract enforces `msg.value >= relayerFee + executionFee + Σassets` is off-repo and not something this pallet checks or can rely on; the guard the report calls for ("generate the trusted result within the snap/pallet itself" or at minimum validate the value before acting on it) is simply absent here.

### Impact Explanation
An account able to call the Gateway contract's `v2_sendMessage`/`v2_registerToken` on Ethereum (an ordinary, unprivileged bridge user — not a relayer, validator, or admin) fully controls the `relayerFee` field of the emitted event. Once any relayer submits a valid proof of that event, `process_message` unconditionally calls `T::RewardPayment::register_reward(&relayer, DefaultRewardKind, relayer_fee)`, crediting the relayer with a reward amount that has no relationship to any value actually locked/bridged for that message. This is an unbacked-mint/theft-class impact: rewards register real claimable value (via `pallet_bridge_relayers`) driven purely by an attacker-chosen number in a message field that is never validated against `message.value` or any other economically-bound quantity in the same pallet.

### Likelihood Explanation
The attack requires no privileged role and no bridge-relayer or validator collusion: it only needs a normal Ethereum-side account submitting a message with an inflated `relayerFee`, and any regular (honest) relayer to submit the corresponding proof to Bridge Hub — which is the intended, expected flow of the protocol. The Rust-side code shows no defense-in-depth check tying `relayer_fee` to `message.value`, so likelihood is high unless the Ethereum Solidity contract enforces this invariant off-chain in a way not visible/auditable from this repository.

### Recommendation
Enforce, in `Pallet::process_message` (or the converter that already has access to `message.value`, `message.execution_fee`, and asset values), that `relayer_fee` cannot exceed the value actually reserved/deposited for the message (e.g., `relayer_fee <= message.value` or an explicitly reserved fee bucket), rejecting or capping the reward if the declared fee is inconsistent with the bridged value. Do not treat `relayer_fee` as trusted solely because it passed beacon/receipt verification — verification proves *authenticity of the log*, not *economic soundness of its self-declared fields*. Additionally consider deriving the reward from a value that is actually escrowed/moved through the reserve/withdraw asset instructions rather than an independent free-form field.

### Proof of Concept
1. On Ethereum, call the Gateway's `v2_sendMessage` (or `v2_registerToken`) with `value = 1` (wei-equivalent) and `relayerFee = 10_000_000_000_000` (an arbitrarily large amount), while only actually transferring/locking the minimal `value`.
2. Wait for the event to be finalized; any relayer submits the `submit` extrinsic with a valid receipt/beacon proof for this event — verification succeeds because the log is authentic (the attacker only controls the log's field values, not whether it's included).
3. `Message::try_from` decodes `relayer_fee = 10_000_000_000_000` from the payload: [1](#0-0) 
4. `process_message` pays this full amount to the relayer via `T::RewardPayment::register_reward`, with no check against `message.value` or the actual reserved assets: [5](#0-4) 
5. The unit tests in the pallet confirm this behavior is unconditional — `relayer_fee` supplied in the `Message` struct is paid out exactly as given, regardless of `value`: [6](#0-5)

### Citations

**File:** bridges/snowbridge/primitives/inbound-queue/src/v2/message.rs (L166-176)
```rust
		let message = Message {
			gateway: log.address,
			nonce: event.nonce,
			origin: H160::from(event_payload.origin.as_ref()),
			assets: substrate_assets,
			payload: message_payload,
			claimer,
			value: event_payload.value,
			execution_fee: event_payload.executionFee,
			relayer_fee: event_payload.relayerFee,
		};
```

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

**File:** bridges/snowbridge/primitives/inbound-queue/src/v2/converter.rs (L145-165)
```rust
		let mut remote_xcm: Xcm<()> = match &message.payload {
			Payload::Raw(raw) => Self::decode_raw_xcm(raw),
			Payload::CreateAsset { token, network } => Self::make_create_asset_xcm(
				token,
				*network,
				message.value,
				bridge_owner,
				claimer.clone(),
			)?,
		};

		// Asset to cover XCM execution fee
		let execution_fee_asset: Asset = (ether_location.clone(), message.execution_fee).into();

		let mut assets = vec![];

		if message.value > 0 {
			// Asset for remaining ether
			let remaining_ether_asset: Asset = (ether_location.clone(), message.value).into();
			assets.push(AssetTransfer::ReserveDeposit(remaining_ether_asset));
		}
```

**File:** bridges/snowbridge/primitives/inbound-queue/src/v2/converter.rs (L380-395)
```rust
		let mut instructions = vec![
			DescendOrigin(InboundQueueLocation::get()),
			UniversalOrigin(GlobalConsensus(EthereumNetwork::get())),
			ReserveAssetDeposited(message.execution_fee.clone().into()),
		];

		// Set claimer before PayFees, in case the fees are not enough. Then the claimer will be
		// able to claim the funds still.
		instructions.push(SetHints {
			hints: vec![AssetClaimer { location: message.claimer }]
				.try_into()
				.expect("checked statically, qed"),
		});

		instructions.push(PayFees { asset: message.execution_fee.clone() });

```

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/test.rs (L441-479)
```rust
#[test]
fn relayer_fee_paid_out_when_no_tip_exists() {
	new_tester().execute_with(|| {
		let nonce: u64 = 88;
		let relayer_fee: u128 = 5_000;

		// Ensure no tip exists for this nonce
		assert_eq!(Tips::<Test>::get(nonce), None);

		// Process inbound message with relayer_fee but no tip
		let relayer: AccountId = Keyring::Bob.into();
		assert_ok!(InboundQueue::process_message(
			relayer,
			Message {
				nonce,
				assets: vec![],
				payload: Payload::Raw(vec![]),
				claimer: None,
				execution_fee: 1_000_000_000,
				relayer_fee,
				gateway: mock::GatewayAddress::get(),
				origin: H160::random(),
				value: 3_000_000_000,
			},
		));

		// Relayer fee should be paid out even without tip
		assert_eq!(
			RegisteredRewardsCount::get(),
			1,
			"Relayer fee should be paid out even when no tip exists"
		);

		// Check the actual reward amount paid out
		assert_eq!(
			RegisteredRewardAmount::get(),
			relayer_fee,
			"Reward amount should equal relayer_fee when no tip exists"
		);
```
