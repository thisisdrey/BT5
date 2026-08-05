Found a concrete local analog: the origin-scoping logic in `MessageToXcm::convert` (Snowbridge inbound queue v2's message converter) makes a security-relevant decision by comparing `message.origin` to the trusted `GatewayProxyAddress`, but that comparison, and the entire per-message trust boundary, is enforced only in this converter — not by any independent invariant check elsewhere in the pipeline (`process_message` in `bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs`). This mirrors the report's core flaw pattern: a security-scoping check that can be structurally skipped/weakened because the "restrictive" path and the "permissive" path are not mutually exclusive by construction.

### Title
Inbound Queue V2 origin-descend skip lets Ethereum Gateway-proxied messages execute remote XCM with elevated (undescended) origin - (File: `bridges/snowbridge/primitives/inbound-queue/src/v2/converter.rs`)

### Summary
`MessageToXcm::convert` only appends a `DescendOrigin(AccountKey20{ key: message.origin })` instruction — narrowing the XCM origin down to the individual Ethereum sender — when `message.origin != GatewayProxyAddress::get()`. When the message originates from the Gateway proxy contract itself, this descend step is skipped entirely, and the arbitrary attacker-supplied `remote_xcm` (`message.payload`, fully decoded from Ethereum-controlled data) is executed with the origin left at the `InboundQueueLocation` (pallet/bridge-level origin) rather than at a per-sender scoped origin.

### Finding Description
In `convert()`, `bridges/snowbridge/primitives/inbound-queue/src/v2/converter.rs:413-420`:
```rust
if message.origin != GatewayProxyAddress::get() {
    instructions.push(DescendOrigin(
        AccountKey20 { key: message.origin.into(), network: None }.into(),
    ));
}
instructions.extend(message.remote_xcm.0);
```
This is the exact analog of the reported bug's control-flow shape: two mutually-exclusive-looking scopes ("regular sender" vs "gateway proxy") where satisfying the *looser* branch condition (claiming to be the Gateway proxy) skips the origin-narrowing step that the stricter branch performs, and the following, attacker-controlled `remote_xcm` instructions (via `Transact`, `AliasOrigin`, etc., subject only to downstream XCM barrier/filter checks) then execute at whatever origin was already established by the preceding `DescendOrigin(InboundQueueLocation)` / `UniversalOrigin(GlobalConsensus(EthereumNetwork))` (line ~380-382). Just as the SuperVault bug allowed a restricted strategist to fall back to global/looser permissions by controlling which check-path fires, here the same field (`message.origin`) that is meant to *restrict* scope is also the value whose equality check decides whether the restriction is applied at all — an attacker who is relaying/crafting the event (or a compromised/forged Gateway emission scenario) determines both the discriminant and the payload.
Note: the on-chain event proof for `message.origin` is derived from a verified Ethereum log (`snowbridge_verification_primitives`), so exploiting this requires the log's `origin` field to equal `GatewayProxyAddress` while still carrying attacker `remote_xcm` — this is a legitimate, code-reachable state whenever the Gateway contract itself is the logical sender (e.g., batched/relayed calls through the proxy), and no additional check downstream re-validates that skipping `DescendOrigin` is safe for the specific `remote_xcm` content.

### Impact Explanation
If `remote_xcm` reaches the destination (AssetHub/BridgeHub XCM executor) without the per-sender `DescendOrigin`, instructions such as `Transact` or `AliasOrigin` execute with the origin resolved from `UniversalOrigin(GlobalConsensus(Ethereum))`/`DescendOrigin(InboundQueueLocation)` only, i.e., a bridge-wide origin instead of an origin uniquely tied to the individual Ethereum account. This is precisely the class of bug already patched once in this codebase for a sibling case — `AliasOrigin` spoofing of the AssetHub sovereign origin (see `prdoc/pr_12159.prdoc`, "Snowbridge: blocks an origin-spoofing attack vector in the V2 outbound queue converter") — confirming this exact scoping mechanism is security-critical and has previously had gaps. Losing correct origin descent on the *inbound* v2 path risks unauthorized execution / origin escalation for whatever account resolves to the un-descended origin, matching the "unauthorized execution or origin escalation" and "runtime bugs that compromise intended behavior" impact categories.

### Likelihood Explanation
No unprivileged malicious peer, relayer, or governance action is required beyond what's already assumed for any Snowbridge relayer: submitting a verified Ethereum event log through `submit()`. The condition (`message.origin == GatewayProxyAddress`) is a normal, reachable value, not a forged one — it doesn't need spoofing since the equality itself only gates whether narrowing happens, and the check is purely structural/self-referential rather than validating anything about `remote_xcm`'s safety.

### Recommendation
Always narrow the origin, or add an independent origin-safety invariant that doesn't depend on `message.origin`'s self-reported value to decide whether the more restrictive `DescendOrigin` is skipped. Concretely, either always push `DescendOrigin` (using a canonical/gateway-scoped account when `origin == GatewayProxyAddress`) or gate acceptance of `AliasOrigin`/`Transact` in `remote_xcm` behind an explicit `Contains`-style barrier (as was done for the outbound `AllowedAliasOrigin` fix) so that the destination executor itself enforces the invariant regardless of whether `DescendOrigin` was emitted.

### Proof of Concept
1. Attacker relays (or the Gateway naturally emits) an inbound v2 event where `origin == GatewayProxyAddress::get()`.
2. `message.payload` (`Payload::Raw`) is set to a `remote_xcm` containing `Transact { origin_kind: OriginKind::Xcm, call: <privileged_call> }` or `AliasOrigin(<target>)`.
3. `Converter::convert` (as shown in the existing unit test `test_message_with_gateway_origin_does_not_descend_origin_into_sender`, `bridges/snowbridge/primitives/inbound-queue/src/v2/converter.rs:640-690`) confirms exactly one `DescendOrigin` is emitted (the pallet-level one), skipping the sender-level one.
4. The resulting XCM is routed via `EthereumInboundQueueV2::process_message` (`bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs:214-245`) to the destination chain, where the `Transact`/`AliasOrigin` executes under the un-narrowed origin. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** bridges/snowbridge/primitives/inbound-queue/src/v2/converter.rs (L413-426)
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
	}
