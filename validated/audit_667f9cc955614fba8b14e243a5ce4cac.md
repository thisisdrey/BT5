### Title
No minimum relayer fee enforced for inbound bridge messages allows zero-incentive messages to permanently stall in Snowbridge's inbound queue - (File: `bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs`)

### Summary
`Pallet::submit` / `Pallet::process_message` in the Snowbridge inbound queue v2 pallet reward the relayer with whatever `relayer_fee` value is embedded in the Ethereum-originated message, with no minimum-fee floor enforced on-chain. A message creator on Ethereum fully controls this fee value. If it is set to `0` (and nobody calls `AddTip::add_tip`), no rational relayer has an economic incentive to submit the corresponding proof, so the message — and the user funds locked on Ethereum that it represents — can sit unprocessed indefinitely. This is the direct analog of the reported `minLoanSize = 0` bug: an economic threshold that is supposed to keep the "clean-up" actor (liquidator/relayer) incentivized is absent, so third parties have no reason to perform the required action, and the burden/loss falls on users and the protocol.

### Finding Description
`process_message` computes the reward purely from attacker-supplied data: [1](#0-0) 

- `relayer_fee` is taken directly from the decoded `Message` (`message.relayer_fee`), which originates from the Ethereum Gateway contract event that the *sender* of the cross-chain transfer controls.
- The only additional incentive source is `Tips::<T>` via `AddTip::add_tip`, which is optional and requires a *separate* actor to spend funds adding a tip — there is no requirement that a message ever receive a non-zero fee or tip. [2](#0-1) 
- There is no `ensure!(relayer_fee >= MinFee, ...)` check anywhere in `submit` or `process_message`, unlike comparable staking/pool pallets in this codebase that do enforce minimum-bond/minimum-slash thresholds (e.g., `min_nominator_bond`/`min_validator_bond` in `substrate/frame/staking-async/src/pallet/impls.rs:82-96`, or `MinJoinBond` in `substrate/frame/nomination-pools/src/lib.rs:2132`) precisely to keep permissionless third-party actions economically viable.
- `submit` is a fully permissionless, `Signed`-origin extrinsic: any relayer pays their own transaction fee to call `T::Verifier::verify` and `Self::process_message`, and is only compensated from `relayer_fee + tip`. If `total_tip == 0`, no `register_reward` call happens at all, and the relayer has spent gas/weight for nothing. [3](#0-2) 

Because relaying costs real transaction fees but yields nothing when `relayer_fee` is zero (or negligible), an attacker can create arbitrarily many zero-fee/low-fee Ethereum-to-Polkadot transfer messages. None of these will attract a rational relayer to submit them (absent someone unrelated volunteering to pay a tip out of their own pocket), so the messages remain permanently un-relayed even though they are verifiable and would otherwise succeed.

### Impact Explanation
This directly maps to the "public underpriced work that degrades block production or stalls bridge processing" and "permanent user-fund or bridge-state lock" impact categories:
- Legitimate users whose transfers get a zero/near-zero `relayer_fee` (whether by mistake, misconfiguration of the Gateway-side fee estimate, or deliberate griefing by a third party crafting such messages) have their locked Ethereum-side funds effectively stuck, since the representative message on the Polkadot side is never relayed/dispatched.
- At scale this is a cheap, permanent-relative-to-cost griefing vector: an attacker pays trivial gas on Ethereum to emit many zero-fee messages, none of which will ever be economically worth submitting, silently bloating the set of "stuck" nonces and undermining bridge liveness guarantees without requiring any validator/relayer/governance misbehavior.

### Likelihood Explanation
Likelihood is moderate-to-high: any unprivileged Ethereum-side actor can set `relayer_fee` to `0` when emitting a Gateway event, and no on-chain guard in `process_message`/`submit` prevents this or forces a floor. No malicious relayer, validator, governance actor, or off-chain infrastructure compromise is needed — only an ordinary user or griefer emitting a message with `relayer_fee = 0`.

### Recommendation
Enforce an on-chain minimum relayer fee (or minimum total incentive) before a message is accepted for processing/dispatch, mirroring the pattern used elsewhere in this codebase (e.g., `MinNominatorBond`/`MinValidatorBond`, `MinJoinBond`). Concretely:
- Add a configurable `MinRelayerFee` (or similar) to `pallet_inbound_queue_v2::Config`.
- In `process_message`, reject (or route to a governance/protocol-subsidized cleanup path) messages whose `relayer_fee + Tips::<T>::get(nonce)` is below this floor, instead of silently accepting them with `total_tip == 0`.
- Alternatively, ensure a minimum fee is validated on the Ethereum Gateway contract side and cross-checked against the decoded message before `Nonce::<T>::set(nonce)` is committed, so that under-priced messages cannot enter the "processed but unrewarded" state described above.

### Proof of Concept
1. On Ethereum, call the Gateway contract to lock funds and emit an outbound-to-Polkadot event with `relayer_fee = 0` (fully attacker-controlled field of `Message`).
2. On the Polkadot side, any relayer could call `Pallet::submit(origin, event)`, which flows into `process_message`:
   - `ensure!(T::GatewayAddress::get() == message.gateway, ...)` passes (real gateway).
   - `Nonce::<T>::get(nonce)` is `false`, so it proceeds and marks `Nonce::<T>::set(nonce)`.
   - `T::MessageProcessor::process_message` succeeds, dispatching the XCM to AssetHub.
   - `tip` from `Tips::<T>` is `None` (nobody added one), so `total_tip = 0`.
   - The `if total_tip > 0` branch is skipped — `T::RewardPayment::register_reward` is never invoked.
3. The relayer paid a real transaction fee for `submit` and the underlying `verify` + XCM send weight, but received zero reward. Rational relayers will not submit such messages.
4. Repeat step 1 for arbitrarily many messages at negligible Ethereum-side cost: all such messages remain permanently unprocessed/un-relayed, and any user funds represented by these messages are effectively locked, matching the "no incentive to clean up small/underwater positions" pattern from the source report.

### Citations

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs (L182-198)
```rust
		/// Submit an inbound message originating from the Gateway contract on Ethereum
		#[pallet::call_index(0)]
		#[pallet::weight(T::WeightInfo::submit())]
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

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs (L248-259)
```rust
	impl<T: Config> AddTip for Pallet<T> {
		fn add_tip(nonce: u64, amount: u128) -> Result<(), AddTipError> {
			ensure!(amount > 0, AddTipError::AmountZero);
			// If the nonce is already processed, return an error
			ensure!(!Nonce::<T>::get(nonce.into()), AddTipError::NonceConsumed);
			// Otherwise add the tip.
			Tips::<T>::mutate(nonce, |tip| {
				*tip = Some(tip.unwrap_or_default().saturating_add(amount));
			});
			return Ok(());
		}
	}
```
