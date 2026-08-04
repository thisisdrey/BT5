## Title
Missing zero-address check in Snowbridge `CreateAsset` payload lets an attacker register an ERC‑20 ForeignAsset that collides with the native-ETH sentinel, corrupting `UnlockNativeToken` accounting - (File: `bridges/snowbridge/primitives/inbound-queue/src/v2/converter.rs`)

### Summary
Snowbridge represents "native Ether" on the Ethereum side of a bridge message using the sentinel address `H160([0;20])` (`address(0)`), exactly like the Connext `Executor`/`AssetLogic` bug referenced in the external report. The inbound-queue v2 converter is inconsistent about validating this sentinel: the `NativeTokenERC20` asset branch explicitly rejects `token_id == H160::zero()`, but the sibling `Payload::CreateAsset` branch — which registers a brand-new ERC‑20 as a `ForeignAssets` entry on AssetHub — performs no such check. This lets a foreign asset be registered at the Location that later serializes back to the exact same zero-address sentinel used for native ETH, so the outbound queue conflates "withdraw this specific (fake) ERC‑20" with "unlock native ETH."

### Finding Description
In `MessageToXcm::prepare` (`bridges/snowbridge/primitives/inbound-queue/src/v2/converter.rs:145-200`):

- For `EthereumAsset::NativeTokenERC20`, the code explicitly guards the sentinel: [1](#0-0) 

- For `Payload::CreateAsset { token, network }`, dispatched to `make_create_asset_xcm`, there is **no equivalent check**: [2](#0-1) [3](#0-2) 

`make_create_asset_xcm` builds `asset_id = Location::new(2, [GlobalConsensus(Ethereum), AccountKey20 { key: (*token).into() }])` unconditionally and issues a `Transact` that creates this as a `ForeignAssets` entry with Ethereum set as its reserve, with no rejection of `token == H160::zero()`.

On the return path (Polkadot → Ethereum), the outbound queue converters use exactly this pattern to distinguish "specific ERC‑20 token" from "native Ether", and explicitly document that a zero key collapses to the ETH sentinel: [4](#0-3) 

The v1 converter and its own test comment confirm the sentinel semantics on the Solidity/Gateway side: [5](#0-4) [6](#0-5) 

Because `CreateAsset` accepts `token = 0x0`, an attacker can cause AssetHub to create a `ForeignAssets` entry at `Location::new(2, [GlobalConsensus(Ethereum), AccountKey20 { key: [0;20] }])` — a location that, once reanchored for an outbound message, degenerates to the same `(0, [AccountKey20 { key: [0;20] }])` shape matched by `extract_ethereum_native_assets`, producing `Command::UnlockNativeToken { token: H160([0;20]), .. }`. This is bit-for-bit identical to the command produced when transferring real, reserved native Ether. The Gateway contract on Ethereum treats `token == address(0)` as "this is ETH, not an ERC‑20 call" — exactly the ambiguity the external report describes for `Executor`/`AssetLogic`. Any subsequent transfer of the bogus "ERC-20 at 0x0" asset out of AssetHub is converted by the bridge into an ETH-unlock instruction against the agent's real Ether balance, rather than a transfer of the (nonexistent) ERC-20 token — a mismatch between the intended asset and the asset that actually settles.

### Impact Explanation
This breaks the invariant that bridge commands must bind exactly one asset identity and settle to the correct amount/asset: a `ForeignAssets` balance denominated in a fake ERC‑20 can be redeemed as native ETH from the agent contract, an unbacked/misrouted unlock of bridge-held funds and corruption of AssetHub's asset ledger versus Ethereum-side reserves — squarely in-scope as "theft or unbacked mint or unlock" / "runtime bugs that compromise intended behavior" for BridgeHub code.

### Likelihood Explanation
Triggering `CreateAsset` requires only that the (already-verified, non-privileged) Ethereum Gateway emit a `v2_registerToken`-style event for token `0x0` — no relayer collusion, no governance, and no validator/collator compromise is needed; the relayer merely delivers the standard header/receipt proof for a legitimately emitted event. The missing check is a straightforward oversight relative to the sibling `NativeTokenERC20` branch in the very same function, making the code path directly reachable via the pallet's normal message-processing flow.

### Recommendation
Add the same guard used for `NativeTokenERC20` to the `CreateAsset` path: reject `token == H160::zero()` in `make_create_asset_xcm` (or in `prepare` before dispatch), and audit all other locations deriving `AccountKey20 { key: token }` from user/Ethereum-supplied token addresses (inbound and outbound, v1 and v2) to consistently forbid the zero-address sentinel from being used as, or aliasing to, any specific ERC‑20/PNA identity.

### Proof of Concept
1. On Ethereum, call the Gateway's token-registration entry point with token address `0x0000000000000000000000000000000000000000`, causing an `OutboundMessageAccepted` event with `Payload::CreateAsset { token: H160::zero(), network: Polkadot }`.
2. A relayer submits the standard proof; `EthereumInboundQueueV2::process_message` calls `MessageToXcm::prepare`, which calls `make_create_asset_xcm` — no rejection occurs (contrast with `ensure!(*token_id != H160::zero(), ...)` a few lines above for `NativeTokenERC20`).
3. AssetHub creates a `ForeignAssets` entry for `Location::new(2, [GlobalConsensus(Ethereum), AccountKey20 { key: [0;20] }])` with Ethereum set as its reserve.
4. Obtain a balance of this asset (e.g., via the deposit portion of the same `CreateAsset` message, or a subsequent transfer), then send it back to Ethereum via `InitiateTransfer`/`ReserveWithdraw`.
5. `extract_ethereum_native_assets` matches `(0, [AccountKey20 { key: [0;20] }])` and emits `Command::UnlockNativeToken { token: H160([0;20]), recipient, amount }` — identical to the command for real native ETH — causing the Gateway contract to release ETH instead of (non-existent) ERC-20 tokens.

### Citations

**File:** bridges/snowbridge/primitives/inbound-queue/src/v2/converter.rs (L145-154)
```rust
		let mut remote_xcm: Xcm<()> = match &message.payload {
			Payload::Raw(raw) => Self::decode_raw_xcm(raw),
			Payload::CreateAsset { token, network } => Self::make_create_asset_xcm(
				token,
				*network,
				message.value,
				bridge_owner,
				claimer.clone(),
			)?,
		};
```

**File:** bridges/snowbridge/primitives/inbound-queue/src/v2/converter.rs (L169-171)
```rust
				EthereumAsset::NativeTokenERC20 { token_id, value } => {
					ensure!(*token_id != H160::zero(), ConvertMessageError::InvalidAsset);
					let token_location: Location = Location::new(
```

**File:** bridges/snowbridge/primitives/inbound-queue/src/v2/converter.rs (L233-270)
```rust
	fn make_create_asset_xcm(
		token: &H160,
		network: super::message::Network,
		eth_value: u128,
		bridge_owner: AccountId,
		claimer: Location,
	) -> Result<Xcm<()>, ConvertMessageError> {
		let dot_asset = Location::new(1, Here);
		let dot_fee: xcm::prelude::Asset = (dot_asset, CreateAssetCall::get().deposit).into();

		let eth_asset: xcm::prelude::Asset =
			(Location::new(2, [GlobalConsensus(EthereumNetwork::get())]), eth_value).into();

		let create_call_index: [u8; 2] = CreateAssetCall::get().create_call;
		let create_min_blance: u128 = CreateAssetCall::get().min_balance;
		let set_reserves_call_index: [u8; 2] = CreateAssetCall::get().set_reserves_call;

		let asset_id = Location::new(
			2,
			[
				GlobalConsensus(EthereumNetwork::get()),
				AccountKey20 { network: None, key: (*token).into() },
			],
		);

		match network {
			super::message::Network::Polkadot => Ok(Self::make_create_asset_xcm_for_polkadot(
				create_call_index,
				set_reserves_call_index,
				create_min_blance,
				asset_id,
				bridge_owner,
				dot_fee,
				eth_asset,
				claimer,
			)),
		}
	}
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs (L134-146)
```rust
			let (token, amount) = match ena {
				Asset { id: AssetId(inner_location), fun: Fungible(amount) } => {
					match inner_location.unpack() {
						(0, [AccountKey20 { network, key }]) if self.network_matches(network) => {
							Ok((H160(*key), amount))
						},
						// To allow ether
						(0, []) => Ok((H160([0; 20]), amount)),
						_ => Err(AssetResolutionFailed),
					}
				},
				_ => Err(AssetResolutionFailed),
			}?;
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v1/converter/mod.rs (L289-305)
```rust
		let (token, amount) = match reserve_asset {
			Asset { id: AssetId(inner_location), fun: Fungible(amount) } => {
				match inner_location.unpack() {
					// Get the ERC20 contract address of the token.
					(0, [AccountKey20 { network, key }]) if self.network_matches(network) => {
						Some((H160(*key), *amount))
					},
					// If there is no ERC20 contract address in the location then signal to the
					// gateway that is a native Ether transfer by using
					// `0x0000000000000000000000000000000000000000` as the token address.
					(0, []) => Some((H160([0; 20]), *amount)),
					_ => None,
				}
			},
			_ => None,
		}
		.ok_or(AssetResolutionFailed)?;
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v1/converter/tests.rs (L534-542)
```rust
	// The token address that is expected to be sent should be
	// `0x0000000000000000000000000000000000000000`. The solidity will
	// interpret this as a transfer of ETH.
	let expected_payload = UnlockNativeToken {
		agent_id: Default::default(),
		token: H160([0; 20]),
		recipient: beneficiary_address.into(),
		amount: 1000,
	};
```
