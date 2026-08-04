Based on the evidence gathered, I found a concrete local analog to the "missing whitelist check" bug class in Snowbridge's inbound message converter.

### Title
Unregistered `NativeTokenERC20` asset accepted by inbound queue v2 converter without registry check, enabling asset trapping/fund freeze on AssetHub - (File: bridges/snowbridge/primitives/inbound-queue/src/v2/converter.rs)

### Summary
The `MessageToXcm::prepare` function processes `EthereumAsset` variants from an Ethereum-originated message. For `ForeignTokenERC20`, the code resolves the token via `ConvertAssetId::maybe_convert(*token_id)`, failing the whole conversion with `ConvertMessageError::InvalidAsset` if the token isn't found in the registry [1](#0-0) . But for `NativeTokenERC20`, the only check performed is that `token_id != H160::zero()` — there is no lookup against any registry of tokens actually known/registered on the Polkadot side before constructing the `ReserveDeposit` asset instruction [2](#0-1) .

### Finding Description
The `EthereumAsset::NativeTokenERC20 { token_id, value }` branch synthesizes a `Location` of the form `(2, [GlobalConsensus(Ethereum), AccountKey20(token_id)])` and unconditionally pushes it as `AssetTransfer::ReserveDeposit` into the message's asset list [2](#0-1) . This differs structurally from the `ForeignTokenERC20` branch, which explicitly validates the token against `ConvertAssetId` (backed by `ForeignToNativeId`/token registry storage) and errors out via `ConvertMessageError::InvalidAsset` if unregistered — confirmed by the `test_invalid_foreign_erc20` unit test [3](#0-2) .

Because verification of the inbound message (via the beacon/header proof and merkle proof of the `OutboundMessageAccepted` Ethereum event log) only proves that the Gateway contract on Ethereum emitted this event — it does not by itself prove the `token_id` corresponds to any token actually registered/known to Polkadot's asset system. The commented-out whitelist check in the original Anchor bridge report is structurally analogous to this asymmetric validation: one asset path checks registration before acceptance, the other path does not.