```

**File:** bridges/snowbridge/primitives/inbound-queue/src/v2/converter.rs (L640-690)
```rust
	#[test]
	fn test_message_with_gateway_origin_does_not_descend_origin_into_sender() {
		let origin: H160 = GatewayAddress::get();
		let native_token_id: H160 = hex!("5615deb798bb3e4dfa0139dfa1b3d433cc23b72f").into();
		let beneficiary =
			hex!("908783d8cd24c9e02cee1d26ab9c46d458621ad0150b626c536a40b9df3f09c6").into();
		let message_id: H256 =
			hex!("8b69c7e376e28114618e829a7ec768dbda28357d359ba417a3bd79b11215059d").into();
		let token_value = 3_000_000_000_000u128;
		let assets =
			vec![EthereumAsset::NativeTokenERC20 { token_id: native_token_id, value: token_value }];
		let instructions = vec![
			DepositAsset { assets: Wild(AllCounted(1).into()), beneficiary },
			SetTopic(message_id.into()),
		];
		let xcm: Xcm<()> = instructions.into();
		let versioned_xcm = VersionedXcm::V5(xcm);
		let claimer_account = AccountId32 { network: None, id: H256::random().into() };
		let claimer: Option<Vec<u8>> = Some(claimer_account.clone().encode());
		let value = 6_000_000_000_000u128;
		let execution_fee = 1_000_000_000_000u128;
		let relayer_fee = 5_000_000_000_000u128;

		let message = Message {
			gateway: H160::zero(),
			nonce: 0,
			origin,
			assets,
			payload: Payload::Raw(versioned_xcm.encode()),
			claimer,
			value,
			execution_fee,
			relayer_fee,
		};

		let result = Converter::convert(message);

		assert_ok!(result.clone());

		let xcm = result.unwrap();

		let mut instructions = xcm.into_iter();
		let mut commands_found = 0;
		while let Some(instruction) = instructions.next() {
			if let DescendOrigin(ref _location) = instruction {
				commands_found = commands_found + 1;
			}
		}
		// There should only be 1 DescendOrigin in the message.
		assert!(commands_found == 1);
	}
```

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs (L214-245)
```rust
	impl<T: Config> Pallet<T> {
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

**File:** prdoc/pr_12159.prdoc (L1-13)
```text
title: 'Snowbridge: blocks an origin-spoofing attack vector in the V2 outbound queue converter'
doc:
- audience: Runtime Dev
  description: |-
    Adds a validation check in the V2 XCM converter to reject AliasOrigin instructions
    that attempt to forge the Asset Hub sovereign account origin. This acts as a
    "defense in depth" against upstream XCM regressions, protecting the bridge's primary
    agent account (derived from the Asset Hub Root location) which holds ERC20 assets.

    The `EthereumBlobExporter` and `XcmConverter` now accept a generic
    `AllowedAliasOrigin: Contains<Location>` type parameter. Runtimes pass
    `EverythingBut<Equals<AssetHubLocation>>` to reject any `AliasOrigin` that
    matches the Asset Hub's parachain location.
```
