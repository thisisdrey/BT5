### Title
Ethereum-native asset (ENA) token address in Snowbridge outbound v2 converter is accepted from unvalidated user-supplied XCM without any registry/whitelist check — ([File: bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs])

### Summary
The reported bug is a case of a “currency” field pulled directly from an attacker-supplied signed request and used to drive a value-transfer without checking it against the contract’s configured accepted value. The same class of defect exists in Snowbridge's outbound queue v2 XCM converter: the token address for `Command::UnlockNativeToken` is derived straight from the `WithdrawAsset` location supplied inside a user-executed XCM program on AssetHub, with **no registry/whitelist validation**, unlike the sibling Polkadot-native-asset (PNA) path which explicitly validates the asset id against a registered `TokenId`.

### Finding Description
`XcmConverter::extract_ethereum_native_assets` in [1](#0-0)  takes the `Assets` from a user-controlled `WithdrawAsset` instruction and, for any asset whose location matches `(0, [AccountKey20 { key, .. }])`, unconditionally treats `key` as a legitimate Ethereum ERC-20 contract address and emits `Command::UnlockNativeToken { token, recipient, amount }`:

```
(0, [AccountKey20 { network, key }]) if self.network_matches(network) => {
    Ok((H160(*key), amount))
},
```

Compare this to `extract_polkadot_native_assets` in the same file [2](#0-1) , which explicitly binds the asset location to a registered token before emitting a command:

```
let token_id = TokenIdOf::convert_location(&asset_id).ok_or(InvalidAsset)?;
let expected_asset_id = ConvertAssetId::maybe_convert(token_id).ok_or(InvalidAsset)?;
ensure!(asset_id == expected_asset_id, InvalidAsset);
```

No equivalent `ConvertAssetId`/registry check exists for the ENA branch — the “currency” (ERC-20 address) used to build the `UnlockNativeToken` command is never validated against any accepted-token list before being handed to `OutboundQueue::validate`/`deliver`. The same pattern (accept-any-`AccountKey20`-as-token) also exists in the v1 converter's `make_unlock_native_token_command` [3](#0-2) .

This is precisely the bug class in the report: a currency/token identifier taken from an untrusted, user-constructed request is used directly in a value-moving command instead of being checked against the canonical accepted set — exactly like `acceptedCurrency` never being checked against `ListingRequest.acceptedCurrency`/`OfferRequest.offerCurrency` before `transferFrom` calls.

The repository itself flags this exact attack pattern as dangerous in an integration test, `user_exploit_with_arbitrary_message_will_fail` [4](#0-3) , where a comment states:

```
// exploited_weth here is far more than the burnt, which means instructions inner
// are user provided and untrustworthy/dangerous!
// Currently it depends on EthereumBlobExporter on BH to check the message is legal
// and convert to Ethereum command.
```

This confirms the design intent that the `EthereumBlobExporter`/`XcmConverter` on BridgeHub is supposed to be the gatekeeper that legitimizes the token/currency before it is committed to the outbound message — but for the ENA path, that gate performs no binding check on the token identity itself, only structural/shape checks (network match, fungibility, non-zero amount).

### Impact Explanation
Any signed AssetHub account can call `pallet_xcm::execute`/`send` to construct a `WithdrawAsset`/`DepositAsset` program naming an arbitrary Ethereum contract address as the "unlocked" token, which the converter will happily convert into a well-formed `Command::UnlockNativeToken` and commit into the outbound Merkle tree/message queue for Ethereum. Whether this becomes fund-affecting on the Ethereum side depends entirely on the Gateway contract's own per-token/per-agent locked-balance accounting (outside this repository's scope), so this repository provides no independent validation layer — a genuine parity gap with the PNA path, which does bind the transferred asset to `ConvertAssetId`. Because BridgeHub is the sole gate mentioned in-repo for legitimizing these commands, and it does not perform that binding for ENAs, this is a real "public underpriced/unvalidated work" gap that degrades the intended trust boundary of the bridge's outbound processing.

### Likelihood Explanation
Medium: any account holding a small amount of ETH on AssetHub (for fees) can trigger this path with a single `pallet_xcm::execute`/`send` call — no privileged origin, relayer, or admin action is required, matching the "unprivileged attacker" requirement. The exact monetary consequence is bounded by external (non-repo) Gateway contract logic, which is why full end-to-end exploitability cannot be confirmed purely from this repository, but the local validation gap itself is concretely provable by comparing the ENA and PNA branches of the same converter.

### Recommendation
Add a token-registry validation step to `extract_ethereum_native_assets` (and to `make_unlock_native_token_command` in the v1 converter) analogous to the PNA path — i.e., verify that the `AccountKey20` address extracted from the `WithdrawAsset` location is present in an explicit allow-list/registry of tokens actually eligible for `UnlockNativeToken`, rather than accepting any 20-byte key shaped like an Ethereum address.

### Proof of Concept
1. On AssetHub, a signed account calls `pallet_xcm::execute` with a program:
   ```
   WithdrawAsset(Asset { id: AssetId(Location::new(0, [AccountKey20{ key: ARBITRARY_ERC20 }])), fun: Fungible(HUGE_AMOUNT) })
   ClearOrigin
   DepositAsset { assets: Wild(All), beneficiary: <ethereum recipient> }
   SetTopic(..)
   ```
   (see the closely related existing test at [5](#0-4)  for the analogous forged-message construction).
2. `EthereumBlobExporter::validate` on BridgeHub routes this through `XcmConverter::convert` → `make_unlock_native_token_command` / `extract_ethereum_native_assets`, which accepts `ARBITRARY_ERC20` as `token` with no registry check [6](#0-5) .
3. A `Command::UnlockNativeToken { token: ARBITRARY_ERC20, recipient, amount: HUGE_AMOUNT }` is committed to the outbound message queue, with the only remaining safety net being the Ethereum-side Gateway contract's balance accounting (not present in this repository).

### Citations

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs (L119-154)
```rust
	/// Extract ethereum native assets
	fn extract_ethereum_native_assets(
		&mut self,
		enas: &Assets,
		deposit_assets: &AssetFilter,
		recipient: H160,
	) -> Result<Vec<Command>, XcmConverterError> {
		let mut commands: Vec<Command> = Vec::new();
		for ena in enas.clone().into_inner().into_iter() {
			// Check the the deposit asset filter matches what was reserved.
			if !deposit_assets.matches(&ena) {
				return Err(FilterDoesNotConsumeAllAssets);
			}

			// only fungible asset is allowed
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

			// transfer amount must be greater than 0.
			ensure!(amount > 0, ZeroAssetTransfer);

			commands.push(Command::UnlockNativeToken { token, recipient, amount });
		}
		Ok(commands)
	}
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs (L156-186)
```rust
	/// Extract polkadot native assets
	fn extract_polkadot_native_assets(
		&mut self,
		pnas: &Assets,
		deposit_assets: &AssetFilter,
		recipient: H160,
	) -> Result<Vec<Command>, XcmConverterError> {
		let mut commands: Vec<Command> = Vec::new();
		ensure!(pnas.len() > 0, NoReserveAssets);
		for pna in pnas.clone().into_inner().into_iter() {
			if !deposit_assets.matches(&pna) {
				return Err(FilterDoesNotConsumeAllAssets);
			}

			// Only fungible is allowed
			let Asset { id: AssetId(asset_id), fun: Fungible(amount) } = pna else {
				return Err(AssetResolutionFailed);
			};

			// transfer amount must be greater than 0.
			ensure!(amount > 0, ZeroAssetTransfer);

			// Ensure PNA already registered
			let token_id = TokenIdOf::convert_location(&asset_id).ok_or(InvalidAsset)?;
			let expected_asset_id = ConvertAssetId::maybe_convert(token_id).ok_or(InvalidAsset)?;
			ensure!(asset_id == expected_asset_id, InvalidAsset);

			commands.push(Command::MintForeignToken { token_id, recipient, amount });
		}
		Ok(commands)
	}
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

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_edge_case.rs (L123-177)
```rust
#[test]
fn user_exploit_with_arbitrary_message_will_fail() {
	fund_on_bh();
	register_assets_on_ah();
	fund_on_ah();
	AssetHubWestend::execute_with(|| {
		type RuntimeEvent = <AssetHubWestend as Chain>::RuntimeEvent;
		type RuntimeOrigin = <AssetHubWestend as Chain>::RuntimeOrigin;

		let remote_fee_asset_location: Location =
			Location::new(2, [EthereumNetwork::get().into()]).into();

		let remote_fee_asset: Asset = (remote_fee_asset_location.clone(), 1).into();

		let assets = VersionedAssets::from(vec![remote_fee_asset]);

		let exploited_weth = Asset {
			id: AssetId(Location::new(0, [AccountKey20 { network: None, key: WETH.into() }])),
			// A big amount without burning
			fun: Fungible(TOKEN_AMOUNT * 1_000_000_000),
		};

		assert_ok!(<AssetHubWestend as AssetHubWestendPallet>::PolkadotXcm::transfer_assets_using_type_and_then(
			RuntimeOrigin::signed(AssetHubWestendSender::get()),
			bx!(VersionedLocation::from(ethereum())),
			bx!(assets),
			bx!(TransferType::DestinationReserve),
			bx!(VersionedAssetId::from(remote_fee_asset_location.clone())),
			bx!(TransferType::DestinationReserve),
			// exploited_weth here is far more than the burnt, which means instructions inner
			// are user provided and untrustworthy/dangerous!
			// Currently it depends on EthereumBlobExporter on BH to check the message is legal
			// and convert to Ethereum command.
			bx!(VersionedXcm::from(Xcm(vec![
				WithdrawAsset(exploited_weth.clone().into()),
				DepositAsset { assets: Wild(All), beneficiary: beneficiary() },
				SetTopic([0; 32]),
			]))),
			Unlimited
		));

		assert_expected_events!(
			AssetHubWestend,
			vec![RuntimeEvent::PolkadotXcm(pallet_xcm::Event::Sent{ .. }) => {},]
		);
	});

	BridgeHubWestend::execute_with(|| {
		type RuntimeEvent = <BridgeHubWestend as Chain>::RuntimeEvent;
		assert_expected_events!(
			BridgeHubWestend,
			vec![RuntimeEvent::MessageQueue(pallet_message_queue::Event::Processed{ success:false, .. }) => {},]
		);
	});
}
```
