Found a real local analog: the `add_tip` flow on `snowbridge-pallet-system-frontend` (AssetHub) lets any signed account swap/burn an arbitrary asset for Ether and dispatch an `AddTip { message_id, amount }` XCM to `pallet-system-v2`/`pallet-inbound-queue-v2` on BridgeHub, which credits the tip into `Tips::<T>` keyed only by an unvalidated `nonce`/`message_id` supplied by the caller — with no registry proving that nonce corresponds to an existing, in-flight inbound message.

### Title
Unvalidated message nonce in `AddTip::add_tip` permits burning user funds for a tip that can never be claimed - (File: `bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs`)

### Summary
`snowbridge-pallet-system-frontend::add_tip` on AssetHub burns/swaps a user-supplied asset into Ether and dispatches an XCM `Transact` to `pallet-inbound-queue-v2::add_tip` on BridgeHub. That handler only checks `!Nonce::<T>::get(nonce)` (i.e., "not yet consumed") before writing the tip amount into `Tips::<T>`. It never verifies that a message with the given `nonce`/`message_id` actually exists, is queued, or will ever arrive from Ethereum. This mirrors the external report's core defect: a public entry point accepts an arbitrary/unverified identifier (`_pubkeys` there, `nonce`/`message_id` here) and mutates on-chain accounting state for it without any registry check that the identifier corresponds to a real, tracked object.

### Finding Description
- `pallet-snowbridge-system-frontend::add_tip` (`bridges/snowbridge/pallets/system-frontend/src/lib.rs:261-273`) is `ensure_signed` only — any account can call it with any `message_id`. It swaps/burns the caller's asset for Ether via `Self::swap_fee_asset_and_burn` (irreversible burn, teleport-style) and then dispatches a remote `AddTip` transact call carrying that `message_id` and the resulting Ether amount. [1](#0-0) 
- On BridgeHub, `pallet_inbound_queue_v2::AddTip::add_tip` (`bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs:248-258`) only checks that the nonce hasn't already been consumed (`!Nonce::<T>::get(nonce)`), then unconditionally stores the tip in `Tips::<T>`: [2](#0-1) 
- There is no check that the nonce corresponds to a message that is actually pending/queued on the Ethereum side, nor any registry of "expected/valid nonces" analogous to what the external report recommends (`depositedValidators`-style mapping). Any `u64` value is accepted.
- `process_message` (`bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs:215-245`) is the only path that consumes a tip: it does `Tips::<T>::take(nonce)` and adds it to the relayer reward, but only fires when a real message with that nonce is later verified and processed via `submit`. If the caller supplied a nonce for a message that never arrives (mistyped, future nonce far beyond current sequence, or already-superseded value), the burned Ether is never refunded and the tip is permanently stranded in storage — `Tips::<T>` entries for phantom nonces persist forever with no cleanup path. [3](#0-2) 

### Impact Explanation
This satisfies the "permanent user-fund lock" pivot: real value (the user's tip asset, swapped and burned for Ether via `swap_fee_asset_and_burn` / `burn_for_teleport`) is destroyed on AssetHub based on an unverified remote identifier, and the credited `Tips` entry on BridgeHub can never be claimed if the nonce never materializes into a real inbound message. This is a genuine loss-of-funds condition triggerable entirely by an unprivileged, ordinary signed user — no malicious relayer, validator, or governance actor is required.

### Likelihood Explanation
Likelihood is moderate: the burn is a normal side effect of calling `add_tip` with a mistaken or aspirational `message_id` (e.g., tipping a nonce the user believes is about to be relayed, or estimating the wrong future nonce given no synchronous confirmation exists that the nonce is currently in-flight). There is also no explicit protection preventing tipping arbitrary far-future nonces that are unlikely to ever be assigned to a real message, since nonce assignment is purely sequential and driven by the source Ethereum contract's emitted events, not user input.

### Recommendation
Before crediting `Tips::<T>` in `add_tip`, validate that `nonce` corresponds to a message that is known to be legitimately outstanding — e.g., track a low/high bound of "next expected nonce" (or an explicit "pending" set populated when `submit` first observes higher nonces) and reject `add_tip` calls for nonces outside that plausible pending window. Alternatively, provide a `reclaim_tip`/refund path for the original tipper if the tip is never consumed after some deadline, similar to how the source report recommends a registry to check inputs against known valid entries before mutating state.

### Proof of Concept
1. On AssetHub, an unprivileged account calls `snowbridge_pallet_system_frontend::add_tip(message_id = <arbitrary_future_or_never_used_nonce>, asset = <some fungible asset>)`.
2. `swap_fee_asset_and_burn` swaps the asset to Ether and burns it for teleport (`bridges/snowbridge/pallets/system-frontend/src/lib.rs:372-404`); an XCM `Transact` carrying `EthereumSystemCall::AddTip { sender, message_id, amount }` is sent to BridgeHub.
3. On BridgeHub, `pallet_inbound_queue_v2::add_tip` checks only `!Nonce::<T>::get(nonce)` and stores `amount` in `Tips::<T>::get(nonce)` (`bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs:248-258`) — no check that this nonce is a real, soon-to-arrive message.
4. Because the chosen nonce was never actually assigned to a real Ethereum event (e.g., picked too far ahead, or a nonce that was skipped), `process_message` is never called with that nonce, so `Tips::<T>::take(nonce)` never fires and the Ether burned in step 2 is permanently unrecoverable, while the tip entry lingers in `Tips::<T>` storage indefinitely. [4](#0-3)

### Citations

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L261-273)
```rust
		pub fn add_tip(origin: OriginFor<T>, message_id: MessageId, asset: Asset) -> DispatchResult
		where
			<T as frame_system::Config>::AccountId: Into<Location>,
		{
			let who = ensure_signed(origin)?;

			let ether_gained = Self::swap_fee_asset_and_burn(who.clone().into(), asset)?;

			// Send the tip details to BH to be allocated to the reward in the Inbound/Outbound
			// pallet
			let call = Self::build_add_tip_call(who.clone(), message_id.clone(), ether_gained);
			Self::send_transact_call(who.into(), call)
		}
```

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs (L175-178)
```rust
	/// key for the storage map is the nonce of the message to which the tip should be added.
	/// The value is the tip amount, in Ether.
	#[pallet::storage]
	pub type Tips<T: Config> = StorageMap<_, Blake2_128Concat, u64, u128, OptionQuery>;
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

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs (L248-258)
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
```
