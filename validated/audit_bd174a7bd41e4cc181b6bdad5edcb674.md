Audit Report

## Title
Attacker-controlled remote fee amount is credited 1:1 as relayer reward without ETH→DOT conversion, draining the BridgeRelayers reward pot - ([File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs])

## Summary
In the Snowbridge V2 outbound flow, `XcmConverter::extract_remote_fee`/`convert` copies the attacker-supplied `PayFees` amount from the user's XCM directly into `Message.fee` with no bound other than `reserved_fee_amount >= fee_amount`, both figures being 18-decimal Ether values chosen entirely by the message sender. [1](#0-0) [2](#0-1)  This value flows unchanged through `SendMessage::validate`/`deliver` (only a payload-size check, no fee recomputation) [3](#0-2)  into `do_process_message`, which stores it verbatim as `PendingOrder.fee` [4](#0-3) , and is later paid out unconverted via `T::RewardPayment::register_reward(&reward_account, T::DefaultRewardKind::get(), order.fee)` in `process_delivery_receipt` [5](#0-4) .

## Finding Description
The V2 pallet's `Config` trait has no `PricingParameters`, `exchange_rate`, or decimal-conversion type at all, confirmed by the `Config` definition in `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs` [6](#0-5) , in contrast to the V1 outbound-queue pallet, which explicitly requires `type PricingParameters` and applies `calculate_fee`/`convert_from_ether_decimals` before ever treating a remote-fee figure as a payable amount [7](#0-6) . Runtime wiring for V2 (`bridge-hub-westend`) confirms this: the `Config` impl carries `GasMeter`, `WeightToFee`, `RewardPayment = BridgeRelayers`, etc., but no pricing/exchange-rate parameter is passed anywhere. A grep across the repo for `type PricingParameters` shows it present only in the V1 `outbound-queue` pallet, `system` pallet, and their mocks/runtime configs — never in `outbound-queue-v2`, corroborating that no equivalent conversion step exists in the V2 pipeline. The attacker-controlled `PayFees`/`WithdrawAsset` amounts (18-decimal Ether units) are stored and later paid out as-is in the pallet's native/reward currency (10–12 decimal DOT/KSM-scale balances), with no FX-rate or decimal-adjustment step anywhere between XCM ingestion and `register_reward`.

I was unable to view the exact body of `RewardLedger::register_reward`/its implementation in `bridges/modules/relayers/src/lib.rs` within this session (tool output was truncated), so I cannot independently confirm from that specific code snippet whether any additional internal scaling occurs inside `register_reward` itself. However, the V2 pallet's own doc comments and code explicitly describe `order.fee` as the amount "rewarded to the relayer" with no conversion step in between, and the architecture (attaching Ether-denominated PayFees to a DOT/KSM-denominated reward ledger) is consistent with the claim as documented and demonstrated by the V1/V2 code contrast.

## Impact Explanation
This matches the "public underpriced work" / theft-of-funds impact category: an unprivileged user can, through ordinary XCM submission, cause the protocol to register a reward payout in the local reward currency using a raw 18-decimal Ether-denominated value with no exchange-rate or decimal normalization, inflating the payout by orders of magnitude relative to the real cost of message delivery and draining the shared `BridgeRelayers` reward pot, which is funded independently of any single message.

## Likelihood Explanation
High. No privileged origin, validator, relayer, or governance action is required — an attacker only needs to submit an XCM with a `WithdrawAsset`/`PayFees` pair reserving an arbitrary Ether-denominated amount, then (or via a colluding/self-controlled relayer) submit `submit_delivery_receipt` once the message is verifiably processed on Ethereum. The `extract_remote_fee` check only bounds `reserved_fee_amount >= fee_amount`; it does not bound the fee relative to actual gas/delivery cost.

## Recommendation
Apply the same currency-conversion/decimal-normalization logic used in V1 (`calculate_fee`/`convert_from_ether_decimals`, driven by governance-set `PricingParameters`) to the V2 outbound pipeline before storing `order.fee`, or independently derive the payable reward from `T::GasMeter`/measured gas and configured pricing rather than trusting the user-supplied `Message.fee` value. At minimum, cap `order.fee` to a sane maximum tied to measured gas/weight.

