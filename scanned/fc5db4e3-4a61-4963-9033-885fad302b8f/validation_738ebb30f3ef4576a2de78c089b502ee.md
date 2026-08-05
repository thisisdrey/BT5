## Finding

### Title
BridgeHub Rococo's Ethereum outbound exporter lacks the `AllowedAliasOrigin` guard, permitting `AliasOrigin` spoofing of the Asset Hub sovereign origin - ([File: cumulus/parachains/runtimes/bridge-hubs/bridge-hub-rococo/src/bridge_to_ethereum_config.rs])

### Summary
The external report's core broken invariant is: a privileged whitelist/authentication check that is supposed to bind a caller to its true source identity can be bypassed when the identity representation used by the check does not match reality, letting an unauthorized caller be treated as a trusted one. The local analog is in Snowbridge's V2 outbound-queue XCM converter, where an `AliasOrigin` instruction inside a user-submitted XCM program is used to set the effective origin exported to Ethereum. Parity fixed an origin-spoofing vulnerability here (see `prdoc/pr_12159.prdoc`) by adding a generic `AllowedAliasOrigin: Contains<Location>` parameter to `EthereumBlobExporter`/`XcmConverter`, with runtimes required to pass `EverythingBut<Equals<AssetHubLocation>>` so that no user-controlled `AliasOrigin` can forge the Asset Hub's own sovereign/root location (which the bridge treats as the trusted holder of the primary ERC-20 agent account).

