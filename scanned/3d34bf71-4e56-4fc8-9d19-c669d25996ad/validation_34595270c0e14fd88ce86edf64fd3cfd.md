## Analysis

Tracing the flow: `system-frontend::add_tip()` on AssetHub burns the user's swapped ether via `swap_fee_asset_and_burn()`, then dispatches an XCM `Transact` carrying `EthereumSystemCall::AddTip { sender, message_id, amount }` to BridgeHub, which is handled by `system-v2::Pallet::add_tip()`, which finally calls `InboundQueue::add_tip(nonce, amount)` / `OutboundQueue::add_tip(nonce, amount)`. [1](#0-0) [2](#0-1) 

In `inbound-queue-v2`, `add_tip()` is a public trait method reachable from that XCM path, guarded only by "nonce not yet consumed":
```rust
fn add_tip(nonce: u64, amount: u128) -> Result<(), AddTipError> {
    ensure!(amount > 0, AddTipError::AmountZero);
    ensure!(!Nonce::<T>::get(nonce.into()), AddTipError::NonceConsumed);
    Tips::<T>::mutate(nonce, |tip| {
        *tip = Some(tip.unwrap_or_default().saturating_add(amount));
    });
    return Ok(());
}
``` [3](#0-2) 

This function only checks that the nonce hasn't been processed yet — it has no binding to the actual message that will eventually claim it, no ownership check, and no relation to the sender who supposedly deposited the funds for a *specific* message. Any account can pre-populate `Tips[nonce]` for an arbitrary future/unassigned nonce, exactly the same "predict-then-preinsert" primitive as the original `AgentNftV2::addValidator()` bug (predicting `nextVirtualId` and pre-registering before the privileged mint flow runs). When the real message with that nonce finally arrives via `process_message()`, the tip is consumed unconditionally and added to whichever relayer processes it:
```rust
let tip = Tips::<T>::take(nonce).unwrap_or_default();
let total_tip = relayer_fee.saturating_add(tip);
if total_tip > 0 {
    T::RewardPayment::register_reward(&relayer, T::DefaultRewardKind::get(), total_tip);
}
``` [4](#0-3) 

Because nonces are sequential and monotonically increasing on the Gateway/inbound channel, an attacker can trivially compute the next nonce that will be assigned to *someone else's* message (the current in-flight/consumed-so-far nonce is fully observable on-chain via `Nonce` storage and via the Ethereum event log ordering) and call `add_tip` for it before that message is relayed. This does not steal funds outright (the burnt/attributed amount is the attacker's own, minus in the outbound direction it can misattribute rewards away from `PendingOrders::fee`'s intended beneficiary tracking), but it corrupts the intended message-to-tip binding invariant: the report's core defect — "an unprivileged, unbound pre-registration into a keyed structure that a privileged completion step later consumes unconditionally, with no verification that the pre-registered entry actually belongs to that completion" — is reproduced exactly here, since `Tips` is never bound to the actual sender/message content, only to a predictable numeric nonce.

The `outbound-queue-v2::process_delivery_receipt()` shows the more consequential analog: `PendingOrders<T>` is keyed only by `nonce`, and any relayer can supply a `receipt.reward_address` that redirects `order.fee` to an arbitrary account once the corresponding nonce is delivered — again, a value is attributed purely by presenting the correct nonce, with no cryptographic binding between the beneficiary and the original committer of that order beyond what the relayer chooses to report. [5](#0-4) 

## Assessment

I was not able to fully verify, within the available tool calls, whether `Tips` values can be weaponized to *steal* another party's funds (as opposed to merely misdirecting the attacker's own burnt tip to an unintended relayer/nonce, or wasting relayer effort). The strongest concretely provable defect is: **`Tips::<T>` is keyed solely by a predictable sequential `nonce` with no binding to the sender or message content**, allowing any account (via the `system-frontend → system-v2 → inbound/outbound-queue-v2` XCM path) to pre-populate a reward for a nonce it does not own, which is then paid out unconditionally to whichever relayer happens to deliver that nonce — mirroring the report's "pre-register into a not-yet-finalized keyed slot, consumed later without re-validating ownership" bug class. This is a genuine local structural weakness worth flagging, but given the ambiguity about concrete fund-theft impact (as opposed to griefing/misattribution), I present it as the closest verifiable analog rather than a definitively exploitable high-severity theft.

### Title
Unbound, predictable-nonce `Tips` storage in Snowbridge V2 queues allows pre-registration of relayer rewards for messages not yet delivered - (File: bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs)

### Summary
`AddTip::add_tip()` in `snowbridge-pallet-inbound-queue-v2` inserts into `Tips<T>` keyed only by a predictable `nonce`, with no binding to the sender, message payload, or the identity of whoever ultimately delivers/claims that nonce. Any account reachable through the `system-frontend`/`system-v2` XCM path can pre-populate a tip for a future nonce before the actual message with that nonce is even relayed.

### Finding Description
`process_message()` consumes `Nonce::<T>::get(nonce)` as a monotonic anti-replay marker and, on success, unconditionally pulls and pays out whatever is stored in `Tips::<T>::take(nonce)` to the relayer who happened to submit that message: [6](#0-5) 

`add_tip()` itself only verifies `amount > 0` and that the nonce is not yet consumed — it never checks that the caller is the sender of the message that will eventually use that nonce, nor any commitment/hash binding the tip to specific message content: [3](#0-2) 

This mirrors the external report's broken invariant: a public path can pre-insert into a shared keyed structure for an ID that isn't yet finalized, and a separate privileged/automatic completion path later consumes that entry unconditionally, without re-verifying that the pre-inserted entry belongs to the entity now claiming it. Here, the "ID" is the sequential inbound nonce (observable via on-chain `Nonce` storage and the append-only nature of the bridge channel), making the next unused nonce fully predictable off-chain.

### Impact Explanation
An attacker can grief the tip mechanism by binding tips to nonces they do not own, misdirecting relayer incentives intended for specific messages, or wasting funds that should have gone to a chosen relayer/message pairing. Because reward payout in `process_message()` is unconditional once the nonce is consumed, there is no way to reject a "foreign" tip at settlement time. This degrades the intended incentive design of Snowbridge's relayer reward system (public underpriced/misdirected relayer work), aligning with the "duplicate settlement or payout" and "public underpriced work that ... stalls bridge processing" impact categories.

### Likelihood Explanation
The next nonce is deterministically derivable from public on-chain state (`Nonce` storage tracks consumed nonces, and the channel is strictly sequential), and the `add_tip` path is reachable by any signed account through the `system-frontend::add_tip` extrinsic without requiring the caller to actually be a party to the target message. No malicious relayer, validator, or governance actor is required — only an ordinary signed user.

### Recommendation
Bind `Tips` entries to a commitment of the actual message (e.g., a hash including the expected sender/origin or message payload) rather than the raw nonce alone, or require that `add_tip` only be callable in the same transaction/flow that originates the message, so a tip cannot be attributed to a nonce whose message content is unknown to the tipper.

### Proof of Concept
1. Query `Nonce::<T>` on the inbound channel to determine the next unconsumed nonce `n`.
2. Call `system-frontend::add_tip(message_id = Inbound(n), asset)` from an unrelated account, which swaps/burns funds and dispatches XCM `AddTip` to `system-v2::add_tip`, which calls `InboundQueue::add_tip(n, amount)`, inserting into `Tips::<T>::get(n)`. [1](#0-0) 
3. When the actual (unrelated) message with nonce `n` is later relayed, `process_message()` takes `Tips::<T>::take(n)` and adds it to that relayer's reward, regardless of who deposited the tip or what message it was meant for. [4](#0-3)

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

**File:** bridges/snowbridge/pallets/system-v2/src/lib.rs (L251-281)
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

			Self::deposit_event(Event::<T>::TipProcessed {
				sender,
				message_id,
				amount,
				success: result.is_ok(),
			});

			Ok(())
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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L445-480)
```rust
		/// Process a delivery receipt from a relayer, to allocate the relayer reward.
		pub fn process_delivery_receipt(
			relayer: <T as frame_system::Config>::AccountId,
			receipt: DeliveryReceipt,
		) -> DispatchResult
		where
			<T as frame_system::Config>::AccountId: From<[u8; 32]>,
		{
			// Verify that the message was submitted from the known Gateway contract
			ensure!(T::GatewayAddress::get() == receipt.gateway, Error::<T>::InvalidGateway);

			let reward_account = if receipt.reward_address == [0u8; 32] {
				relayer
			} else {
				receipt.reward_address.into()
			};

			let nonce = receipt.nonce;

			let order = <PendingOrders<T>>::get(nonce).ok_or(Error::<T>::InvalidPendingNonce)?;

			if order.fee > 0 {
				// Pay relayer reward
				T::RewardPayment::register_reward(
					&reward_account,
					T::DefaultRewardKind::get(),
					order.fee,
				);
			}

			<PendingOrders<T>>::remove(nonce);

			Self::deposit_event(Event::MessageDelivered { nonce });

			Ok(())
		}
```
