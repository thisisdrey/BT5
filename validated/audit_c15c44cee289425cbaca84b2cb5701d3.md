Audit Report

## Title
Unbounded, attacker-controlled `Command::CallContract.gas` in Snowbridge outbound-queue v2 has no upper bound check, allowing permanent lock of `PendingOrders` fee and relayer griefing - ([File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs])

## Summary
An unprivileged XCM sender can embed a `Transact` instruction carrying `ContractCall::V1 { gas, .. }` with an arbitrary `u64` value (up to `u64::MAX`). This value passes unchecked through `XcmConverter::convert` into `Command::CallContract{ gas, .. }`, and `ConstantGasMeter::maximum_dispatch_gas_used_at_most` returns it verbatim (unlike every other command variant, which is hard-coded), so it is committed as-is into `OutboundCommandWrapper`/`CommandWrapper.gas` in `Messages`/`MessageLeaves`. No code path anywhere in this pipeline caps `gas` against a realistic Ethereum execution/block gas limit.

## Finding Description
`XcmConverter::convert` decodes the `Transact` payload as `ContractCall` and forwards the attacker-supplied `gas` field directly into `Command::CallContract`: [1](#0-0) 

`ConstantGasMeter::maximum_dispatch_gas_used_at_most` treats every other `Command` variant with a fixed constant (`40_000`, `50_000 + initializer.maximum_required_gas`, `200_000`, `1_200_000`, `100_000`), but for `CallContract` it simply returns the caller-supplied `gas_limit` unmodified: [2](#0-1) 

In `do_process_message`, this unvalidated gas value is written directly into `OutboundCommandWrapper.gas` (stored in `Messages`) and `CommandWrapper.gas` (ABI-encoded and Merkle-rooted into `MessageLeaves`), and a `PendingOrder{ nonce, fee, .. }` is created for the message: [3](#0-2) 

There is no size/value check on `gas` anywhere in this flow — `SendMessage::validate` in `send_message_impl.rs` only checks the encoded payload byte length (`MaxMessagePayloadSize`), not the semantic validity of any field: [4](#0-3) 

`process_delivery_receipt` (invoked from `submit_delivery_receipt`) only removes/settles a `PendingOrders` entry after a relayer supplies a valid Ethereum event-log proof of successful execution; there is no alternate path to release a stuck order: [5](#0-4) 

## Impact Explanation
If `gas` exceeds what the Ethereum Gateway/destination block can accommodate, the relayer's delivery transaction on Ethereum will fail/revert, and no success event log will ever be produced for that nonce. Since `submit_delivery_receipt`/`process_delivery_receipt` is the only mechanism that resolves and removes a `PendingOrders` entry, such a message permanently strands its `PendingOrders[nonce]` entry (and the associated `fee`), matching the "permanent... bridge-state lock" and "public underpriced work that... stalls bridge processing" impact categories. This is a real gap in defense-in-depth: all sibling command variants are deliberately hard-capped by constants specifically to prevent this class of issue, while `CallContract` alone passes the caller value through unchecked.

## Likelihood Explanation
This requires only ordinary, unprivileged XCM execution capability to route a message through the Snowbridge V2 exporter with a `Transact` instruction encoding `ContractCall::V1{ gas: <huge value>, .. }` — no governance, admin, or validator privilege is needed. The existing integration test harness demonstrates the exact mechanics of constructing and submitting such a `ContractCall::V1` via ordinary XCM instructions, confirming the code path is reachable by design. The additional cost to the attacker is bounded by the message's declared fee, not by any gas-proportional economic penalty enforced in this pallet, so the barrier to grief the queue is low.

## Recommendation
Add an explicit upper-bound check on `gas` in `XcmConverter::convert` (reject the message if `gas` exceeds a configured maximum) and/or clamp/reject in `ConstantGasMeter::maximum_dispatch_gas_used_at_most` for `Command::CallContract`, consistent with a realistic Ethereum block gas limit, mirroring the fixed ceilings already applied to every other `Command` variant. The same treatment should be considered for `Command::Upgrade`'s unconditionally-added `initializer.maximum_required_gas`.

## Proof of Concept
1. Construct an XCM message per the existing test harness (`WithdrawAsset`/`PayFees`/`AliasOrigin`/`DepositAsset`/`Transact{ call: ContractCall::V1{ target, calldata, value: 0, gas: u64::MAX }.encode() }`/`SetTopic`) and submit it via any exporter entry point reachable by an unprivileged origin. [1](#0-0) 
2. `XcmConverter::convert` accepts it unmodified, producing `Command::CallContract{ gas: u64::MAX, .. }`.
3. `do_process_message` commits `OutboundCommandWrapper{ gas: u64::MAX, .. }` into `Messages`/`MessageLeaves` and creates a `PendingOrder` for the nonce. [6](#0-5) 
4. A relayer's delivery transaction to the Ethereum Gateway fails because the declared gas exceeds any feasible block gas limit.
5. `submit_delivery_receipt` can never succeed for this nonce (no success event log exists to prove), so `PendingOrders[nonce]` and its `fee` remain permanently stuck, and a unit/integration test asserting `ConstantGasMeter::maximum_dispatch_gas_used_at_most(&Command::CallContract{ gas: u64::MAX, .. })` returns `u64::MAX` unchanged would confirm the missing bound.

### Citations

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs (L294-305)
```rust
		// Transact commands
		let transact_call = match_expression!(self.peek(), Ok(Transact { call, .. }), call);
		if let Some(transact_call) = transact_call {
			let _ = self.next();
			let transact =
				ContractCall::decode_all(&mut transact_call.clone().into_encoded().as_slice())
					.map_err(|_| TransactDecodeFailed)?;
			match transact {
				ContractCall::V1 { target, calldata, gas, value } => commands
					.push(Command::CallContract { target: target.into(), calldata, gas, value }),
			}
		}
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/message.rs (L291-306)
```rust
impl GasMeter for ConstantGasMeter {
	fn maximum_dispatch_gas_used_at_most(command: &Command) -> u64 {
		match command {
			Command::SetOperatingMode { .. } => 40_000,
			Command::Upgrade { initializer, .. } => {
				// total maximum gas must also include the gas used for updating the proxy before
				// the the initializer is called.
				50_000 + initializer.maximum_required_gas
			},
			Command::UnlockNativeToken { .. } => 200_000,
			Command::RegisterForeignToken { .. } => 1_200_000,
			Command::MintForeignToken { .. } => 100_000,
			Command::CallContract { gas: gas_limit, .. } => *gas_limit,
		}
	}
}
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L371-436)
```rust
			// Convert it to OutboundMessage and save into Messages storage
			let commands: Vec<OutboundCommandWrapper> = commands
				.into_iter()
				.map(|command| OutboundCommandWrapper {
					kind: command.index(),
					gas: T::GasMeter::maximum_dispatch_gas_used_at_most(&command),
					payload: command.abi_encode(),
				})
				.collect();

			let nonce = <Nonce<T>>::get().checked_add(1).ok_or_else(|| {
				Self::deposit_event(Event::MessageRejected {
					id: None,
					payload: message.to_vec(),
					error: Unsupported,
				});
				Unsupported
			})?;

			let outbound_message = OutboundMessage {
				origin,
				nonce,
				topic: id,
				commands: commands.clone().try_into().map_err(|_| {
					Self::deposit_event(Event::MessageRejected {
						id: Some(id),
						payload: message.to_vec(),
						error: Corrupt,
					});
					Corrupt
				})?,
			};
			Messages::<T>::append(outbound_message);

			// Convert it to an OutboundMessageWrapper (in ABI format), hash it using Keccak256 to
			// generate a committed hash, and store it in MessageLeaves storage which can be
			// verified on Ethereum later.
			let abi_commands: Vec<CommandWrapper> = commands
				.into_iter()
				.map(|command| CommandWrapper {
					kind: command.kind,
					gas: command.gas,
					payload: Bytes::from(command.payload),
				})
				.collect();
			let committed_message = OutboundMessageWrapper {
				origin: FixedBytes::from(origin.as_fixed_bytes()),
				nonce,
				topic: FixedBytes::from(id.as_fixed_bytes()),
				commands: abi_commands,
			};
			let message_abi_encoded_hash =
				<T as Config>::Hashing::hash(&committed_message.abi_encode());
			MessageLeaves::<T>::append(message_abi_encoded_hash);

			// Generate `PendingOrder` with fee attached in the message, stored
			// into the `PendingOrders` map storage, with assigned nonce as the key.
			// When the message is processed on ethereum side, the relayer will send the nonce
			// back with delivery proof, only after that the order can
			// be resolved and the fee will be rewarded to the relayer.
			let order = PendingOrder {
				nonce,
				fee,
				block_number: frame_system::Pallet::<T>::current_block_number(),
			};
			<PendingOrders<T>>::insert(nonce, order);
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L446-470)
```rust
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
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/send_message_impl.rs (L23-32)
```rust
	fn validate(message: &Message) -> Result<Self::Ticket, SendError> {
		// The inner payload should not be too large
		let payload = message.encode();
		ensure!(
			payload.len() < T::MaxMessagePayloadSize::get() as usize,
			SendError::MessageTooLarge
		);

		Ok(message.clone())
	}
```