`bridge-hub-westend`'s `bridge_to_ethereum_config.rs` and `xcm_config.rs` wire this fix in (6 matches for `EthereumBlobExporter`/`AllowedAliasOrigin`/`EverythingBut<Equals<AssetHubLocation>>`), but `bridge-hub-rococo`'s `bridge_to_ethereum_config.rs` only contains references to `EthereumBlobExporter` and is missing the `AllowedAliasOrigin`/`EverythingBut<Equals<AssetHubLocation>>` wiring entirely. [1](#0-0) [2](#0-1) 

### Finding Description
The Snowbridge V2 outbound converter interprets `AliasOrigin(Location)` inside a caller-supplied XCM program to decide which account is deemed the sender of the exported message to Ethereum. Because the Asset Hub sovereign/root location backs the bridge's high-value ERC-20 agent account, an attacker who can get `AliasOrigin(AssetHubLocation)` accepted would have their message treated as if Asset Hub itself authored it — an origin/identity-binding failure structurally identical to the report's "wrong caller address accepted as whitelisted sender." The fix added a `Contains<Location>` filter parameter (`AllowedAliasOrigin`) to `XcmConverter`/`EthereumBlobExporter`, with the expectation that every runtime instantiates it as `EverythingBut<Equals<AssetHubLocation>>`. [3](#0-2) 

`bridge-hub-westend` correctly threads this filter through its config (`AllowedAliasOrigin`, `EverythingBut<Equals<AssetHubLocation>>` appear together with `EthereumBlobExporter` in both `bridge_to_ethereum_config.rs` and `xcm_config.rs`). By contrast, `bridge-hub-rococo`'s `bridge_to_ethereum_config.rs` contains matches only for `EthereumBlobExporter`, with no occurrence of `AllowedAliasOrigin` or `EverythingBut<Equals<AssetHubLocation>>`. That indicates Rococo's exporter/converter instantiation was not updated with the same guard, so it likely still uses a permissive filter (e.g. `()`/`Everything`) that does not reject `AliasOrigin(AssetHubLocation)`.

### Impact Explanation
If confirmed at the type level (i.e., Rococo's `XcmConverter`/`EthereumBlobExporter` generic parameter is not `EverythingBut<Equals<AssetHubLocation>>`), an unprivileged XCM-executing account on Asset Hub Rococo (or any sibling chain able to route a V2-format message through the exporter) could submit `AliasOrigin(Location::new(1, [Parachain(AssetHubRococoParaId)]))` inside their outbound message. The converter would accept this as a valid origin override, and the message would be exported to Ethereum appearing to originate from Asset Hub's own agent/sovereign account — enabling unauthorized execution/asset movement against the bridge's primary agent holdings, which matches the "unauthorized execution or origin escalation" and "theft or unbacked mint/unlock" impact classes in scope.

### Likelihood Explanation
The attack requires only a normal signed account able to submit XCM `Transact`/`Execute` with a crafted V2 outbound-queue-compatible program — no validator, relayer, or governance privilege is needed, matching the required "unprivileged attacker, public entrypoint" profile. The same class of bug was already found and fixed on `bridge-hub-westend` (PR referenced in `prdoc/pr_12159.prdoc`), which strongly suggests Rococo shares the same exporter/converter code path and was simply missed in the fix's runtime rollout.

### Recommendation
Audit `cumulus/parachains/runtimes/bridge-hubs/bridge-hub-rococo/src/bridge_to_ethereum_config.rs` (and its `xcm_config.rs`) and instantiate `EthereumBlobExporter`'s `XcmConverter` with `AllowedAliasOrigin = EverythingBut<Equals<AssetHubRococoLocation>>`, mirroring the westend configuration, so `AliasOrigin` instructions matching Asset Hub's own location are rejected with `XcmConverterError::InvalidOrigin` as they are on Westend.

### Proof of Concept
The westend regression test `signed_assethub_user_cannot_forge_assethub_agent_origin` (in `cumulus/parachains/runtimes/bridge-hubs/bridge-hub-westend/tests/snowbridge.rs:195-293`) demonstrates the exact attack shape: a signed Asset Hub account builds an `ExportMessage` to Ethereum containing `AliasOrigin(Location::new(1, Parachain(assethub_parachain_id)))`, and on a properly configured runtime this is rejected (`XcmError::Unroutable`, zero messages queued). Because `bridge-hub-rococo`'s config lacks the equivalent `AllowedAliasOrigin` wiring, running the analogous test against Rococo's runtime would be expected to succeed instead of failing, confirming the forged-origin message is queued. [4](#0-3)

### Citations

**File:** prdoc/pr_12159.prdoc (L1-21)
```text
title: 'Snowbridge: blocks an origin-spoofing attack vector in the V2 outbound queue converter'
doc:
- audience: Runtime Dev
  description: |-
    Adds a validation check in the V2 XCM converter to reject AliasOrigin instructions
    that attempt to forge the Asset Hub sovereign account origin. This acts as a
    "defense in depth" against upstream XCM regressions, protecting the bridge's primary
    agent account (derived from the Asset Hub Root location) which holds ERC20 assets.

    The `EthereumBlobExporter` and `XcmConverter` now accept a generic
    `AllowedAliasOrigin: Contains<Location>` type parameter. Runtimes pass
    `EverythingBut<Equals<AssetHubLocation>>` to reject any `AliasOrigin` that
    matches the Asset Hub's parachain location.
crates:
- name: snowbridge-outbound-queue-primitives
  bump: major
- name: snowbridge-runtime-common
  bump: minor
- name: bridge-hub-westend-runtime
  bump: minor
- name: bridge-hub-westend-integration-tests
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/converter/tests.rs (L567-600)
```rust
#[test]
fn xcm_converter_convert_with_assethub_alias_origin_yields_invalid_origin() {
	let network = BridgedNetwork::get();

	let token_address: [u8; 20] = hex!("1000000000000000000000000000000000000000");
	let beneficiary_address: [u8; 20] = hex!("2000000000000000000000000000000000000000");

	let assets: Assets = vec![Asset {
		id: AssetId([AccountKey20 { network: None, key: token_address }].into()),
		fun: Fungible(1000),
	}]
	.into();
	let filter: AssetFilter = assets.clone().into();
	let fee_asset: Asset = Asset { id: AssetId(Here.into()), fun: Fungible(1000) }.into();

	let message: Xcm<()> = vec![
		WithdrawAsset(fee_asset.clone().into()),
		PayFees { asset: fee_asset },
		WithdrawAsset(assets.clone()),
		AliasOrigin(AssetHubLocation::get()),
		DepositAsset {
			assets: filter,
			beneficiary: AccountKey20 { network: None, key: beneficiary_address }.into(),
		},
		SetTopic([0; 32]),
	]
	.into();
	let mut converter =
		XcmConverter::<MockTokenIdConvert, (), EverythingBut<Equals<AssetHubLocation>>>::new(
			&message, network,
		);
	let result = converter.convert();
	assert_eq!(result.err(), Some(XcmConverterError::InvalidOrigin));
}
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/converter/mod.rs (L132-154)
```rust
		// Inspect `AliasOrigin` as V2 message. This exporter should only process Snowbridge V2
		// messages. We use the presence of an `AliasOrigin` instruction to distinguish between
		// Snowbridge V2 and Snowbridge V1 messages, since XCM V5 came after Snowbridge V1 and
		// so it's not supported in Snowbridge V1. Snowbridge V1 messages are processed by the
		// snowbridge-outbound-queue-primitives v1 exporter.
		let mut instructions = message.clone().0;
		let result = instructions.matcher().match_next_inst_while(
			|_| true,
			|inst| {
				return match inst {
					AliasOrigin(..) => Err(ProcessMessageError::Yield),
					_ => Ok(ControlFlow::Continue(())),
				};
			},
		);
		ensure!(result.is_err(), SendError::NotApplicable);

		let mut converter =
			XcmConverter::<ConvertAssetId, (), AllowedAliasOrigin>::new(&message, expected_network);
		let message = converter.convert().map_err(|err| {
			tracing::error!(target: TARGET, error=?err, "unroutable due to pattern matching.");
			SendError::Unroutable
		})?;
```

**File:** cumulus/parachains/runtimes/bridge-hubs/bridge-hub-westend/tests/snowbridge.rs (L195-293)
```rust
pub fn signed_assethub_user_cannot_forge_assethub_agent_origin() {
	let assethub_parachain_id = ASSET_HUB_WESTEND_PARACHAIN_ID;
	let weth_contract_address = H160::random();
	let destination_address = H160::random();
	let fee_amount = DefaultBridgeHubEthereumBaseFee::get();
	let ethereum_chain_id = 11155111;

	let collator_session_key = collator_session_keys();

	bridge_hub_test_utils::ExtBuilder::<Runtime>::default()
		.with_collators(collator_session_key.collators())
		.with_session_keys(collator_session_key.session_keys())
		.with_para_id(BRIDGE_HUB_WESTEND_PARACHAIN_ID.into())
		.with_tracing()
		.build()
		.execute_with(|| {
			<snowbridge_pallet_system::Pallet<Runtime>>::initialize(
				BRIDGE_HUB_WESTEND_PARACHAIN_ID.into(),
				assethub_parachain_id.into(),
			)
			.unwrap();

			// fund asset hub sovereign account enough so it can pay fees
			snowbridge_runtime_test_common::initial_fund::<Runtime>(
				assethub_parachain_id,
				5_000_000_000_000,
			);

			let fee_asset = Asset { id: AssetId(Here.into()), fun: Fungible(fee_amount) };

			let transfer_asset = Asset {
				id: AssetId(Location::new(
					0,
					[AccountKey20 { network: None, key: weth_contract_address.into() }],
				)),
				fun: Fungible(1000000000),
			};

			// Construct a forged V2 XCM that attempts to use AliasOrigin(AssetHubLocation)
			let forged_assethub_origin = Location::new(1, Parachain(assethub_parachain_id));
			let forged_xcm = Xcm(vec![
				WithdrawAsset(Assets::from(vec![fee_asset.clone()])),
				PayFees { asset: fee_asset },
				WithdrawAsset(Assets::from(vec![transfer_asset.clone()])),
				AliasOrigin(forged_assethub_origin),
				DepositAsset {
					assets: Wild(All),
					beneficiary: Location::new(
						0,
						[AccountKey20 { network: None, key: destination_address.into() }],
					),
				},
				SetTopic([9; 32]),
			]);

			let export_xcm = Xcm(vec![
				WithdrawAsset(Assets::from(vec![Asset {
					id: AssetId(Location::new(1, Here)),
					fun: Fungible(fee_amount),
				}])),
				BuyExecution {
					fees: Asset { id: AssetId(Location::new(1, Here)), fun: Fungible(fee_amount) },
					weight_limit: Unlimited,
				},
				ExportMessage {
					network: Ethereum { chain_id: ethereum_chain_id },
					destination: Here,
					xcm: forged_xcm,
				},
			]);

			let assethub_parachain_location = Location::new(1, Parachain(assethub_parachain_id));
			let mut hash = export_xcm.using_encoded(sp_io::hashing::blake2_256);
			let outcome = xcm_executor::XcmExecutor::<XcmConfig>::prepare_and_execute(
				assethub_parachain_location,
				export_xcm,
				&mut hash,
				RuntimeHelper::<Runtime, AllPalletsWithoutSystem>::xcm_max_weight(
					XcmReceivedFrom::Sibling,
				),
				Weight::zero(),
			);

			// Assert that the message failed to execute due to "Unroutable" error inside the
			// exporter
			assert!(matches!(
				outcome,
				Outcome::Incomplete {
					error: InstructionError { error: XcmError::Unroutable, .. },
					..
				}
			));

			// Check that no messages were queued in the outbound queue
			let committed_messages =
				snowbridge_pallet_outbound_queue_v2::Messages::<Runtime>::get();
			assert_eq!(committed_messages.len(), 0);
		});
}
```
