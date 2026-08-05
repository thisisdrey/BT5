Based on my research, I found a concrete local analog in the Snowbridge V2 outbound-queue fee/reward accounting path, which mirrors the external report's core flaw: a fee/gas value supplied inside a user-controlled message is trusted and later paid out, without the pallet verifying that it is actually backed by value genuinely removed from the sender.

### Title
Snowbridge V2 outbound message fee is self-attested and drives unbacked relayer reward minting - (File: `bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs`, `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
`snowbridge-pallet-outbound-queue-v2` stores a user-supplied `fee` value inside every `PendingOrder` [1](#0-0) , taken verbatim from the `Message.fee` field produced by `XcmConverter::convert` [2](#0-1) . That `fee` becomes the reward paid to whichever relayer submits a delivery receipt [3](#0-2) , via `T::RewardPayment::register_reward`, which for the Snowbridge reward kind mints a fresh `ReserveAssetDeposited` of that exact amount to the beneficiary on Asset Hub [4](#0-3) . This is architecturally the same defect as the audited bug: a "gas/fee" parameter chosen by the message author is not tied to funds actually deducted from the author, yet it directly determines value paid out of the system later.

### Finding Description
`extract_remote_fee` in the XCM→Ethereum message converter only checks internal consistency between two attacker-authored fields of the same exported XCM program — the amount in `WithdrawAsset(fee)` and the amount in `PayFees{asset: fee}` — requiring `reserved_fee_amount >= fee_amount` [5](#0-4) . It does not (and, at the point this parser runs — during `ExportXcm::validate` pattern-matching of the *outgoing* XCM instruction list — cannot easily) verify that this quantity corresponds to Ether that was genuinely locked/reserved for Snowbridge's benefit as part of the same transfer. The resulting `fee_amount` is placed unchanged into `Message.fee` [6](#0-5) , and the exporter's `validate()` returns `Assets::default()` as the XCM delivery price for this leg [7](#0-6) , so the standard XCM delivery-fee-charging mechanism does not independently withdraw or lock this amount either.

When the message is processed, `do_process_message` stores `fee` verbatim into `PendingOrders` keyed by nonce [8](#0-7) . Later, `process_delivery_receipt` reads `order.fee` and calls `T::RewardPayment::register_reward(&reward_account, T::DefaultRewardKind::get(), order.fee)` [3](#0-2) . On Bridge Hub, that reward is later claimed and paid out through `PayAccountOnLocation::pay_reward`, which constructs a synthetic `ReserveAssetDeposited` XCM crediting the beneficiary with `reward` units of the Ethereum-native asset, exactly as if that Ether had genuinely arrived from the Ethereum reserve [9](#0-8) . Nowhere in this chain is the `fee` amount cross-checked against value that was actually removed from the user's balance and placed under bridge/reserve custody for this specific nonce; the "gas/fee" parameter behaves exactly like `forward_milliton`/`gas_transfer` in the original report — trusted, not deducted, and directly consumed by a payout routine.

### Impact Explanation
If the fee value inserted into a message is not provably backed by assets actually removed from the sender for that transfer, an attacker who can get an `ExportMessage` accepted by this exporter with an inflated `fee_amount` field causes the pallet to register (and eventually mint, via `ReserveAssetDeposited`) a relayer reward that is not backed by any genuine Ether locked on the Ethereum side. This is an unbacked-mint / value-conservation violation on the bridge's asset accounting — the exact impact class called out in the required-impacts list ("theft or unbacked mint or unlock"), reachable without a malicious relayer, validator, or governance actor: only an ordinary Asset Hub user constructing a suitably shaped XCM program is required.

### Likelihood Explanation
Medium-to-low confidence without deeper tracing: I could not fully confirm, within this investigation, whether an earlier stage of the pipeline (e.g., `pallet_xcm::execute`/`send` origin filters, the `InitiateTransfer`/`ExportMessage` handling in the XCM executor, or `snowbridge_pallet_system_v2` configuration) independently forces the `WithdrawAsset`/`PayFees` amounts embedded in the exported program to match real value removed from the sender's holding register before `EthereumBlobExporter::validate` ever runs. If such an upstream binding exists and is airtight, the local converter's lack of an explicit check is defense-in-depth rather than exploitable on its own. This uncertainty should be resolved by a Devin session with full build/test access, tracing the exact runtime call path from `pallet_xcm` dispatch through `xcm-executor`'s `ExportMessage` instruction execution into `EthereumBlobExporter::validate`, to determine whether the `fee_amount` figure is ever forced to equal assets truly withdrawn and retained under bridge control at the time of message commit.

### Recommendation
- In `extract_remote_fee` (or in `do_process_message` before inserting `PendingOrder`), assert that the `fee_amount` recorded for a nonce is provably backed by assets that were actually withdrawn from the sender's account for this transfer and placed under Snowbridge/BridgeHub sovereign control (e.g., cross-check against the local `WithdrawAsset`/reserve accounting performed earlier in the same XCM program, not merely against another self-declared field in the same message).
- Consider having the exporter's `validate()` return a non-empty `Assets` price equal to `fee_amount`, so the XCM executor's normal fee-charging/delivery-fee machinery enforces the withdrawal, rather than relying purely on parsed literal values.
- Add integration tests (per the long-term recommendation in the original report) asserting that the sum of Ether-denominated reward/fee amounts registered in `PendingOrders` and later minted via `PayAccountOnLocation` never exceeds the value actually removed from senders across a batch of `ExportMessage` calls.

### Proof of Concept
Conceptual PoC (requires confirming the missing upstream binding described above):
1. Construct an `Xcm` program that satisfies `XcmConverter::convert`'s pattern: `WithdrawAsset(fee_asset_high)`, `PayFees{asset: fee_asset_high}` (both with an attacker-chosen large `amount`), followed by a valid `ReserveAssetDeposited`/`WithdrawAsset` for a token transfer, `AliasOrigin`, `DepositAsset`, `SetTopic`.
2. Submit this program from Asset Hub via `pallet_xcm::execute`/`send` so that it is routed through `EthereumBlobExporter` (V2) to Bridge Hub's `EthereumOutboundQueueV2`.
3. If the earlier XCM execution stages do not force the declared `WithdrawAsset`/`PayFees` amount to equal value genuinely removed from the sender's balance, the resulting `PendingOrder.fee` on Bridge Hub will equal the attacker-chosen inflated amount.
4. Have any relayer (or the attacker's own account, as `reward_address`) submit `submit_delivery_receipt` after the message is delivered on Ethereum; `process_delivery_receipt` registers the inflated `order.fee` as a reward [10](#0-9) .
5. Claim the reward via `pallet_bridge_relayers::claim_rewards_to`, causing `PayAccountOnLocation` to mint the inflated amount of Ether-denominated asset to the beneficiary on Asset Hub [9](#0-8) , unbacked by any real Ether locked on the Ethereum side.

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/types.rs (L14-24)
```rust
/// Pending order
#[derive(Encode, Decode, TypeInfo, Clone, Eq, PartialEq, Debug, MaxEncodedLen)]
pub struct PendingOrder<BlockNumber> {
	/// The nonce used to identify the message
	pub nonce: u64,
	/// The block number in which the message was committed
	pub block_number: BlockNumber,
	/// The fee in Ether provided by the user to incentivize message delivery
	#[codec(compact)]
	pub fee: u128,
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

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs (L217-219)
```rust
	pub fn convert(&mut self) -> Result<Message, XcmConverterError> {
		// Get fee amount
		let fee_amount = self.extract_remote_fee()?;
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

**File:** bridges/snowbridge/primitives/core/src/reward.rs (L127-151)
```rust
	fn pay_reward(
		relayer: &Relayer,
		_: (),
		reward: RewardBalance,
		beneficiary: Self::Beneficiary,
	) -> Result<(), Self::Error> {
		let ethereum_location = Location::new(2, [GlobalConsensus(EthereumNetwork::get())]);
		let assets: Asset = (ethereum_location.clone(), reward.into()).into();

		let xcm: Xcm<()> = alloc::vec![
			UnpaidExecution { weight_limit: Unlimited, check_origin: None },
			DescendOrigin(InboundQueueLocation::get().into()),
			UniversalOrigin(GlobalConsensus(EthereumNetwork::get())),
			ReserveAssetDeposited(assets.into()),
			DepositAsset { assets: AllCounted(1).into(), beneficiary },
		]
		.into();

		let (ticket, fee) =
			validate_send::<XcmSender>(AssetHubLocation::get(), xcm).map_err(|_| XcmSendFailure)?;
		XcmExecutor::charge_fees(relayer.clone(), fee).map_err(|_| ChargeFeesFailure)?;
		XcmSender::deliver(ticket).map_err(|_| XcmSendFailure)?;

		Ok(())
	}
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/converter/mod.rs (L156-163)
```rust
		// validate the message
		let ticket = OutboundQueue::validate(&message).map_err(|err| {
			tracing::error!(target: TARGET, error=?err, "OutboundQueue validation of message failed.");
			SendError::Unroutable
		})?;

		Ok(((ticket.encode(), XcmHash::from(message.id)), Assets::default()))
	}
```