The resulting `ReserveAssetDeposited`/`WithdrawAsset` instruction is then dispatched into the XCM on AssetHub [4](#0-3) . If the corresponding beneficiary-facing `DepositAsset` instruction in the attacker-supplied `remote_xcm` payload references this unregistered asset, the XCM executor's `AssetTransactor`/`FungiblesAdapter` will not recognize the asset (no matching `ForeignAssets`/`TrustBackedAssets` entry), causing the deposit to fail while the asset remains in the XCM holding register. Per `pallet-xcm` semantics, assets left in holding at the end of execution are trapped (`AssetsTrapped` event) — only recoverable via a manual claim mechanism, not automatically returned to the sender. This mirrors the report's core invariant break: **omission of a whitelist/registration check on one specific asset-kind path allows an attacker-controlled token identifier to enter processing that assumes registration, leading to funds becoming stuck/inaccessible in normal flow.**

### Impact Explanation
Any relayer (unprivileged, permissionless — anyone can call `submit` on `EthereumInboundQueueV2` once they have a valid Ethereum proof) can carry a Gateway event containing a `NativeTokenERC20` asset with an arbitrary/unregistered `token_id`, since the Gateway contract emission of `OutboundMessageAccepted` is the thing proven, not the token's registration status on the Polkadot side. This can cause AssetHub-side XCM execution to trap the corresponding "reserve deposit" value, freezing/locking those funds (or making them require manual, non-automatic recovery), which aligns with the "permanent user-fund or bridge-state lock" impact class from the pivots.

### Likelihood Explanation
The path requires no privileged actor: any relayer can submit a proof for a genuine Ethereum Gateway event containing attacker-crafted asset data (since the Gateway/message-sender side ultimately determines what `NativeTokenERC20.token_id` value is emitted, and the report explicitly discusses cross-chain bridges where non-whitelisted tokens are the attack surface). The asymmetry between the two branches (`ForeignTokenERC20` validates, `NativeTokenERC20` doesn't) is directly visible in the source and is the same shape as the original disclosed bug (a whitelist check present for one path but missing/disabled for another equivalent path).

### Recommendation
Add an explicit registry/whitelist check for `EthereumAsset::NativeTokenERC20` in `MessageToXcm::prepare`, mirroring the `ConvertAssetId::maybe_convert` validation used for `ForeignTokenERC20`, before constructing the `ReserveDeposit` asset and pushing it into the XCM. Reject the message with `ConvertMessageError::InvalidAsset` if the `token_id` is not a known/registered native asset, consistent with how `RegisterForeignToken`/`ForeignToNativeId` records legitimate cross-chain assets.

### Proof of Concept
1. An attacker (or complicit relayer) triggers the real Snowbridge Gateway contract on Ethereum to emit `OutboundMessageAccepted` with an `EthereumAsset[]` entry of kind `NativeTokenERC20` where `token_id` is an address that has never been registered/reanchored on the Polkadot side (only constraint enforced client-side is `token_id != H160::zero()`, per [5](#0-4) ).
2. Any relayer submits this event with a valid proof to `EthereumInboundQueueV2::submit`; verification succeeds because it only proves the log occurred, not that the token is registered.
3. `MessageToXcm::convert` builds `WithdrawAsset`/`ReserveAssetDeposited` instructions for this unregistered token location and appends the attacker-supplied `remote_xcm` (e.g., a `DepositAsset` targeting a beneficiary) as seen in the `send_token_v2` test structure [6](#0-5) .
4. On AssetHub, the XCM executor cannot match the unregistered asset location to any live `ForeignAssets`/`TrustBackedAssets` entry; the `DepositAsset` instruction fails for that asset, and it remains in the holding register at the end of execution, becoming trapped (`pallet_xcm::Event::AssetsTrapped`) rather than delivered to any beneficiary — funds effectively frozen pending manual governance-level recovery, unlike the explicit upfront rejection that exists for the `ForeignTokenERC20` path.

Note: I could not directly inspect the Solidity Gateway contract's `v2_sendMessage`/token-locking logic in this index (no matches found for `v2_sendMessage`/`isTokenRegistered` in the repo), so I cannot fully confirm whether the Ethereum-side contract independently restricts which `token_id` values can be emitted as `NativeTokenERC20`. This is a gap in my verification — if the Solidity Gateway strictly enforces registration before emitting such events, the exploitability of this specific Rust-side asymmetry would be reduced to defense-in-depth rather than a directly reachable attack. A Devin session with full repository/browser access would be needed to inspect the Solidity contracts under `bridges/snowbridge/contracts/` to close this gap.

### Citations

**File:** bridges/snowbridge/primitives/inbound-queue/src/v2/converter.rs (L169-180)
```rust
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

**File:** bridges/snowbridge/primitives/inbound-queue/src/v2/converter.rs (L181-184)
```rust
				EthereumAsset::ForeignTokenERC20 { token_id, value } => {
					let asset_location = ConvertAssetId::maybe_convert(*token_id)
						.ok_or(ConvertMessageError::InvalidAsset)?;
					let asset_hub_from_ethereum: Location = Location::new(
```

**File:** bridges/snowbridge/primitives/inbound-queue/src/v2/converter.rs (L399-411)
```rust
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
```

**File:** bridges/snowbridge/primitives/inbound-queue/src/v2/converter.rs (L692-727)
```rust
	#[test]
	fn test_invalid_foreign_erc20() {
		let origin: H160 = hex!("29e3b139f4393adda86303fcdaa35f60bb7092bf").into();
		let token_id: H256 =
			hex!("37a6c666da38711a963d938eafdd09314fd3f95a96a3baffb55f26560f4ecdd8").into();
		let beneficiary =
			hex!("908783d8cd24c9e02cee1d26ab9c46d458621ad0150b626c536a40b9df3f09c6").into();
		let message_id: H256 =
			hex!("8b69c7e376e28114618e829a7ec768dbda28357d359ba417a3bd79b11215059d").into();
		let token_value = 3_000_000_000_000u128;
		let assets = vec![EthereumAsset::ForeignTokenERC20 { token_id, value: token_value }];
		let instructions = vec![
			DepositAsset { assets: Wild(AllCounted(1).into()), beneficiary },
			SetTopic(message_id.into()),
		];
		let xcm: Xcm<()> = instructions.into();
		let versioned_xcm = VersionedXcm::V5(xcm);
		let claimer_account = AccountId32 { network: None, id: H256::random().into() };
		let claimer: Option<Vec<u8>> = Some(claimer_account.clone().encode());
		let value = 0;
		let execution_fee = 1_000_000_000_000u128;
		let relayer_fee = 5_000_000_000_000u128;

		let message = Message {
			gateway: H160::zero(),
			nonce: 0,
			origin,
			assets,
			payload: Payload::Raw(versioned_xcm.encode()),
			claimer,
			value,
			execution_fee,
			relayer_fee,
		};

		assert_err!(ConverterFailing::convert(message), ConvertMessageError::InvalidAsset);
```

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_inbound.rs (L162-200)
```rust
	let assets = vec![
		// the token being transferred
		NativeTokenERC20 { token_id: token.into(), value: token_transfer_value },
	];

	set_up_eth_and_dot_pool();
	let topic_id = BridgeHubWestend::execute_with(|| {
		type RuntimeEvent = <BridgeHubWestend as Chain>::RuntimeEvent;
		let instructions = vec![
			RefundSurplus,
			DepositAsset {
				assets: Wild(AllOf {
					id: AssetId(token_location.clone()),
					fun: WildFungibility::Fungible,
				}),
				beneficiary,
			},
			DepositAsset {
				assets: Wild(AllOf { id: AssetId(eth_location()), fun: WildFungibility::Fungible }),
				beneficiary: claimer,
			},
		];
		let xcm: Xcm<()> = instructions.into();
		let versioned_message_xcm = VersionedXcm::V5(xcm);
		let origin = H160::random();

		let message = Message {
			gateway: EthereumGatewayAddress::get(),
			nonce: 1,
			origin,
			assets,
			payload: Payload::Raw(versioned_message_xcm.encode()),
			claimer: Some(claimer_bytes),
			value: 1_500_000_000_000u128,
			execution_fee: 1_500_000_000_000u128,
			relayer_fee: relayer_reward,
		};

		EthereumInboundQueueV2::process_message(relayer_account.clone(), message.clone()).unwrap();
```
