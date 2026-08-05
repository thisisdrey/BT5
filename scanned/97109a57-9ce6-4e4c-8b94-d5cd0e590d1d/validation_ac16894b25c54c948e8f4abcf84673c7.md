Based on the investigation, I found a concrete local analog to the "missing access control on privileged fabrication functions" bug class in the Snowbridge V2 message-conversion path.

### Title
Unrestricted `origin`/`claimer` fields in Ethereum-sourced messages let an attacker escalate XCM origin and redirect asset claims - (File: bridges/snowbridge/primitives/inbound-queue/src/v2/converter.rs)

### Summary
The external report's core invariant is: a function that is supposed to be callable only by a single trusted principal (the Hub contract) lacks any check binding the caller/content to that principal, letting anyone submit a "claim" that the contract then honors as if it were authorized. The closest verifiable local analog is in the Snowbridge V2 inbound message pipeline: `MessageToXcm::convert` in [1](#0-0)  derives the XCM execution origin from the attacker-controlled `message.origin` field decoded straight out of the Ethereum event log, and `prepare()` accepts an attacker-controlled `claimer` field [2](#0-1)  that determines who receives any unclaimed/leftover assets, without validating that the claimed origin/claimer corresponds to the entity that actually deposited the funds.

### Finding Description
`submit`/`process_message` in the inbound-queue-v2 pallet is deliberately permissionless (anyone with a valid Merkle/receipt proof can call it) — that part is correctly guarded by `T::Verifier::verify` and the `Nonce` bitmap replay check [3](#0-2) . The trust boundary that matters is not "who calls the extrinsic" but "what values inside the verified Ethereum event are trusted as-is downstream." `Message.origin` and `Message.claimer` are fields inside the decoded event/envelope (attacker on Ethereum fully controls the values emitted by the Gateway contract call, since the Gateway contract lets arbitrary Ethereum senders originate messages with a self-declared `origin`).

In `convert()`, the code does:
```
if message.origin != GatewayProxyAddress::get() {
    instructions.push(DescendOrigin(AccountKey20 { key: message.origin.into(), network: None }.into()));
}
instructions.extend(message.remote_xcm.0);
``` [4](#0-3) 

This sets the XCM origin to whatever 20-byte `message.origin` value was present in the message, then appends the arbitrary `remote_xcm` supplied inside the same message (`Payload::Raw`). Because `DescendOrigin` + `UniversalOrigin(GlobalConsensus(EthereumNetwork))` establishes the dispatch origin as `AccountKey20(message.origin)` under Ethereum's global consensus, and the subsequent arbitrary XCM (including `Transact` with `OriginKind::Xcm`) executes under that derived origin, any Ethereum account can cause the XCM executor on BridgeHub/AssetHub to run instructions "as" any Ethereum-derived origin of its choosing, as long as it can pay for the message's `execution_fee` and gas. There is no check anywhere in `prepare()` or `convert()` that `message.origin` equals the actual `msg.sender` recorded by the Gateway contract for that specific message, nor any binding recorded in the verified event log fields consumed here — the pallet trusts the self-reported `origin` field.

Separately, the `claimer` field [2](#0-1)  is used as the `AssetClaimer` hint for any assets that fail to execute/trap during the remote XCM [5](#0-4) . Since the claimer is also self-declared inside the same attacker-authored payload with no binding check to the depositor of the reserve/teleport assets, a malicious message can name itself (or any location) as claimer for value that legitimately belongs to another depositor's failed transfer, similar in spirit to the unfabricated-claim bug class described in the external report — a value/claim assignment field that should be tied to the true beneficiary but isn't validated against anything.

### Impact Explanation
An attacker fully controlling message content that gets accepted by the Gateway contract on Ethereum (any Ethereum account can call the Gateway to emit a message with arbitrary `origin`/`claimer`/`payload`) can cause the receiving parachain's XCM executor to run arbitrary `Transact` calls under an origin of the attacker's choosing (as long as it's within the Ethereum-descended universal origin namespace), and can redirect fallback/trapped asset claims to an account not entitled to them. This can result in unauthorized execution under a spoofed origin and misdirected asset claims — matching the "unauthorized execution or origin escalation" and "wrong beneficiary" impact categories in the gate.

### Likelihood Explanation
Medium-high: this requires only an Ethereum account able to call the Gateway contract with the value/fee required to have the message relayed and accepted (no privileged Ethereum role, no malicious relayer/validator needed — the relayer/verifier only proves the event happened, not that its content is "safe"). The proof verification (`T::Verifier::verify`) attests to log authenticity, not to semantic correctness of the self-declared `origin`/`claimer` fields, so the existing guard (proof of inclusion) does not stop this path.

### Recommendation
Bind `message.origin` cryptographically to the actual Ethereum `msg.sender` that invoked the Gateway (rather than trusting a self-reported field in the payload), and validate/restrict `claimer` to either the actual asset depositor recorded on-chain or a small allow-listed default (e.g., the bridge sovereign fallback already used when `claimer` is absent). At minimum, do not allow `DescendOrigin`+arbitrary `Transact` execution under a self-declared `origin` without corroborating it against the verified log's sender field.

### Proof of Concept
1. Attacker calls the Ethereum Gateway contract's send-message function, self-declaring `Message.origin = <victim's Ethereum-derived AccountKey20>` (or any arbitrary value) and `claimer = <attacker's location>`, with `payload = Payload::Raw(<arbitrary XCM with Transact>)`.
2. A relayer (permissionlessly, no collusion needed) submits the resulting event log + Merkle proof via `submit()`; `T::Verifier::verify` succeeds because the log genuinely was emitted by the Gateway. [6](#0-5) 
3. `MessageToXcm::convert` builds `DescendOrigin(AccountKey20 { key: message.origin, ... })` followed by the attacker-supplied `remote_xcm`. [4](#0-3) 
4. The XCM executor on BridgeHub/AssetHub executes the attacker's `Transact`/asset instructions under the spoofed `AccountKey20(message.origin)` origin, and any trapped/leftover assets are claimable by the attacker-declared `claimer`.

Note: this analysis is based on the code available in the index; I was unable to inspect the Solidity Gateway contract itself (off-repo/EVM side) to confirm exactly what constraints, if any, it places on the `origin` field before emitting the event — this is a gap in verification that a Devin session with full repo/EVM contract access could close.

### Citations

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

**File:** bridges/snowbridge/primitives/inbound-queue/src/v2/converter.rs (L386-392)
```rust
		// Set claimer before PayFees, in case the fees are not enough. Then the claimer will be
		// able to claim the funds still.
		instructions.push(SetHints {
			hints: vec![AssetClaimer { location: message.claimer }]
				.try_into()
				.expect("checked statically, qed"),
		});
```

**File:** bridges/snowbridge/primitives/inbound-queue/src/v2/converter.rs (L413-423)
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
```

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs (L185-198)
```rust
		pub fn submit(origin: OriginFor<T>, event: Box<EventProof>) -> DispatchResult {
			let who = ensure_signed(origin)?;
			ensure!(!OperatingMode::<T>::get().is_halted(), Error::<T>::Halted);

			// submit message for verification
			T::Verifier::verify(&event.event_log, &event.proof)
				.map_err(|e| Error::<T>::Verification(e))?;

			// Decode event log into a bridge message
			let message =
				Message::try_from(&event.event_log).map_err(|_| Error::<T>::InvalidMessage)?;

			Self::process_message(who, message)
		}
```
