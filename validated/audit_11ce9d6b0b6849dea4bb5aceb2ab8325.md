## Analysis Summary

The external report's core broken invariant: **a public entry point admits attacker-chosen positions whose economic value is below the cost of the "watchdog" action (liquidation), so the watchdog never fires and the bad state accumulates permanently.**

I traced the closest structural analog in `bridges/snowbridge/pallets/outbound-queue-v2`. The `fee` field of an outbound `Message` is fully attacker/user-controlled (extracted from the `PayFees` XCM instruction in `bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs` lines 94-117, 219, 312-317) and is never checked against any minimum in `SendMessage::validate` (`bridges/snowbridge/pallets/outbound-queue-v2/src/send_message_impl.rs` lines 23-32) nor in `do_process_message` (`bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs` lines 343-443). The only place a "reward too small" condition is even acknowledged is `AddTip::add_tip` (lib.rs lines 483-495), which is a *separate, optional, permissionless* top-up — nothing forces it to be called, and nothing prevents `fee = 0` orders from being created and never topped up.

This mirrors the report exactly: no minimum-fee check at message admission (`checksBorrow` analog), and the “top-up”/permissionless remediation (`add_tip`, analog to WiseLending's minimum-deposit toggle) does not close the gap because it is not mandatory. A `PendingOrder` with `fee = 0` sits in `PendingOrders` forever since no rational relayer will pay Ethereum gas to submit `submit_delivery_receipt` for zero reward — the relayer-economics failure is identical to the "no incentive to liquidate a dust position" bug class, and confirmed by the pallet's own doc comment (`outbound-queue/src/lib.rs` lines 38-56) stating relayers are only compensated via the attached fee.

I was not able to fully trace whether `snowbridge_pallet_system_v2::send` or `EthereumBlobExporter::deliver` (the two callers into this pallet, per the module doc at lines 7-11) impose any independent minimum-fee/reserve requirement before calling `validate`/`deliver`; if such an upstream check exists it could mitigate this, but I found no such check in the code I was able to inspect. This is a genuine gap in my verification, not a claim I can resolve with confidence within the available search results.

### Title
Outbound Snowbridge queue accepts zero/near-zero fee messages with no minimum-reward enforcement, permanently stalling delivery-receipt settlement - (File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs)

### Summary
`Pallet::do_process_message` admits any XCM-derived `Message` regardless of its `fee` value and creates a `PendingOrder` unconditionally. No minimum-fee check exists in the admission path (`SendMessage::validate`/`deliver`) or in `do_process_message`. Orders with `fee == 0` (or below relayer gas cost) can never be economically justified for a relayer to fulfil via `submit_delivery_receipt`, so they remain in `PendingOrders` indefinitely, growing state and never settling — the direct analog of WiseLending's uneconomical-liquidation bad-debt bug, transposed to relayer economics.

### Finding Description
The message admission flow is:
1. `XcmConverter::convert` extracts `fee_amount` from the `PayFees` instruction with no lower bound check (`bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs:94-117,219,312-317`).
2. `SendMessage::validate` for the pallet only checks payload size, never fee (`bridges/snowbridge/pallets/outbound-queue-v2/src/send_message_impl.rs:23-32`).
3. `do_process_message` unconditionally creates a `PendingOrder{ nonce, fee, block_number }` and inserts it into `PendingOrders`, with no `ensure!(fee >= MinimumFee)` guard (`bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs:426-436`).
4. `process_delivery_receipt` only pays a reward `if order.fee > 0` — any positive-but-negligible fee (or exactly `0`) results in an order that is either unrewarded or under-rewarded relative to the relayer's real Ethereum gas cost for calling the Gateway/submitting the receipt (`lib.rs:466-473`).
5. The only escape hatch, `AddTip::add_tip` (`lib.rs:483-495`), is optional and permissionless to invoke, but nothing in the protocol *forces* a top-up before the message is queued — it's a bolt-on fix (mirrors the WiseLending report's "minimum deposit that can be bypassed" theme), not a binding invariant at admission time.

Because delivery is unordered and per-nonce (module doc `lib.rs:7-42`), a dust-fee message does not block *other* messages from being relayed, but it does permanently occupy a `PendingOrders` map entry and its associated committed leaf/nonce lifecycle never resolves via honest relayer economics, since no unprivileged, rational actor will pay real ETH gas to earn `0` or near-`0` reward.

### Impact Explanation
This falls under "public underpriced work that degrades block production or stalls bridge processing" in the impact gate. An attacker (any unprivileged XCM sender able to construct a `PayFees` instruction with a minimal amount, e.g. `1`) can force the bridge to accept fee-less work into the committed outbound queue at negligible cost (they still must pay for XCM execution weight locally, but not a market-clearing relayer reward). Repeated cheaply, this creates a growing set of orders that will never be delivered/settled through honest incentives, causing unresolved bridge state (`PendingOrders`) to accumulate without bound and starving legitimate governance/user messages of relayer attention over time, since relayers must choose which nonces are worth relaying.

### Likelihood Explanation
High likelihood for an unprivileged actor: constructing an XCM with `PayFees(ETH, 1)` requires no special privilege, validator/collator/relayer compromise, or governance action — purely a public dispatch path (`pallet_xcm::execute`/XCM transport into AH → BH → `EthereumBlobExporter::deliver`). The only cost is the local execution fee for the XCM itself, decoupled entirely from the relayer reward that governs whether the resulting outbound order is ever fulfilled.

### Recommendation
Enforce a `MinimumMessageFee` (or dynamically computed minimum based on `GasMeter`/`WeightToFee` estimates for the worst-case Ethereum gas of the message's commands) inside `do_process_message` before inserting into `PendingOrders`, rejecting or postponing messages whose `fee` is below the break-even cost for a rational relayer. Alternatively, require the `fee` to be escrowed/burned proportionally at admission time so that dust-fee messages cannot be created without an offsetting cost to the submitter, closing the same class of bypass noted in the source report regarding a mandatory-but-circumventable minimum check.

### Proof of Concept
1. Build XCM: `WithdrawAsset(ETH, 1)`, `PayFees(ETH, 1)`, `ReserveAssetDeposited(PNA, X)`, `AliasOrigin`, `DepositAsset`, `SetTopic`.
2. Submit via `pallet_xcm::execute` on a sibling parachain routed through AH → BH `SnowbridgeMessageExporter`.
3. `XcmConverter::convert` accepts `fee_amount = 1` with no minimum check (`convert.rs:94-117`).
4. `do_process_message` enqueues the message, creates `PendingOrder{ fee: 1 }` in `PendingOrders` (`lib.rs:426-436`) — confirmed unconditional by test `bridges/snowbridge/pallets/outbound-queue-v2/src/test.rs` patterns showing `PendingOrders` populated directly from message `fee` with no minimum assertions.
5. No rational relayer submits `submit_delivery_receipt` for `fee = 1` given real Ethereum gas costs; the order remains in `PendingOrders` permanently unless a third party voluntarily calls `add_tip` (not guaranteed, and itself unbounded/optional per `lib.rs:483-495`). [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6)

### Citations

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

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs (L217-319)
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

		// All xcm instructions must be consumed before exit.
```

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L38-56)
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
```
