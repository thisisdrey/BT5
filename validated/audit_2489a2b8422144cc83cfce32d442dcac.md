### Title
Stale-versus-fresh `PricingParameters` snapshot mismatch between message enqueue and commit lets the Snowbridge outbound-queue commit relayer rewards that the sender never actually paid for - ([File: bridges/snowbridge/pallets/outbound-queue/src/lib.rs])

### Summary
`Jackpot.sol`'s bug class is: a payment/price value is read fresh at execution time instead of the value that was actually fixed/charged for the specific operation, causing the charged amount and the committed/settled amount to diverge. The Snowbridge outbound-queue pallet has the same structural flaw: the fee actually charged to the sender at message-submission time (`Pallet::validate`) and the reward/`max_fee_per_gas` actually committed into the message that Ethereum relayers get paid against (`Pallet::do_process_message`) are each computed independently by re-reading the *current* `T::PricingParameters::get()` — but these two reads can happen in different blocks, with governance's `set_pricing_parameters` changing the value in between.

### Finding Description
When a message is sent, `SendMessage::validate` computes the fee charged to the caller using the pricing parameters read at that instant: [1](#0-0) 
This fee (`Fee { local, remote }`) is what gets deducted from the sender via the `SendMessageFeeProvider`/XCM exporter machinery at submission time. The message itself is then merely enqueued with no snapshot of the pricing parameters used to compute that fee — `QueuedMessage`/`Ticket` only carries `id`, `channel_id`, and `command`.

Later, when the `MessageQueue` pallet actually dequeues and processes the message (potentially many blocks after it was enqueued, since queue processing is weight-bounded and asynchronous — see the `Yield` check), `do_process_message` computes the values that are actually baked into the `CommittedMessage` sent to Ethereum — `reward` and `max_fee_per_gas` — by reading `T::PricingParameters::get()` *again*, fresh, at commit time: [2](#0-1) 

These committed values (`reward`, `max_fee_per_gas`) are exactly what the Ethereum Gateway contract pays relayers: [3](#0-2) 

Governance can update `PricingParameters` (`exchange_rate`, `fee_per_gas`, `rewards`, `multiplier`) at any time via `set_pricing_parameters`: [4](#0-3) 

Because nothing freezes the pricing snapshot at enqueue time into the `QueuedMessage`, any change to `PricingParameters` between when a user's message is enqueued (fee charged in `validate`) and when it is later dequeued/committed (`reward`/`max_fee_per_gas` computed in `do_process_message`) produces a mismatch between what was actually collected from the sender and what the protocol commits to pay out on Ethereum:

- If parameters (e.g. `exchange_rate`, `fee_per_gas`, `multiplier`) increase in that window, the committed message promises a higher `reward`/`max_fee_per_gas` than what was collected from the original sender — the bridge is committing to pay relayers more than it actually charged, an unbacked promise against the fee pool.
- If parameters decrease, the sender already paid more than the committed message now promises to relayers — analogous to the original report's "overpayment" case, with the excess having no refund path.

This mirrors the reported bug exactly: a value meant to be fixed at the time of a specific action (drawing/ticket price vs. message fee) is instead re-fetched live at a *different* stage of the same logical operation, with no mechanism to bind the two reads together.

### Impact Explanation
This is a public-entrypoint issue reachable by any user sending a Snowbridge outbound message (no privileged actor needed to trigger the divergence — governance changing `PricingParameters` is a normal, expected operational action, not an attack). The impact is fund-accounting divergence between what the chain collects from users and what it commits to disburse to relayers on the Ethereum side, i.e. duplicate/unbacked settlement of relayer rewards or unrecoverable user overpayment, directly matching "theft or unbacked mint/unlock" and "message queues... must only advance after decode, dispatch, execution, and settlement succeed atomically" from the impact gate. Given delayed/asynchronous message processing (`MaxMessagesPerBlock` throttling with `Yield`), the window in which parameters can change before commit is not bounded to a single block, so the divergence is not a mere rounding edge case.

### Likelihood Explanation
`set_pricing_parameters` is a normal governance operation expected to run "every few weeks" per the module docs: [5](#0-4) 
Any outbound message enqueued shortly before such an update and processed after it (or vice versa) will hit this mismatch — this requires no adversarial coordination, malicious relayer, or privileged abuse; it is a routine timing race inherent to the design.

### Recommendation
Snapshot the `PricingParameters` (or at least the derived `Fee`, `reward`, and `max_fee_per_gas`) used in `validate`/`calculate_fee` into the `QueuedMessage`/`CommittedMessage` payload at enqueue time, and use that stored snapshot in `do_process_message` instead of re-reading `T::PricingParameters::get()` at commit time. This ensures the amount charged to the sender and the amount committed for relayer reward always originate from the same parameter set.

### Proof of Concept
1. User calls an operation that ultimately invokes `Pallet::validate` (e.g. via XCM `ExportMessage`) while `PricingParameters` = `P1`. The sender is charged `Fee::calculate_fee(gas, P1)`. [1](#0-0) 
2. The message is enqueued into `T::MessageQueue` but not yet processed this block (e.g., `MaxMessagesPerBlock` reached, triggering `Yield`). [6](#0-5) 
3. Before the message is dequeued, governance calls `set_pricing_parameters` to update `PricingParameters` to `P2` (e.g., raising `exchange_rate`/`multiplier`/`fee_per_gas`). [7](#0-6) 
4. `do_process_message` runs in a later block and computes `reward`/`max_fee_per_gas` for the `CommittedMessage` using the now-current `P2`, not `P1`: [8](#0-7) 
5. The relayer, upon delivering to Ethereum, is entitled to `reward` and gas refund up to `max_fee_per_gas` computed from `P2`, while the fee actually deducted from the original sender was computed from `P1` — the two amounts diverge, either under-collecting versus the committed payout or over-charging the sender with no refund.

Existing test coverage confirms `calculate_fee` is a pure function of whatever `PricingParameters` snapshot is passed in at call time, with no binding to the parameters used for the original fee charge: [9](#0-8)

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue/src/send_message_impl.rs (L59-60)
```rust
		let gas_used_at_most = T::GasMeter::maximum_gas_used_at_most(&message.command);
		let fee = Self::calculate_fee(gas_used_at_most, T::PricingParameters::get());
```

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L49-55)
```rust
//! The fee calculation also requires the following parameters:
//! * Average ETH/DOT exchange rate over some period
//! * Max fee per unit of gas that bridge is willing to refund relayers for
//!
//! By design, it is expected that governance should manually update these
//! parameters every few weeks using the `set_pricing_parameters` extrinsic in the
//! system pallet.
```

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L72-80)
```rust
//! ## Fee Settlement
//!
//! On the remote side, in the gateway contract, the relayer accrues
//!
//! ```text
//! Min(GasPrice, Message.MaxFeePerGas) * GasUsed() + Message.Reward
//! ```
//! Or in plain english, relayers are refunded for gas consumption, using a
//! price that is a minimum of the actual gas price, or `Message.MaxFeePerGas`.
```

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L300-352)
```rust
		/// Process a message delivered by the MessageQueue pallet
		pub(crate) fn do_process_message(
			_: ProcessMessageOriginOf<T>,
			mut message: &[u8],
		) -> Result<bool, ProcessMessageError> {
			use ProcessMessageError::*;

			// Yield if the maximum number of messages has been processed this block.
			// This ensures that the weight of `on_finalize` has a known maximum bound.
			ensure!(
				MessageLeaves::<T>::decode_len().unwrap_or(0) <
					T::MaxMessagesPerBlock::get() as usize,
				Yield
			);

			// Decode bytes into versioned message
			let versioned_queued_message: VersionedQueuedMessage =
				VersionedQueuedMessage::decode(&mut message).map_err(|_| Corrupt)?;

			// Convert versioned message into latest supported message version
			let queued_message: QueuedMessage =
				versioned_queued_message.try_into().map_err(|_| Unsupported)?;

			// Obtain next nonce
			let nonce = <Nonce<T>>::try_mutate(
				queued_message.channel_id,
				|nonce| -> Result<u64, ProcessMessageError> {
					*nonce = nonce.checked_add(1).ok_or(Unsupported)?;
					Ok(*nonce)
				},
			)?;

			let pricing_params = T::PricingParameters::get();
			let command = queued_message.command.index();
			let params = queued_message.command.abi_encode();
			let max_dispatch_gas =
				T::GasMeter::maximum_dispatch_gas_used_at_most(&queued_message.command);
			let reward = pricing_params.rewards.remote;

			// Construct the final committed message
			let message = CommittedMessage {
				channel_id: queued_message.channel_id,
				nonce,
				command,
				params,
				max_dispatch_gas,
				max_fee_per_gas: pricing_params
					.fee_per_gas
					.try_into()
					.defensive_unwrap_or(u128::MAX),
				reward: reward.try_into().defensive_unwrap_or(u128::MAX),
				id: queued_message.id,
			};
```

**File:** bridges/snowbridge/pallets/system/src/lib.rs (L316-333)
```rust
		#[pallet::weight((T::WeightInfo::set_pricing_parameters(), DispatchClass::Operational))]
		pub fn set_pricing_parameters(
			origin: OriginFor<T>,
			params: PricingParametersOf<T>,
		) -> DispatchResult {
			ensure_root(origin)?;
			params.validate().map_err(|_| Error::<T>::InvalidPricingParameters)?;
			PricingParameters::<T>::put(params.clone());

			let command = Command::SetPricingParameters {
				exchange_rate: params.exchange_rate.into(),
				delivery_cost: T::InboundDeliveryCost::get().saturated_into::<u128>(),
				multiplier: params.multiplier.into(),
			};
			Self::send(PRIMARY_GOVERNANCE_CHANNEL, command, PaysFee::<T>::No)?;

			Self::deposit_event(Event::PricingParametersChanged { params });
			Ok(())
```

**File:** bridges/snowbridge/pallets/outbound-queue/src/test.rs (L271-301)
```rust
#[test]
fn test_calculate_fees_with_unit_multiplier() {
	new_tester().execute_with(|| {
		let gas_used: u64 = 250000;
		let price_params: PricingParameters<<Test as Config>::Balance> = PricingParameters {
			exchange_rate: FixedU128::from_rational(1, 400),
			fee_per_gas: 10000_u32.into(),
			rewards: Rewards { local: 1_u32.into(), remote: 1_u32.into() },
			multiplier: FixedU128::from_rational(1, 1),
		};
		let fee = OutboundQueue::calculate_fee(gas_used, price_params);
		assert_eq!(fee.local, 698000000);
		assert_eq!(fee.remote, 1000000);
	});
}

#[test]
fn test_calculate_fees_with_multiplier() {
	new_tester().execute_with(|| {
		let gas_used: u64 = 250000;
		let price_params: PricingParameters<<Test as Config>::Balance> = PricingParameters {
			exchange_rate: FixedU128::from_rational(1, 400),
			fee_per_gas: 10000_u32.into(),
			rewards: Rewards { local: 1_u32.into(), remote: 1_u32.into() },
			multiplier: FixedU128::from_rational(4, 3),
		};
		let fee = OutboundQueue::calculate_fee(gas_used, price_params);
		assert_eq!(fee.local, 698000000);
		assert_eq!(fee.remote, 1333333);
	});
}
```
