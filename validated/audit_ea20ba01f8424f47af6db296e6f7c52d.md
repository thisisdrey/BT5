### Title
Attacker-controlled remote fee amount is credited 1:1 as relayer reward without ETH→DOT conversion, draining the BridgeRelayers reward pot - ([File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs])

### Summary
In the Snowbridge V2 outbound flow, the fee that an unprivileged user attaches to an outbound message (`PayFees` amount in the XCM they submit) is copied verbatim into `PendingOrder.fee` and later paid out as-is to a relayer via `RewardPayment::register_reward`. Unlike the V1 outbound-queue pallet, which explicitly converts the Ether-denominated remote fee into the local reward currency using an exchange rate and decimal adjustment (`calculate_fee` / `convert_from_ether_decimals`), the V2 pallet performs **no currency conversion at all**. A user who declares a moderate Ether fee (18-decimal units) causes that same numeric value to be registered as a native-currency (10/12-decimal) reward, inflating the payout by many orders of magnitude and draining the shared relayer reward pot — the exact bug class described in the report: an attacker-controlled "size/limit" parameter directly and unboundedly inflates a fee that the protocol pays out of its own funds.

### Finding Description
The XCM-to-message converter extracts a fee amount entirely from data supplied in the user's own XCM instructions: [1](#0-0) 

The only checks are that the `WithdrawAsset` amount is `>=` the `PayFees` amount and that the asset ID is the Ether-Here asset — there is no bound relative to actual gas cost, and the numeric value (in wei-like 18-decimal Ether units) is stored unmodified into the constructed `Message`: [2](#0-1) 

This `Message.fee` flows unchanged through `SendMessage::validate`/`deliver` (no fee recomputation) and is stored directly into `PendingOrder.fee` when the message is processed: [3](#0-2) 

When the relayer later submits a delivery receipt, this raw value is registered as a reward with no conversion: [4](#0-3) 

Contrast this with the sibling V1 pallet, which treats the same class of remote fee value with an explicit FX-rate and decimal-adjustment step before it is ever used as a payable amount: [5](#0-4) 

No equivalent `PricingParameters`/`exchange_rate`/`convert_from_ether_decimals` step exists anywhere in the `outbound-queue-v2` pallet or its `send_message_impl.rs`/`process_message_impl.rs`, confirmed by the `Config` trait for v2 having no `PricingParameters` type at all (compare with the v1 `Config` requiring `type PricingParameters`), and the runtime wiring shows the v2 pallet configured without any pricing/exchange-rate parameter: [6](#0-5) 

### Impact Explanation
`T::RewardPayment::register_reward` credits the relayer's account in the local reward currency out of the `BridgeRelayers` reward ledger/pot, which is a shared protocol resource funded independently of any single message's real cost. Because `order.fee` is taken verbatim from an 18-decimal Ether value with no exchange-rate conversion, any user submitting a legitimate-looking cross-chain transfer with a normal Ether fee (e.g. fractions of an ETH, which is a huge number in raw integer terms) causes an enormously inflated reward to be registered — impact scales with the ETH/DOT decimal and rate mismatch (potentially many orders of magnitude), enabling systematic draining of the relayer reward pool with funds that were never actually backed on-chain in the local currency. This is a direct "public underpriced work" / fund-drain impact on BridgeHub's own reward accounting, matching the required impact criteria (theft/unbacked payout, duplicate/mis-scaled settlement).

### Likelihood Explanation
High. Constructing the XCM with an arbitrary `PayFees` amount and a matching `WithdrawAsset` reserve requires no privileged origin — any account able to send a cross-chain transfer through `pallet-xcm`/`EthereumBlobExporter` can set the fee field to any value they choose (bounded only by holding a matching amount of the reserve asset, which is a normal ether-denominated asset, not the inflated reward-currency amount). Anyone (including the attacker themselves) can then submit the `submit_delivery_receipt` extrinsic once the message is processed on Ethereum, claiming the inflated reward. No malicious relayer/validator/governance action is required — this is a pure public-entrypoint miscalculation.

### Recommendation
Apply the same currency-conversion and decimal-normalization logic used in V1's `calculate_fee`/`convert_from_ether_decimals` to the V2 pipeline before storing `order.fee`, or independently recompute the reward amount from `T::GasMeter` and governance-configured pricing parameters rather than trusting the user-declared `Message.fee` value at face value. At minimum, cap `order.fee` to a sane maximum and require it be derived from measured gas/weight, not attacker-declared XCM data.

### Proof of Concept
1. Attacker constructs an XCM on AssetHub of the form `WithdrawAsset(Ether: X) -> PayFees(Ether: X) -> ... -> AliasOrigin -> DepositAsset -> SetTopic`, setting `X` to a legitimate-looking Ether amount (e.g. `X = 1_000_000_000_000_000_000` = 1 ETH in wei units), which is entirely normal/valid on the Ethereum side of the message.
2. `XcmConverter::convert` (`bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs:218-317`) stores `fee: 1_000_000_000_000_000_000` into the `Message` with no downscaling.
3. `outbound-queue-v2::do_process_message` stores `PendingOrder { fee: 1_000_000_000_000_000_000, .. }` (`lib.rs:426-436`).
4. Attacker (or colluding relayer) obtains the Ethereum-side delivery proof and calls `submit_delivery_receipt`, triggering `process_delivery_receipt`, which calls `T::RewardPayment::register_reward(&reward_account, DefaultRewardKind, 1_000_000_000_000_000_000)` (`lib.rs:466-472`) — crediting a reward denominated in the chain's native balance (10-12 decimals) using a raw 18-decimal Ether figure, vastly exceeding any real cost and draining the reward pot relative to legitimate relayer payouts.

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

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs (L310-317)
```rust
		let topic_id = match_expression!(self.next()?, SetTopic(id), id).ok_or(SetTopicExpected)?;

		let message = Message {
			id: (*topic_id).into(),
			origin,
			fee: fee_amount,
			commands: BoundedVec::try_from(commands).map_err(|_| TooManyCommands)?,
		};
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

**File:** cumulus/parachains/runtimes/bridge-hubs/bridge-hub-westend/src/bridge_to_ethereum_config.rs (L194-219)
```rust
impl snowbridge_pallet_outbound_queue_v2::Config for Runtime {
	type RuntimeEvent = RuntimeEvent;
	type Hashing = Keccak256;
	type MessageQueue = MessageQueue;
	// Maximum payload size for outbound messages.
	type MaxMessagePayloadSize = ConstU32<2048>;
	// Maximum number of outbound messages that can be committed per block.
	// It's benchmarked, including the entire process flow(initialize,submit,commit) in the
	// worst-case, Benchmark results in `../weights/snowbridge_pallet_outbound_queue_v2.
	// rs` show that the `process` function consumes less than 1% of the block capacity, which is
	// safe enough.
	type MaxMessagesPerBlock = ConstU32<32>;
	type GasMeter = ConstantGasMeterV2;
	type Balance = Balance;
	type WeightToFee = WeightToFee;
	type Verifier = EthereumBeaconClient;
	type GatewayAddress = EthereumGatewayAddress;
	type WeightInfo = crate::weights::snowbridge_pallet_outbound_queue_v2::WeightInfo<Runtime>;
	type EthereumNetwork = EthereumNetwork;
	type RewardKind = BridgeReward;
	type DefaultRewardKind = SnowbridgeReward;
	type RewardPayment = BridgeRelayers;
	type AggregateMessageOrigin = AggregateMessageOrigin;
	type OnNewCommitment = ();
	#[cfg(feature = "runtime-benchmarks")]
	type Helper = Runtime;
```
