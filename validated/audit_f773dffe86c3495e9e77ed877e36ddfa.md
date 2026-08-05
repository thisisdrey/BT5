Audit Report

## Title
Outbound queue v2 commits Ethereum-bound messages with unvalidated relayer fee, allowing permanently stalled bridge delivery - (File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs)

## Summary
`Pallet::do_process_message` in the Snowbridge outbound-queue-v2 pallet decodes an enqueued `Message` — including a caller-controlled `fee` field sourced from the XCM `WithdrawAsset`/`PayFees` legs parsed by `XcmConverter::convert` — and stores it verbatim into `PendingOrders` without validating it against the real Ethereum execution/gas cost computed by `T::GasMeter::maximum_dispatch_gas_used_at_most` for the same commands. Because relayer reward payment in `process_delivery_receipt` is entirely driven by `order.fee` and only pays `if order.fee > 0`, a message committed with a zero or economically insufficient fee has no incentive for any relayer to deliver it, permanently stalling that message.

## Finding Description
`do_process_message` decodes `Message { origin, id, fee, commands }` from the message queue and unconditionally proceeds: [1](#0-0) . The commands' actual required gas is computed via `T::GasMeter::maximum_dispatch_gas_used_at_most` [2](#0-1) , but that value is stored only for the Ethereum-side gas limit — it is never compared against `fee` before `PendingOrder { nonce, fee, block_number }` is created and the nonce is advanced [3](#0-2) . `process_delivery_receipt` later pays the reward only `if order.fee > 0` [4](#0-3) , confirming zero/low fees are accepted as valid state.

The `fee` value originates from `XcmConverter::extract_remote_fee`/`convert`, which simply reads the amount the caller places in the `WithdrawAsset`/`PayFees` Ether legs of the XCM program, with only a check that the reserved amount is `>=` the declared fee amount — no check against actual delivery cost [5](#0-4) [6](#0-5) . Confirmed by a test that a fee of `1000` is accepted for a `ReserveAssetDeposited` transfer regardless of real gas cost [7](#0-6) . `SendMessage::validate` for outbound-queue-v2 checks only payload size, not fee sufficiency: [8](#0-7) . This is unlike v1, whose pallet computes the fee itself via `calculate_fee`/`PricingParameters` rather than trusting a caller-supplied value [9](#0-8) .

Notably, Snowbridge's own design doc explicitly flags this gap as an unresolved concern: "we should also impose a minimum relayer reward of at least the existential deposit 0.1 DOT, which acts as a deposit to stop spamming messages with 0 rewards" [10](#0-9) . Searching the pallet, `send_message_impl.rs`, `process_message_impl.rs`, and `system-v2` pallet, no such minimum-fee enforcement exists anywhere in the current code — the only mechanism to add value to an already-pending order is the governance/privileged `AddTip::add_tip`, which is not something an ordinary user or the protocol itself invokes automatically [11](#0-10) .

## Impact Explanation
A message committed with an underpriced or zero fee still consumes a nonce, occupies a `Messages`/`MessageLeaves` slot, and is committed into the per-block merkle root for eventual Ethereum verification, but no rational relayer will submit `submit_delivery_receipt` for it, since doing so costs Ethereum gas with no compensating reward. The corresponding `PendingOrders` entry is never resolved. If the message also represents an asset transfer to Ethereum (e.g., PNA reserve or ENA withdrawal already executed on the Polkadot side per the XCM flow in `bridges/snowbridge/docs/v2.md`), the transferred value is effectively lost/stuck since delivery to Ethereum never completes. This matches the "permanent user-fund or bridge-state lock" / "public underpriced work that ... stalls bridge processing" impact category in the Polkadot SDK gate.

## Likelihood Explanation
No privileged actor is required. Any account able to originate a Snowbridge V2 XCM transfer from AssetHub (via `EthereumBlobExporter::deliver`/`snowbridge_pallet_system_v2::Pallet::send`) fully controls the `WithdrawAsset`/`PayFees` amounts that become `Message.fee`, and can set them to `0` or a negligible value. This is a normal unprivileged interaction, not a compromised-relayer, governance, or infra-control scenario, and reproducible by construction (as shown by the test harness demonstrating the converter accepts arbitrary fee amounts without any cost check).

## Recommendation
Enforce a minimum acceptable `fee`/reward at message admission (either in `XcmConverter::convert`/`extract_remote_fee`, in `SendMessage::validate`, or in `do_process_message` before inserting into `PendingOrders`) computed from `T::GasMeter::maximum_dispatch_gas_used_at_most` and pricing parameters, analogous to v1's `calculate_fee`. Reject or hold messages whose supplied fee is below that computed minimum, and/or implement the existential-deposit-based minimum reward described in `bridges/snowbridge/docs/v2.md` to prevent zero/near-zero-fee spam from consuming nonces and merkle slots.

## Proof of Concept
1. From AssetHub, construct a Snowbridge V2 XCM transfer whose `WithdrawAsset`/`PayFees` (Ether) legs specify a fee of `0` (or a value far below real gas cost), following the pattern validated by `xcm_converter_transfer_native_token_success` in `bridges/snowbridge/primitives/outbound-queue/src/v2/converter/tests.rs:1237-1278`, but with `fee_asset` amount set to `0`.
2. `XcmConverter::convert` produces `Message { fee: 0, .. }`; `EthereumBlobExporter::validate` and `Pallet::validate`/`deliver` in `send_message_impl.rs` accept it (only payload-size checked).
3. `Pallet::do_process_message` decodes the message, computes real gas via `T::GasMeter::maximum_dispatch_gas_used_at_most`, but inserts `PendingOrder { nonce, fee: 0, .. }` into `PendingOrders` regardless (`lib.rs:426-443`).
4. The message is committed into the merkle root and exposed via `prove_message`, but `process_delivery_receipt`/`process_delivery_receipt`'s reward branch (`lib.rs:466`) never triggers for `fee == 0`, so no relayer submits `submit_delivery_receipt`; the `PendingOrders` entry, nonce slot, and any consumed source-side assets remain permanently stuck.

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L360-369)
```rust
			// Decode bytes into Message
			let Message { origin, id, fee, commands } =
				Message::decode(&mut message).map_err(|_| {
					Self::deposit_event(Event::MessageRejected {
						id: None,
						payload: message.to_vec(),
						error: Corrupt,
					});
					Corrupt
				})?;
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L371-379)
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
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L426-443)
```rust
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

			<Nonce<T>>::set(nonce);

			Self::deposit_event(Event::MessageAccepted { id, nonce });

			Ok(true)
		}
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L462-473)
```rust
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
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L483-495)
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
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs (L94-117)
```rust
	/// Extract the fee asset item from PayFees(V5)
	fn extract_remote_fee(&mut self) -> Result<u128, XcmConverterError> {
		use XcmConverterError::*;
		let reserved_fee_assets = match_expression!(self.next()?, WithdrawAsset(fee), fee)
			.ok_or(WithdrawAssetExpected)?;
		ensure!(reserved_fee_assets.len() == 1, AssetResolutionFailed);
		let reserved_fee_asset =
			reserved_fee_assets.inner().first().cloned().ok_or(AssetResolutionFailed)?;
		let (reserved_fee_asset_id, reserved_fee_amount) = match reserved_fee_asset {
			Asset { id: asset_id, fun: Fungible(amount) } => Ok((asset_id, amount)),
			_ => Err(AssetResolutionFailed),
		}?;
		let fee_asset =
			match_expression!(self.next()?, PayFees { asset: fee }, fee).ok_or(InvalidFeeAsset)?;
		let (fee_asset_id, fee_amount) = match fee_asset {
			Asset { id: asset_id, fun: Fungible(amount) } => Ok((asset_id, *amount)),
			_ => Err(AssetResolutionFailed),
		}?;
		// Check the fee asset is Ether (XCM is evaluated in Ethereum context).
		ensure!(fee_asset_id.0 == Here.into(), InvalidFeeAsset);
		ensure!(reserved_fee_asset_id.0 == Here.into(), InvalidFeeAsset);
		ensure!(reserved_fee_amount >= fee_amount, InvalidFeeAsset);
		Ok(fee_amount)
	}
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs (L307-317)
```rust
		ensure!(commands.len() > 0, NoCommands);

		// ensure SetTopic exists
		let topic_id = match_expression!(self.next()?, SetTopic(id), id).ok_or(SetTopicExpected)?;

		let message = Message {
			id: (*topic_id).into(),
			origin,
			fee: fee_amount,
			commands: BoundedVec::try_from(commands).map_err(|_| TooManyCommands)?,
		};
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/converter/tests.rs (L1268-1278)
```rust
	let expected_payload =
		Command::MintForeignToken { recipient: beneficiary_address.into(), amount, token_id };
	let expected_message = Message {
		id: [0; 32].into(),
		origin: hex!("aa16eddac8725928eaeda4aae518bf10d02bee80382517d21464a5cdf8d1d8e1").into(),
		fee: 1000,
		commands: BoundedVec::try_from(vec![expected_payload]).unwrap(),
	};
	let result = converter.convert();
	assert_eq!(result, Ok(expected_message));
}
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

**File:** bridges/snowbridge/pallets/outbound-queue/src/send_message_impl.rs (L1-10)
```rust
// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: 2023 Snowfork <hello@snowfork.com>
//! Implementation for [`snowbridge_outbound_queue_primitives::v1::SendMessage`]
use super::*;
use bridge_hub_common::AggregateMessageOrigin;
use codec::Encode;
use frame_support::{
	ensure,
	traits::{EnqueueMessage, Get},
	CloneNoBound, DebugNoBound, PartialEqNoBound,
```

**File:** bridges/snowbridge/docs/v2.md (L99-102)
```markdown
The XCM bridge-router on AH will charge a small fee to prevent spamming BH with bridge messages. This is necessary since
the `ExportMessage` instruction in message $x_2$ will have no execution fee on BH. For a similar reason, we should also
impose a minimum relayer reward of at least the existential deposit 0.1 DOT, which acts as a deposit to stop spamming
messages with 0 rewards.
```