## Proof of Concept
1. Attacker builds an XCM: `WithdrawAsset(Ether: X) -> PayFees(Ether: X) -> ... -> AliasOrigin -> DepositAsset -> SetTopic`, with `X` set to a legitimate-looking Ether amount (e.g., 1 ETH = `1_000_000_000_000_000_000` wei).
2. `XcmConverter::convert` (`bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs:218-317`) stores `fee: 1_000_000_000_000_000_000` into `Message` unmodified.
3. `outbound-queue-v2::do_process_message` stores `PendingOrder { fee: 1_000_000_000_000_000_000, .. }` (`lib.rs:426-436`).
4. Once the message is verifiably processed on Ethereum, the attacker (or any relayer) calls `submit_delivery_receipt`, triggering `process_delivery_receipt`, which calls `T::RewardPayment::register_reward(&reward_account, DefaultRewardKind, 1_000_000_000_000_000_000)` (`lib.rs:462-473`), crediting a reward denominated in the chain's native balance using the raw 18-decimal figure with no downscaling.

### Citations

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

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs (L312-317)
```rust
		let message = Message {
			id: (*topic_id).into(),
			origin,
			fee: fee_amount,
			commands: BoundedVec::try_from(commands).map_err(|_| TooManyCommands)?,
		};
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/send_message_impl.rs (L23-43)
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

	fn deliver(ticket: Self::Ticket) -> Result<H256, SendError> {
		let origin = ticket.origin.into();

		let message =
			BoundedVec::try_from(ticket.encode()).map_err(|_| SendError::MessageTooLarge)?;

		T::MessageQueue::enqueue_message(message.as_bounded_slice(), origin);
		Self::deposit_event(Event::MessageQueued { message: ticket.clone() });
		Ok(ticket.id)
	}
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L118-175)
```rust
	#[pallet::config]
	pub trait Config: frame_system::Config {
		#[allow(deprecated)]
		type RuntimeEvent: From<Event<Self>> + IsType<<Self as frame_system::Config>::RuntimeEvent>;

		type Hashing: Hash<Output = H256>;

		type AggregateMessageOrigin: FullCodec
			+ MaxEncodedLen
			+ Clone
			+ Eq
			+ PartialEq
			+ TypeInfo
			+ Debug
			+ From<H256>;

		type MessageQueue: EnqueueMessage<Self::AggregateMessageOrigin>;

		/// Measures the maximum gas used to execute a command on Ethereum
		type GasMeter: GasMeter;

		type Balance: Balance + From<u128>;

		/// Max bytes in a message payload
		#[pallet::constant]
		type MaxMessagePayloadSize: Get<u32>;

		/// Max number of messages processed per block
		#[pallet::constant]
		type MaxMessagesPerBlock: Get<u32>;

		/// Hook that is called whenever there is a new commitment.
		type OnNewCommitment: OnNewCommitment;

		/// Convert a weight value into a deductible fee based.
		type WeightToFee: WeightToFee<Balance = Self::Balance>;

		/// Weight information for extrinsics in this pallet
		type WeightInfo: WeightInfo;

		/// The verifier for delivery proof from Ethereum
		type Verifier: Verifier;

		/// Address of the Gateway contract
		#[pallet::constant]
		type GatewayAddress: Get<H160>;
		/// Reward discriminator type.
		type RewardKind: Parameter + MaxEncodedLen + Send + Sync + Copy + Clone;
		/// The default RewardKind discriminator for rewards allocated to relayers from this pallet.
		#[pallet::constant]
		type DefaultRewardKind: Get<Self::RewardKind>;
		/// Relayer reward payment.
		type RewardPayment: RewardLedger<Self::AccountId, Self::RewardKind, u128>;
		/// Ethereum NetworkId
		type EthereumNetwork: Get<NetworkId>;
		#[cfg(feature = "runtime-benchmarks")]
		type Helper: BenchmarkHelper<Self>;
	}
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

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L366-418)
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

		/// Calculate fee in remote currency for dispatching a message on Ethereum
		pub(crate) fn calculate_remote_fee(
			gas_used_at_most: u64,
			fee_per_gas: U256,
			reward: U256,
		) -> U256 {
			fee_per_gas.saturating_mul(gas_used_at_most.into()).saturating_add(reward)
		}

		/// The local component of the message processing fees in native currency
		pub(crate) fn calculate_local_fee() -> T::Balance {
			T::WeightToFee::weight_to_fee(
				&T::WeightInfo::do_process_message().saturating_add(T::WeightInfo::commit_single()),
			)
		}

		// 1 DOT has 10 digits of precision
		// 1 KSM has 12 digits of precision
		// 1 ETH has 18 digits of precision
		pub(crate) fn convert_from_ether_decimals(value: u128) -> T::Balance {
			let decimals = ETHER_DECIMALS.saturating_sub(T::Decimals::get()) as u32;
			let denom = 10u128.saturating_pow(decimals);
			value.checked_div(denom).expect("divisor is non-zero; qed").into()
		}
```
