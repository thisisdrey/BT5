Found the exact analog: `Tips::<T>::mutate` in the Snowbridge inbound-queue-v2 pallet creates/updates a storage entry keyed by a message `nonce` without any check that a corresponding message actually exists or will ever arrive.

### Title
Unbounded tip storage can be created for nonces that never resolve to a real message - ([File: bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs])

### Summary
`add_tip` in `bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs` only checks that the `nonce` has not already been *consumed* by `Nonce::<T>::get(nonce)`, but never verifies that the `nonce` corresponds to a message that is actually pending, in flight, or will ever be delivered from Ethereum. Anyone able to invoke `AddTip::add_tip` (via `pallet-system-v2`/`pallet-system-frontend`'s `add_tip` extrinsic) can write/inflate a `Tips` entry for an arbitrary future or non-existent `nonce`, mirroring the P2P report's pattern of updating a mapping value for a key that was never initialized/registered.

### Finding Description
`Tips` is a plain `StorageMap<_, Blake2_128Concat, u64, u128, OptionQuery>` keyed by message nonce: [1](#0-0) 

`add_tip` only guards against a nonce that has *already* been consumed, not against a nonce that has never been assigned to a real message: [2](#0-1) 

Contrast this with the sibling `outbound-queue-v2` implementation, which correctly requires the referenced entity (`PendingOrders`) to already exist before mutating it, returning `AddTipError::UnknownMessage` otherwise: [3](#0-2) 

The inbound side has no equivalent "does this nonce correspond to a real, pending message" check — it can only tell you the nonce hasn't been consumed *yet*, which is true for every nonce that has not happened, including nonces far in the future that may never be used, or nonces that will never legitimately exist. This is the same broken invariant as the external report: a value keyed by an entity identifier is written/updated in a mapping without verifying that identifier's existence/registration.

### Impact Explanation
This is a public, unprivileged entry point (`add_tip` is callable by anyone permitted by `T::FrontendOrigin`, and the underlying `AddTip::add_tip` trait call performs no existence check for inbound nonces) that lets a caller create and inflate arbitrary `Tips` storage entries for `nonce` values with no corresponding message. Because tips are paid from a fixed reward budget once a message with that nonce is eventually processed (`Tips::<T>::take(nonce)` in `process_message`), an attacker can pre-seed large tip amounts against arbitrary future nonces, which will unexpectedly inflate relayer payouts once (if ever) a message with that nonce is processed, or otherwise permanently bloat storage with tips for nonces that never resolve (storage lock/leak — never reclaimed since there's no message to consume them).

### Likelihood Explanation
Likelihood is moderate: it requires the caller to be authorized as `T::FrontendOrigin` for `pallet-system-v2`'s `add_tip`, so it is not a fully unrestricted public path, but it does not require a validator, collator, relayer, or governance actor — any account satisfying the configured frontend origin (which, depending on runtime configuration, may be a broad/permissionless XCM origin from another chain) can trigger it repeatedly for many nonces.

### Recommendation
Add a check in `Pallet<T>::add_tip` (inbound-queue-v2) that the `nonce` is plausible/pending — e.g., bound it to the current known/expected nonce range, or track pending inbound nonces analogous to `PendingOrders` in the outbound queue, and reject tips for nonces with no tracked pending state, mirroring the `AddTipError::UnknownMessage` guard already used on the outbound side.

### Proof of Concept
1. Caller invokes (through the permitted frontend origin) `add_tip(nonce = 999_999_999, amount = X)` for a `nonce` that has never been used and has no relation to any real inflight Ethereum message.
2. `Nonce::<T>::get(nonce)` returns `false` (never consumed), so the `ensure!` passes.
3. `Tips::<T>::mutate(nonce, ...)` inserts `Some(X)` into storage, permanently occupying storage for a nonce that has no delivered/expected message.
4. If nonce `999_999_999` is later legitimately used, `process_message` will pay out an inflated tip the relayer did not organically earn; if it's never used, the storage entry is permanently orphaned. [4](#0-3)

### Citations

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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L483-496)
```rust
	impl<T: Config> AddTip for Pallet<T> {
		fn add_tip(nonce: u64, amount: u128) -> Result<(), AddTipError> {
			ensure!(amount > 0, AddTipError::AmountZero);
			PendingOrders::<T>::try_mutate_exists(nonce, |maybe_order| -> Result<(), AddTipError> {
				match maybe_order {
					Some(order) => {
						order.fee = order.fee.saturating_add(amount);
						Ok(())
					},
					None => Err(AddTipError::UnknownMessage),
				}
			})
		}
	}
```
