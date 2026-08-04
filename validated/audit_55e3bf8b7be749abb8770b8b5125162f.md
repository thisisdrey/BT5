### Title
Inbound Queue V2 `add_tip` Accepts Tips For Any Unverified Nonce, Permanently Locking Burned Bridge Funds - (File: bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs)

### Summary
The external report's core defect is accepting data as valid without verifying that it corresponds to a real, currently-relevant record (Chainlink's `latestRoundData` was consumed without checking that the round is fresh/legitimate). The local analog is `Pallet::<T>::add_tip` in `snowbridge-pallet-inbound-queue-v2`, which accepts a tip for *any* `nonce` value as long as it has not already been marked "consumed" in the `NonceBitmap`, without any check that a message with that nonce will actually ever be delivered from Ethereum. This is unlike the sibling `snowbridge-pallet-outbound-queue-v2::add_tip`, which correctly validates against an existing `PendingOrders` entry before accepting a tip.

### Finding Description
`AddTip::add_tip` for the inbound queue is: [1](#0-0) 

The only guard is `!Nonce::<T>::get(nonce)`, i.e. the nonce bit is not yet set in `NonceBitmap`. This is true for *every* nonce that hasn't been processed yet — including nonces that are far beyond what the Ethereum Gateway will ever emit (e.g., an arbitrary large or mistaken nonce), because the bitmap has no notion of "currently pending" vs "will exist" vs "never will exist". Contrast this with the outbound queue's implementation, which requires the nonce to correspond to an actual, currently-pending order: [2](#0-1) 

The tip is only ever consumed/released inside `process_message`, when a message with the matching `nonce` is actually decoded and dispatched from Ethereum: [3](#0-2) 

There is no dispatchable or code path to reclaim a tip stored in `Tips::<T>` if the target `nonce` is never delivered (e.g., because it was chosen incorrectly, is out of the actual sequence, or the corresponding message on Ethereum is never sent/relayed). The public entry point that ultimately reaches this trait method is `snowbridge-pallet-system-v2::Pallet::add_tip`, gated only by `T::FrontendOrigin`, which is satisfied by ordinary signed AssetHub user origin via XCM (as exercised in the emulated tests): [4](#0-3) [5](#0-4) 

On the sender side (AssetHub), the tip asset is swapped to Ether and the Ether is burned for teleportation to BridgeHub before the XCM `add_tip` call even executes: [6](#0-5) 

Because the inbound queue's `add_tip` unconditionally returns `Ok(())` for any not-yet-consumed nonce (rather than `Err(AddTipError::UnknownMessage)` like the outbound queue does for unknown nonces), the `system-v2::add_tip` handler treats the call as fully successful and does **not** record it into `LostTips`: [7](#0-6) 

This means the already-burned/teleported Ether is stored in `Tips::<T>` keyed to a nonce that will never be reached (or was mistakenly targeted), and it is neither refunded, nor tracked as a recognized loss, nor ever claimable — a silent, permanent value lock that existing guards do not catch, because those guards only check "not yet consumed", not "does/will this nonce correspond to a real message".

### Impact Explanation
This causes permanent loss/lock of bridged funds (Ether, ultimately backed by real user assets burned/teleported from AssetHub) with no recovery path, matching the "permanent user-fund or bridge-state lock" impact category. It is reachable by any ordinary, unprivileged AssetHub user through the standard `add_tip` XCM flow — no relayer, validator, governance, or admin involvement is required.

### Likelihood Explanation
High: the flaw is triggered by ordinary user error (choosing a wrong/future nonce) or a targeted call using a nonce that is known to be unreachable/incorrect, and requires no special privileges, timing races, or cooperation from any other party. The public, cross-chain `add_tip` flow is fully exercised in the repository's own integration tests, confirming this code path is live and reachable in production configuration.

### Recommendation
Align `snowbridge-pallet-inbound-queue-v2::add_tip` with the outbound-queue-v2 pattern: only accept a tip if the nonce corresponds to a currently known/valid future message context (e.g., track and validate against an actual pending/expected nonce range, or require the message to already exist in a "pending" state before allowing a tip), and return `AddTipError::UnknownMessage` (so the `system-v2` handler records it into `LostTips`) for any nonce that cannot be validated as a real, deliverable inbound message.

### Proof of Concept
1. On AssetHub, a user calls `SnowbridgeSystemFrontend::add_tip` with `MessageId::Inbound(nonce)` where `nonce` is chosen far outside the range that the Ethereum Gateway's outbound nonce counter will ever reach (or simply a value that will never actually be delivered).
2. `system-frontend::swap_and_burn` swaps and burns real assets into Ether and teleports it to BridgeHub.
3. On BridgeHub, `EthereumSystemV2::add_tip` forwards to `InboundQueue::add_tip(nonce, amount)`.
4. Because `Nonce::<T>::get(nonce)` is `false` (bit unset) for this arbitrary nonce, `add_tip` returns `Ok(())` and stores the amount in `Tips::<T>` under that nonce.
5. Since no message with this `nonce` will ever be relayed from Ethereum, `Tips::<T>::take(nonce)` in `process_message` is never invoked for this key; the value is never released to any relayer and is unrecoverable by the depositor — a silent, permanent fund lock, confirmed by the divergence from the `UnknownMessage`-checked behavior implemented in `outbound-queue-v2::add_tip`.

### Citations

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

**File:** bridges/snowbridge/pallets/system-v2/src/lib.rs (L251-271)
```rust
		#[pallet::call_index(3)]
		#[pallet::weight(<T as pallet::Config>::WeightInfo::add_tip())]
		pub fn add_tip(
			origin: OriginFor<T>,
			sender: AccountIdOf<T>,
			message_id: MessageId,
			amount: u128,
		) -> DispatchResult {
			T::FrontendOrigin::ensure_origin(origin)?;

			let result = match message_id {
				Inbound(nonce) => <T as pallet::Config>::InboundQueue::add_tip(nonce, amount),
				Outbound(nonce) => <T as pallet::Config>::OutboundQueue::add_tip(nonce, amount),
			};

			if let Err(ref e) = result {
				tracing::debug!(target: LOG_TARGET, ?e, ?message_id, ?amount, "error adding tip");
				LostTips::<T>::mutate(&sender, |lost_tip| {
					*lost_tip = lost_tip.saturating_add(amount);
				});
			}
```

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_inbound.rs (L1366-1386)
```rust
#[test]
pub fn add_tip_from_asset_hub_user_origin() {
	fund_on_bh();
	register_assets_on_ah();
	fund_on_ah();
	set_up_eth_and_dot_pool();
	let relayer = AssetHubWestendSender::get();

	// Add the tip to a nonce that has not been processed.
	let tip_message_id = MessageId::Inbound(2);

	let dot = Location::new(1, Here);
	AssetHubWestend::execute_with(|| {
		type RuntimeOrigin = <AssetHubWestend as Chain>::RuntimeOrigin;

		assert_ok!(<AssetHubWestend as AssetHubWestendPallet>::SnowbridgeSystemFrontend::add_tip(
			RuntimeOrigin::signed(relayer.clone()),
			tip_message_id.clone(),
			xcm::prelude::Asset::from((dot, 1_000_000_000u128)),
		));
	});
```

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L287-317)
```rust
		/// Swaps a specified tip asset to Ether and then burns the resulting ether for
		/// teleportation. Returns the amount of Ether gained if successful, or a DispatchError if
		/// any step fails.
		fn swap_and_burn(
			origin: Location,
			tip_asset_location: Location,
			ether_location: Location,
			tip_amount: u128,
		) -> Result<u128, DispatchError> {
			// Swap tip asset to ether
			let swap_path = vec![tip_asset_location.clone(), ether_location.clone()];
			let who = T::AccountIdConverter::convert_location(&origin)
				.ok_or(Error::<T>::LocationConversionFailed)?;

			let ether_gained = T::Swap::swap_exact_tokens_for_tokens(
				who.clone(),
				swap_path,
				tip_amount,
				None, // No minimum amount required
				who,
				true,
			)?;

			// Burn the ether
			let ether_asset = Asset::from((ether_location.clone(), ether_gained));

			burn_for_teleport::<T::AssetTransactor>(&origin, &ether_asset)
				.map_err(|_| Error::<T>::BurnError)?;

			Ok(ether_gained)
		}
```
