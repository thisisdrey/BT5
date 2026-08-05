### Title
Snowbridge V2 outbound queue accepts an arbitrary, user-controlled relayer fee with no minimum-cost enforcement, enabling underpriced/zero-fee messages that stall bridge delivery - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/send_message_impl.rs`)

### Summary
In Snowbridge V1, `snowbridge_pallet_outbound_queue::calculate_fee` computes a protocol-derived fee from gas usage, exchange rate, and reward parameters before a message is queued [1](#0-0) . In Snowbridge V2, the analogous entrypoint `Pallet::validate` in the outbound-queue-v2 pallet only checks payload size and unconditionally accepts the `Message`, including its `fee` field, without ever computing or enforcing a floor value [2](#0-1) . The `fee` amount itself is derived purely from user-supplied XCM instructions (`WithdrawAsset`/`PayFees`) with no cross-check against the gas required to execute the encoded `commands` on Ethereum [3](#0-2) . This is the same broken invariant as the external report: a fee that is supposed to cover destination-chain (Ethereum) execution cost is capped/set independently of that cost, and here there is no cap or floor check at all on-chain.

### Finding Description
The V2 message-processing path is:
1. `XcmConverter::convert` extracts `fee_amount` solely from the `PayFees` XCM instruction supplied by the caller, with no relation to the actual Ethereum gas cost of the `commands` being encoded [4](#0-3) .
2. `EthereumBlobExporter::validate` (v2) calls `OutboundQueue::validate(&message)` and forwards straight to delivery without adjusting or rejecting for a low fee [5](#0-4) .
3. `Pallet::validate` in `outbound-queue-v2` (the actual `SendMessage` trait impl consumed by the exporter) performs only a `MaxMessagePayloadSize` check — it never inspects `message.fee` against any computed minimum [2](#0-1) .
4. `do_process_message` takes the (unchecked) `fee` straight from the decoded message and stores it verbatim in `PendingOrders` as the relayer's future reward [6](#0-5) .
5. `process_delivery_receipt` only pays out `order.fee` if `> 0`; a zero or near-zero fee is accepted without complaint [7](#0-6) .

Unlike the V1 pallet, which documents and enforces `Fee(Message) = LocalFee + RemoteFeeAdjusted` with a safety multiplier to guard against ETH/DOT fluctuations [8](#0-7) , V2 has removed that protocol-level guard: the fee is whatever the sender's XCM program declares. `GasMeter::maximum_dispatch_gas_used_at_most` is computed for each command at processing time [9](#0-8) , but this gas figure is never compared against `fee`/`fee_per_gas` to reject or top up an underpriced message — it is used only for the ABI-encoded gas limit reported to the Gateway contract, not for fee validation.

### Impact Explanation
Because the fee attached to a V2 message is fully attacker/user-controlled and unchecked against real Ethereum gas costs, any unprivileged account routing an XCM message through Asset Hub → BridgeHub → `EthereumBlobExporterV2` can enqueue messages with a fee far below what is economically required to relay and pay Ethereum gas. Relayers have no financial incentive to submit such messages to Ethereum, so they remain unrelayed. Because `PendingOrders` entries are only removed on `process_delivery_receipt` (i.e., after a proof of successful Ethereum-side delivery) [10](#0-9) , such underpriced/unrelayed messages accumulate indefinitely in the pending-order queue and their locked/withdrawn assets on the sending side are never finalized on Ethereum — a bridge-processing stall and locked-fund condition consistent with "public underpriced work that degrades block production or stalls bridge processing" and "permanent user-fund or bridge-state lock" in the impact gate.

### Likelihood Explanation
No privileged actor is required — any account able to construct and submit an XCM program from Asset Hub through the V2 Snowbridge exporter controls the `PayFees` amount directly. The `AllowedAliasOrigin` filter only restricts which account may be aliased as origin for asset ownership/dispatch purposes [11](#0-10) ; it does not gate or validate the fee amount, so this is a straightforward, always-available public-entrypoint condition, not one requiring a malicious relayer, validator, or governance action.

### Recommendation
In `outbound-queue-v2`'s `Pallet::validate` (or in the V2 `XcmConverter`), compute a minimum required fee from `GasMeter::maximum_dispatch_gas_used_at_most` and current pricing parameters (mirroring V1's `calculate_fee`) and reject (`SendError`) or clamp messages whose declared `fee` is below that floor, restoring an on-chain fee-cap/floor invariant equivalent to the one V1 enforces.

### Proof of Concept
Conceptual reproduction (cannot be executed without a live runtime, but derivable directly from the cited code paths):
1. Construct an XCM program from an allowed `AliasOrigin` on Asset Hub containing `WithdrawAsset` / `PayFees { asset: fee, ... }` with `fee_amount` set to `1` (or any value far below the real Ethereum gas cost for the attached `commands`), followed by a valid `DepositAsset`/`Transact`/`SetTopic` sequence, per the structure parsed in `XcmConverter::convert` [3](#0-2) .
2. Route it to `SnowbridgeExporterV2`; `EthereumBlobExporter::validate` and `outbound_queue_v2::Pallet::validate` both accept it unmodified since only a payload-size check is performed [2](#0-1) .
3. `do_process_message` stores a `PendingOrder { fee: 1, .. }` [6](#0-5) .
4. No rational relayer submits `submit_delivery_receipt` for a 1-unit reward covering real Ethereum gas; the order remains in `PendingOrders` and the message is never delivered, while the source-side assets already withdrawn from the sender remain committed to an undelivered cross-chain operation.

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L38-70)
```rust
//! # Fees
//!
//! An upfront fee must be paid for delivering a message. This fee covers several
//! components:
//! 1. The weight of processing the message locally
//! 2. The gas refund paid out to relayers for message submission
//! 3. An additional reward paid out to relayers for message submission
//!
//! Messages are weighed to determine the maximum amount of gas they could
//! consume on Ethereum. Using this upper bound, a final fee can be calculated.
//!
//! The fee calculation also requires the following parameters:
//! * Average ETH/DOT exchange rate over some period
//! * Max fee per unit of gas that bridge is willing to refund relayers for
//!
//! By design, it is expected that governance should manually update these
//! parameters every few weeks using the `set_pricing_parameters` extrinsic in the
//! system pallet.
//!
//! This is an interim measure. Once ETH/DOT liquidity pools are available in the Polkadot network,
//! we'll use them as a source of pricing info, subject to certain safeguards.
//!
//! ## Fee Computation Function
//!
//! ```text
//! LocalFee(Message) = WeightToFee(ProcessMessageWeight(Message))
//! RemoteFee(Message) = MaxGasRequired(Message) * Params.MaxFeePerGas + Params.Reward
//! RemoteFeeAdjusted(Message) = Params.Multiplier * (RemoteFee(Message) / Params.Ratio("ETH/DOT"))
//! Fee(Message) = LocalFee(Message) + RemoteFeeAdjusted(Message)
//! ```
//!
//! By design, the computed fee includes a safety factor (the `Multiplier`) to cover
//! unfavourable fluctuations in the ETH/DOT exchange rate.
```

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L366-393)
```rust
		/// Calculate total fee in native currency to cover all costs of delivering a message to the
		/// remote destination. See module-level documentation for more details.
		pub(crate) fn calculate_fee(
			gas_used_at_most: u64,
			params: PricingParameters<T::Balance>,
		) -> Fee<T::Balance> {
			// Remote fee in ether
			let fee = Self::calculate_remote_fee(
				gas_used_at_most,
				params.fee_per_gas,
				params.rewards.remote,
			);

			// downcast to u128
			let fee: u128 = fee.try_into().defensive_unwrap_or(u128::MAX);

			// multiply by multiplier and convert to local currency
			let fee = FixedU128::from_inner(fee)
				.saturating_mul(params.multiplier)
				.checked_div(&params.exchange_rate)
				.expect("exchange rate is not zero; qed")
				.into_inner();

			// adjust fixed point to match local currency
			let fee = Self::convert_from_ether_decimals(fee);

			Fee::from((Self::calculate_local_fee(), fee))
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

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs (L217-317)
```rust
	pub fn convert(&mut self) -> Result<Message, XcmConverterError> {
		// Get fee amount
		let fee_amount = self.extract_remote_fee()?;

		// Get ENA reserve asset from WithdrawAsset.
		let mut enas =
			match_expression!(self.peek(), Ok(WithdrawAsset(reserve_assets)), reserve_assets);
		if enas.is_some() {
			let _ = self.next();
		}

		// Get PNA reserve asset from ReserveAssetDeposited
		let pnas = match_expression!(
			self.peek(),
			Ok(ReserveAssetDeposited(reserve_assets)),
			reserve_assets
		);
		if pnas.is_some() {
			let _ = self.next();
		}

		// Try to get ENA again if it is after PNA
		if enas.is_none() {
			enas =
				match_expression!(self.peek(), Ok(WithdrawAsset(reserve_assets)), reserve_assets);
			if enas.is_some() {
				let _ = self.next();
			}
		}
		// Check AliasOrigin.
		let origin_location = match_expression!(self.next()?, AliasOrigin(origin), origin)
			.ok_or(AliasOriginExpected)?;

		// Validate the AliasOrigin using the configured AllowedAliasOrigin filter.
		// This provides a mechanism for the runtime to restrict which origins
		// are permitted to alias, providing defense-in-depth against
		// unprivileged alias attempts.
		ensure!(AllowedAliasOrigin::contains(origin_location), InvalidOrigin);

		let origin = AgentIdOf::convert_location(origin_location).ok_or(InvalidOrigin)?;

		let (deposit_assets, beneficiary) = match_expression!(
			self.next()?,
			DepositAsset { assets, beneficiary },
			(assets, beneficiary)
		)
		.ok_or(DepositAssetExpected)?;

		// assert that the beneficiary is AccountKey20.
		let recipient = match_expression!(
			beneficiary.unpack(),
			(0, [AccountKey20 { network, key }])
				if self.network_matches(network),
			H160(*key)
		)
		.ok_or(BeneficiaryResolutionFailed)?;

		let mut commands: Vec<Command> = Vec::new();

		// ENA transfer commands
		if let Some(enas) = enas {
			commands.append(&mut self.extract_ethereum_native_assets(
				enas,
				deposit_assets,
				recipient,
			)?);
		}

		// PNA transfer commands
		if let Some(pnas) = pnas {
			commands.append(&mut self.extract_polkadot_native_assets(
				pnas,
				deposit_assets,
				recipient,
			)?);
		}

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

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/converter/mod.rs (L149-163)
```rust
		let mut converter =
			XcmConverter::<ConvertAssetId, (), AllowedAliasOrigin>::new(&message, expected_network);
		let message = converter.convert().map_err(|err| {
			tracing::error!(target: TARGET, error=?err, "unroutable due to pattern matching.");
			SendError::Unroutable
		})?;

		// validate the message
		let ticket = OutboundQueue::validate(&message).map_err(|err| {
			tracing::error!(target: TARGET, error=?err, "OutboundQueue validation of message failed.");
			SendError::Unroutable
		})?;

		Ok(((ticket.encode(), XcmHash::from(message.id)), Assets::default()))
	}
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L372-379)
```rust
			let commands: Vec<OutboundCommandWrapper> = commands
				.into_iter()
				.map(|command| OutboundCommandWrapper {
					kind: command.index(),
					gas: T::GasMeter::maximum_dispatch_gas_used_at_most(&command),
					payload: command.abi_encode(),
				})
				.collect();
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L426-436)
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
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L462-475)
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

			<PendingOrders<T>>::remove(nonce);
```
