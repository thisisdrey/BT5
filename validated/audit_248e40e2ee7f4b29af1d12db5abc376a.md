Audit Report

## Title
Hardcoded `AllCounted(2)` in Snowbridge `CreateAsset` XCM construction can trap bridged assets instead of delivering them to the claimer - (File: `bridges/snowbridge/primitives/inbound-queue/src/v2/converter.rs`)

## Summary
`MessageToXcm::make_create_asset_xcm_for_polkadot` ends its create-asset XCM with `DepositAsset { assets: Wild(AllCounted(2)), beneficiary: claimer }`, a fixed count that assumes at most two distinct assets remain in holding when this instruction executes. `Message.assets` (`Vec<EthereumAsset>`) is decoded and populated independently of `Payload` in `Message::try_from` (`bridges/snowbridge/primitives/inbound-queue/src/v2/message.rs:144-179`), and nothing in the converter or the pallet code I was able to inspect enforces that `assets` must be empty when `payload` is `Payload::CreateAsset`.

## Finding Description
`MessageToXcm::prepare` (`converter.rs:159-200`) unconditionally loops over `message.assets` and pushes a `ReserveDeposit`/`ReserveWithdraw` entry for every `EthereumAsset`, regardless of which `Payload` variant is active. [1](#0-0) 

`ConvertMessage::convert` (`converter.rs:396-423`) then batches those extra assets into `ReserveAssetDeposited`/`WithdrawAsset` instructions placed into holding *before* `message.remote_xcm` (the create-asset flow) is appended and executed. [2](#0-1) 

`make_create_asset_xcm_for_polkadot` consumes only the specific `eth_asset`/`dot_fee_asset` pair via `ExchangeAsset`/`DepositAsset`/`Transact`, then finishes with `RefundSurplus` and `DepositAsset { assets: Wild(AllCounted(2)), beneficiary: claimer }`. [3](#0-2) 

Because the `message.assets` loop in `prepare()` is not gated on the `Payload` variant, and the `Message`/`Payload` decode path (`message.rs:83-120`, `144-215`) never enforces that `assets` is empty when `payload` is `Payload::CreateAsset`, an Ethereum-side message emitter can combine `Payload::CreateAsset` with a non-empty `assets` vector containing multiple distinct `NativeTokenERC20`/`ForeignTokenERC20` entries. [4](#0-3) [5](#0-4) 

Those extra assets are deposited into holding by the `ReserveAssetDeposited`/`WithdrawAsset` instructions in `convert()`, are never touched by the create-asset-specific `ExchangeAsset`/`Transact`/`DepositAsset(dot_fee_asset)` instructions (which only reference the single `eth_asset`/`dot_fee_asset` pair), and thus remain in holding when the final `DepositAsset { assets: Wild(AllCounted(2)) }` executes. If there are more than two distinct fungible assets present at that point (e.g., two or more extra `EthereumAsset` entries beyond whatever the create-asset flow itself leaves behind), `AllCounted(2)` only picks up the first two counted assets; any additional distinct asset is left in holding and becomes trapped (`AssetsTrapped`) rather than reaching the claimer.

## Impact Explanation
This matches the "public underpriced/incomplete processing causing improper settlement" class for message/queue/payout state: bridged value that should be atomically delivered to the message's claimer is instead left in the XCM holding register as a trapped asset, recoverable only via a separate, manual `pallet_xcm::claim_assets` call rather than automatic settlement. It is a fund-availability defect rather than an outright theft — value is not stolen, but normal one-shot settlement silently fails for a subset of legitimately transferred value, breaking the "settle exactly once to the rightful beneficiary" invariant for bridged assets.

## Likelihood Explanation
I confirmed that `Message`/`Payload` decoding (`message.rs`) does not enforce any relationship between `payload` being `Payload::CreateAsset` and `assets` being empty, and that the `converter.rs` code processes `message.assets` unconditionally regardless of payload variant. I was not able to fully inspect `bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs` (my last grep for `CreateAsset|fn process_message|validate` in that file returned only a single non-informative match, and I ran out of tool budget before reading the file in full) to conclusively determine whether the pallet's message validation/dispatch layer imposes such a constraint before calling into the converter. Given that `Message.assets` is populated straight from Solidity-ABI-decoded Ethereum event data under attacker (Ethereum-side message emitter) control, and the converter itself performs no gating, the trigger condition looks plausibly reachable, but I cannot rule out that pallet-level validation blocks this combination without reading that file's full logic.

## Recommendation
Replace the hardcoded `Wild(AllCounted(2))` in `make_create_asset_xcm_for_polkadot` with either `Wild(All)` (unbounded, since this is the terminal instruction of the message) or a count computed dynamically from the number of distinct assets actually expected in holding at that point (base create-asset leftovers plus one entry per additional asset in `message.assets`), and explicitly validate/reject (or route through a distinct, correctly-sized instruction sequence) any message that combines `Payload::CreateAsset` with a non-empty `assets` vector, if such combinations are not intended to be supported.

## Proof of Concept
1. Construct a `Message` with `payload: Payload::CreateAsset { token, network: Network::Polkadot }` and `assets: vec![EthereumAsset::NativeTokenERC20 { token_id: A, value: v1 }, EthereumAsset::ForeignTokenERC20 { token_id: B, value: v2 }]` (or more entries).
2. Call `MessageToXcm::convert(message)` (as exercised by the existing test harness in `converter.rs`'s `#[cfg(test)] mod tests`, e.g. following the pattern of `test_successful_message`) and inspect the resulting XCM's final `DepositAsset` instruction and the state of holding immediately prior to it.
3. Observe that with ≥3 distinct fungible assets present in holding (create-asset leftovers plus the extra bridged tokens), `Wild(AllCounted(2))` selects only two of them for deposit to `claimer`; add an execution-based test (via `xcm-executor`) to confirm the remaining asset triggers an `AssetsTrapped` event instead of being delivered.
4. Confirm whether `bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs` permits submitting such a combined `Payload::CreateAsset` + non-empty-`assets` message end-to-end (this step was not completed due to tool-budget exhaustion and should be verified before treating this as fully reachable in production).

### Citations

**File:** bridges/snowbridge/primitives/inbound-queue/src/v2/converter.rs (L159-200)
```rust
		let mut assets = vec![];

		if message.value > 0 {
			// Asset for remaining ether
			let remaining_ether_asset: Asset = (ether_location.clone(), message.value).into();
			assets.push(AssetTransfer::ReserveDeposit(remaining_ether_asset));
		}

		for asset in &message.assets {
			match asset {
				EthereumAsset::NativeTokenERC20 { token_id, value } => {
					ensure!(*token_id != H160::zero(), ConvertMessageError::InvalidAsset);
					let token_location: Location = Location::new(
						2,
						[
							GlobalConsensus(EthereumNetwork::get()),
							AccountKey20 { network: None, key: (*token_id).into() },
						],
					);
					let asset: Asset = (token_location, *value).into();
					assets.push(AssetTransfer::ReserveDeposit(asset));
				},
				EthereumAsset::ForeignTokenERC20 { token_id, value } => {
					let asset_location = ConvertAssetId::maybe_convert(*token_id)
						.ok_or(ConvertMessageError::InvalidAsset)?;
					let asset_hub_from_ethereum: Location = Location::new(
						1,
						[
							GlobalConsensus(LocalNetwork::get()),
							Parachain(AssetHubParaId::get().into()),
						],
					);
					let ethereum_universal: InteriorLocation =
						[GlobalConsensus(EthereumNetwork::get())].into();
					let reanchored_asset_location = asset_location
						.reanchored(&asset_hub_from_ethereum, &ethereum_universal)
						.map_err(|_| ConvertMessageError::CannotReanchor)?;
					let asset: Asset = (reanchored_asset_location, *value).into();
					assets.push(AssetTransfer::ReserveWithdraw(asset));
				},
			}
		}
```

**File:** bridges/snowbridge/primitives/inbound-queue/src/v2/converter.rs (L288-325)
```rust
		vec![
			// Exchange eth for dot to pay the asset creation deposit.
			ExchangeAsset {
				give: eth_asset.into(),
				want: dot_fee_asset.clone().into(),
				maximal: false,
			},
			// Deposit the dot deposit into the bridge sovereign account (where the asset
			// creation fee will be deducted from).
			DepositAsset {
				assets: dot_fee_asset.clone().into(),
				beneficiary: bridge_owner_bytes.into(),
			},
			// Call to create the asset.
			Transact {
				origin_kind: OriginKind::Xcm,
				fallback_max_weight: None,
				call: (
					create_call_index,
					asset_id.clone(),
					MultiAddress::<[u8; 32], ()>::Id(bridge_owner_bytes.into()),
					create_min_blance,
				)
					.encode()
					.into(),
			},
			// Call to set Ethereum as the asset's reserve.
			Transact {
				origin_kind: OriginKind::Xcm,
				fallback_max_weight: None,
				call: (set_reserves_call_index, asset_id, vec![reserve_data]).encode().into(),
			},
			RefundSurplus,
			// Deposit leftover funds to Snowbridge sovereign
			DepositAsset { assets: Wild(AllCounted(2)), beneficiary: claimer },
		]
		.into()
	}
```

**File:** bridges/snowbridge/primitives/inbound-queue/src/v2/converter.rs (L396-423)
```rust
		let mut reserve_deposit_assets = vec![];
		let mut reserve_withdraw_assets = vec![];

		for asset in message.assets {
			match asset {
				AssetTransfer::ReserveDeposit(asset) => reserve_deposit_assets.push(asset),
				AssetTransfer::ReserveWithdraw(asset) => reserve_withdraw_assets.push(asset),
			};
		}

		if !reserve_deposit_assets.is_empty() {
			instructions.push(ReserveAssetDeposited(reserve_deposit_assets.into()));
		}
		if !reserve_withdraw_assets.is_empty() {
			instructions.push(WithdrawAsset(reserve_withdraw_assets.into()));
		}

		// If the message origin is not the gateway proxy contract, set the origin to
		// the original sender on Ethereum. Important to be before the arbitrary XCM that is
		// appended to the message on the next line.
		if message.origin != GatewayProxyAddress::get() {
			instructions.push(DescendOrigin(
				AccountKey20 { key: message.origin.into(), network: None }.into(),
			));
		}

		// Add the XCM sent in the message to the end of the xcm instruction
		instructions.extend(message.remote_xcm.0);
```

**File:** bridges/snowbridge/primitives/inbound-queue/src/v2/message.rs (L83-120)
```rust
#[derive(Clone, Encode, Decode, Debug, TypeInfo)]
pub enum Payload {
	/// Raw bytes payload. Commonly used to represent raw XCM bytes
	Raw(Vec<u8>),
	/// A token registration template
	CreateAsset { token: H160, network: Network },
}

/// Network enum for cross-chain message destination
#[derive(Clone, Copy, Debug, Eq, PartialEq, Encode, Decode, TypeInfo)]
pub enum Network {
	/// Polkadot network
	Polkadot,
}

/// The ethereum side sends messages which are transcoded into XCM on BH. These messages are
/// self-contained, in that they can be transcoded using only information in the message.
#[derive(Clone, Encode, Decode, Debug, TypeInfo)]
pub struct Message {
	/// The address of the outbound queue on Ethereum that emitted this message as an event log
	pub gateway: H160,
	/// A nonce for enforcing replay protection and ordering.
	pub nonce: u64,
	/// The address on Ethereum that initiated the message.
	pub origin: H160,
	/// The assets sent from Ethereum (ERC-20s).
	pub assets: Vec<EthereumAsset>,
	/// The command originating from the Gateway contract.
	pub payload: Payload,
	/// The claimer in the case that funds get trapped. Expected to be an XCM::v5::Location.
	pub claimer: Option<Vec<u8>>,
	/// Native ether bridged over from Ethereum
	pub value: u128,
	/// Fee in eth to cover the xcm execution on AH.
	pub execution_fee: u128,
	/// Relayer reward in eth. Needs to cover all costs of sending a message.
	pub relayer_fee: u128,
}
```

**File:** bridges/snowbridge/primitives/inbound-queue/src/v2/message.rs (L194-215)
```rust
impl TryFrom<&IGatewayV2::Payload> for Payload {
	type Error = MessageDecodeError;

	fn try_from(payload: &GatewayV2Payload) -> Result<Self, Self::Error> {
		let xcm = match payload.xcm.kind {
			0 => Payload::Raw(payload.xcm.data.to_vec()),
			1 => {
				let create_asset =
					IGatewayV2::XcmCreateAsset::abi_decode_validate(&payload.xcm.data)
						.map_err(|_| MessageDecodeError)?;
				// Convert u8 network to Network enum
				let network = match create_asset.network {
					0 => Network::Polkadot,
					_ => return Err(MessageDecodeError),
				};
				Payload::CreateAsset { token: H160::from(create_asset.token.as_ref()), network }
			},
			_ => return Err(MessageDecodeError),
		};
		Ok(xcm)
	}
}
```
