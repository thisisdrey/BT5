## Analysis

The Curve-pool bug's core invariant is: **a helper function rewrites a "native asset" sentinel address into the value used elsewhere to represent the chain's true native asset, and this rewritten value is then fed — unchecked — into a per-asset lookup/creation path that assumes it only ever receives *distinct ERC20/token* identifiers.** That collision lets an attacker force the system to treat the reserved "native" slot as an arbitrary user-suppliable token id.

Snowbridge's inbound-queue v1 message converter contains the exact same pattern. [1](#0-0) 

`convert_token_address` rewrites `H160([0;20])` (the sentinel Ethereum uses for "native ETH") into `Location::new(2, [GlobalConsensus(network)])` — which is **the same location used everywhere else in Snowbridge as the canonical representation of the Ethereum network / native ETH itself** (identical to `bridge_location` computed two lines above, and identical to `ether_location` used pervasively in the v2 converter for all Ether-denominated `ReserveAssetDeposited`/`WithdrawAsset`/`PayFees` instructions). [2](#0-1) 

`convert_register_token` — reachable from the permissionless `Command::RegisterToken { token, fee }` inbound message, which originates from *any* Ethereum-side caller of the Gateway's `registerToken(token)` function, no relayer/governance/key compromise required — takes the attacker-supplied `token: H160` and passes it straight into `convert_token_address` with **no validation that `token != H160::zero()`**:
```rust
let bridge_location = Location::new(2, GlobalConsensus(network));
...
let asset_id = Self::convert_token_address(network, token);   // token == 0x0 → asset_id == bridge_location
...
Transact { ... call: (create_call_index, asset_id, MultiAddress::Id(owner), MINIMUM_DEPOSIT).encode() ... }
```

This `Transact` invokes `create_asset` on AssetHub's `ForeignAssets` pallet with `asset_id = bridge_location`, i.e. it registers a ForeignAsset class keyed by the location that Snowbridge's own protocol reserves to denote "native ETH from this Ethereum chain."

Crucially, the sibling v2 code path for the analogous case *does* have the missing guard, proving Parity engineers already know this exact collision must be blocked but only fixed it in one of the two places: [3](#0-2) 
```rust
EthereumAsset::NativeTokenERC20 { token_id, value } => {
    ensure!(*token_id != H160::zero(), ConvertMessageError::InvalidAsset);
    ...
```
The v1 `RegisterToken`/`convert_register_token` path has no equivalent `ensure!`.

### Title
Unvalidated `token = 0x0` in Snowbridge V1 `RegisterToken` lets an unprivileged Ethereum caller squat AssetHub's reserved native‑ETH asset location - (File: bridges/snowbridge/primitives/inbound-queue/src/v1.rs)

### Summary
`MessageToXcm::convert_register_token` (v1 inbound queue) builds the `create_asset` `Transact` call using `asset_id = Self::convert_token_address(network, token)` without checking that the attacker-controlled Ethereum `token` address is non-zero. `convert_token_address` deliberately maps `H160::zero()` to `Location::new(2, [GlobalConsensus(network)])`, the same location Snowbridge reserves to represent the Ethereum network / native ETH itself elsewhere in the protocol (see `bridge_location` in the same function and `ether_location` in the v2 converter). Any account on Ethereum can call the Gateway's `registerToken` with `token = 0x0000...0000` to make BridgeHub emit a `create_asset` Transact for that reserved location.

### Finding Description
`RegisterToken` is a permissionless command: any Ethereum account may trigger it via the Gateway contract, and BridgeHub's inbound-queue v1 pallet transcodes it into XCM with no additional validation of the `token` field. [4](#0-3) [5](#0-4) 

`convert_token_address` special-cases the zero address: [1](#0-0) 

`convert_register_token` feeds the attacker-controlled `token` straight into that function to produce the `asset_id` used in the `create_asset` Transact, with no `ensure!(token != H160::zero())` guard analogous to the one that exists in the v2 asset-transfer converter: [6](#0-5) 

Because `Location::new(2, GlobalConsensus(network))` is exactly the location Snowbridge treats as "the Ethereum chain / its native asset" everywhere else (compare `bridge_location` in the very same function and `ether_location` in `bridges/snowbridge/primitives/inbound-queue/src/v2/converter.rs:127`), this call registers a `ForeignAssets` entry keyed by that reserved value instead of by a genuine per-ERC20-token location.

### Impact Explanation
- Any unprivileged Ethereum account (no relayer, validator, governance, or key compromise needed) can force AssetHub to create (or attempt to create) a `ForeignAssets` class at the exact location reserved for representing native ETH/the Ethereum bridge root.
- If this registration lands before the protocol's own legitimate configuration/registration for native ETH accounting occurs, subsequent legitimate `create_asset` calls for that same location fail (`AssetAlreadyExists`), permanently blocking correct native-ETH asset-class setup on that AssetHub — a public, underpriced action that can stall Ether-denominated bridge processing (matches the "public underpriced work that … stalls bridge processing" and "permanent … bridge-state lock" impact categories).
- Even if ordering makes exploitation only a griefing/DoS vector rather than fund theft, it demonstrates the same broken invariant as the audit finding: a "native asset sentinel → rewritten value" collision that lets attacker input alias a reserved system identifier, defeating the assumption (already enforced in the v2 sibling code) that user-supplied token identifiers can never equal the reserved native-asset value.

### Likelihood Explanation
High feasibility: the `RegisterToken` command is fully attacker-controlled from the Ethereum side (arbitrary `token` value), requires no elevated privileges, and the missing check is a simple omission — the fix already exists one file over (`ensure!(*token_id != H160::zero(), ...)` in `v2/converter.rs`), confirming the developers consider zero-address token inputs a real attack surface that was only partially remediated.

### Recommendation
Add the same guard used in the v2 converter to `convert_register_token` (and any other v1 command handler that maps an Ethereum `token: H160` through `convert_token_address`):
```rust
ensure!(token != H160::zero(), ConvertMessageError::InvalidToken);
```
before computing `asset_id`, so the reserved native-ETH/bridge-root location can never be targeted by a user-supplied `RegisterToken` command.

### Proof of Concept
1. On Ethereum, call `Gateway.registerToken(token = 0x0000000000000000000000000000000000000000, fee = <valid fee>)`.
2. The message is relayed and decoded by BridgeHub's inbound-queue v1 pallet as `Command::RegisterToken { token: H160::zero(), fee }`.
3. `MessageToXcm::convert(...)` dispatches to `convert_register_token`, which calls `convert_token_address(network, H160::zero())`, returning `asset_id = Location::new(2, [GlobalConsensus(network)])` — the same value as `bridge_location`.
4. The resulting XCM's `Transact` invokes `create_asset(asset_id, owner, MINIMUM_DEPOSIT)` on AssetHub's ForeignAssets pallet using this reserved location as the asset identifier, succeeding with no validation error, since no code path checks `token != 0x0` in v1 (unlike the equivalent `ensure!` present in `bridges/snowbridge/primitives/inbound-queue/src/v2/converter.rs:170`).

### Citations

**File:** bridges/snowbridge/primitives/inbound-queue/src/v1.rs (L36-44)
```rust
#[derive(Clone, Encode, Decode, Debug)]
pub enum Command {
	/// Register a wrapped token on the AssetHub `ForeignAssets` pallet
	RegisterToken {
		/// The address of the ERC20 token to be bridged over to AssetHub
		token: H160,
		/// XCM execution fee on AssetHub
		fee: u128,
	},
```

**File:** bridges/snowbridge/primitives/inbound-queue/src/v1.rs (L179-188)
```rust
	fn convert(
		message_id: H256,
		message: VersionedMessage,
	) -> Result<(Xcm<()>, Self::Balance), ConvertMessageError> {
		use Command::*;
		use VersionedMessage::*;
		match message {
			V1(MessageV1 { chain_id, command: RegisterToken { token, fee } }) => {
				Ok(Self::convert_register_token(message_id, chain_id, token, fee))
			},
```

**File:** bridges/snowbridge/primitives/inbound-queue/src/v1.rs (L237-296)
```rust
	fn convert_register_token(
		message_id: H256,
		chain_id: u64,
		token: H160,
		fee: u128,
	) -> (Xcm<()>, Balance) {
		let network = Ethereum { chain_id };
		let xcm_fee: Asset = (Location::parent(), fee).into();
		let deposit: Asset = (Location::parent(), CreateAssetDeposit::get()).into();

		let total_amount = fee + CreateAssetDeposit::get();
		let total: Asset = (Location::parent(), total_amount).into();

		let bridge_location = Location::new(2, GlobalConsensus(network));

		let owner = EthereumLocationsConverterFor::<[u8; 32]>::from_chain_id(&chain_id);
		let asset_id = Self::convert_token_address(network, token);
		let create_call_index: [u8; 2] = CreateAssetCall::get();
		let inbound_queue_pallet_index = InboundQueuePalletInstance::get();

		let xcm: Xcm<()> = vec![
			// Teleport required fees.
			ReceiveTeleportedAsset(total.into()),
			// Pay for execution.
			BuyExecution { fees: xcm_fee, weight_limit: Unlimited },
			// Fund the snowbridge sovereign with the required deposit for creation.
			DepositAsset { assets: Definite(deposit.into()), beneficiary: bridge_location.clone() },
			// This `SetAppendix` ensures that `xcm_fee` not spent by `Transact` will be
			// deposited to snowbridge sovereign, instead of being trapped, regardless of
			// `Transact` success or not.
			SetAppendix(Xcm(vec![
				RefundSurplus,
				DepositAsset { assets: AllCounted(1).into(), beneficiary: bridge_location },
			])),
			// Only our inbound-queue pallet is allowed to invoke `UniversalOrigin`.
			DescendOrigin(PalletInstance(inbound_queue_pallet_index).into()),
			// Change origin to the bridge.
			UniversalOrigin(GlobalConsensus(network)),
			// Call create_asset on foreign assets pallet.
			Transact {
				origin_kind: OriginKind::Xcm,
				fallback_max_weight: Some(Weight::from_parts(400_000_000, 8_000)),
				call: (
					create_call_index,
					asset_id,
					MultiAddress::<[u8; 32], ()>::Id(owner),
					MINIMUM_DEPOSIT,
				)
					.encode()
					.into(),
			},
			// Forward message id to Asset Hub
			SetTopic(message_id.into()),
			// Once the program ends here, appendix program will run, which will deposit any
			// leftover fee to snowbridge sovereign.
		]
		.into();

		(xcm, total_amount.into())
	}
```

**File:** bridges/snowbridge/primitives/inbound-queue/src/v1.rs (L392-402)
```rust
	// Convert ERC20 token address to a location that can be understood by Assets Hub.
	fn convert_token_address(network: NetworkId, token: H160) -> Location {
		if token == H160([0; 20]) {
			Location::new(2, [GlobalConsensus(network)])
		} else {
			Location::new(
				2,
				[GlobalConsensus(network), AccountKey20 { network: None, key: token.into() }],
			)
		}
	}
```

**File:** bridges/snowbridge/primitives/inbound-queue/src/v2/converter.rs (L167-180)
```rust
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
```
