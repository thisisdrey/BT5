Based on the evidence gathered, I found a genuine local analog to the SKALE "send-then-fail, funds frozen" bug class, but I want to be transparent about the limits of what I could verify given the remaining tool budget.

### Title
Inbound Snowbridge V2 messages irrevocably consume nonce and pay relayer reward before AssetHub-side settlement succeeds, leaving user funds recoverable only through a fragile trap-claimer match - (File: `bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs`)

### Summary
`Pallet::process_message` in the Snowbridge V2 inbound queue commits the message as processed (`Nonce::<T>::set(nonce)`) and, once the XCM is forwarded, pays the relayer their `relayer_fee`/tip — all before it is known whether the receiving chain (AssetHub, or any chain the message is further routed to) actually completes the intended asset creation/deposit. This mirrors the SKALE `TokenManagerERC20` bug: the origin side treats the transfer as final and irreversible while the destination side can still reject it (e.g. asset not yet created), and the only recovery path for the user's funds is the XCM trap/claim mechanism, which has already been shown in this codebase to be fragile.

### Finding Description
`process_message` marks the nonce consumed and converts/dispatches the XCM unconditionally: [1](#0-0) 

The relayer reward is paid out regardless of what ultimately happens to the message on the destination chain, since XCM dispatch to AssetHub is asynchronous (delivered via `Nonce`/queue, processed later by `pallet_message_queue`).

The emulated test `register_and_send_token_in_one_transaction_fails` demonstrates the destination-side failure mode directly: the inbound message tries to `create_asset` + `set_reserves` + deposit in one XCM, but if any step fails the whole program aborts and `pallet_message_queue` reports `Processed { success: false, .. }`, even though on the BridgeHub side the nonce was already consumed and the relayer was already rewarded: [2](#0-1) 

When an XCM aborts mid-execution, any assets already withdrawn into the holding register are trapped via `AssetsTrapped`, and are only recoverable if a signed account can produce a `Location` that exactly matches the trap-key that was computed for the fallback/claimer origin. This exact mechanism was shown to be broken in this repository: prior to `PR 11919`, the fallback claimer used `network: None`, which did not match what `SignedToAccountId32` produces on AssetHub, making default-claimer trapped funds "effectively unrecoverable without a runtime upgrade": [3](#0-2) 

The regression tests added alongside that fix (`fallback_claimer_traps_to_bridge_owner_and_claim_assets_succeeds`, `user_supplied_claimer_can_claim_trapped_assets`, `fallback_claimer_trap_cannot_be_claimed_by_other_account`) confirm the trap/claim path is the *only* recovery mechanism for funds when destination-side processing fails: [4](#0-3) 

This is architecturally the same broken invariant as the SKALE report: the origin side (Ethereum Gateway lock + BridgeHub nonce consumption + relayer reward payment) commits irreversibly before the destination side (AssetHub asset creation/deposit) is guaranteed to succeed, and there is no atomic all-or-nothing commit across the two legs — only a best-effort trap/claim fallback whose correctness depends on exact `Location` matching between the message converter and whatever `SignedOrigin` converter is configured on the destination chain.

### Impact Explanation
If the trap-claimer location ever fails to match the destination chain's origin-converter output (as it already did once, per `PR 11919`), or if a message is forwarded through an additional hop (e.g. AssetHub → Penpal, as exercised by `send_token_to_penpal_v2`) where the claimer's network/location assumptions don't hold, user funds become permanently locked/trapped with no way to reconstruct the correct claim `Location`. This is a direct fund-freezing bug in the "Snowbridge delivery flow" bucket called out in the required-impacts list, since the nonce/nonce-consumption + relayer-reward payment happen unconditionally regardless of final settlement success.

### Likelihood Explanation
This is not a hypothetical: the exact class of bug (claimer/trap location mismatch causing unrecoverable trapped funds) was found and fixed once already in this codebase (`PR 11919`), and it required a source-code fix, not user/relayer/admin action, to resolve. This confirms the underlying design pattern — settle-then-maybe-fail with trap/claim as the sole recovery path — is inherently fragile and has already produced one confirmed instance of frozen funds. I could not fully verify (due to tool-call exhaustion) whether additional un-covered paths (particularly multi-hop forwarding to third-party parachains like Penpal) still have unmatched claimer locations, so I cannot assert a currently-unpatched instance with full certainty.

### Recommendation
- Do not treat inbound message nonce-consumption + relayer reward payment as final until the destination-side dispatch is confirmed successful (or explicitly design an idempotent retry path keyed by nonce that permits reprocessing/re-claiming without an on-chain governance/runtime upgrade).
- Audit every hop through which a Snowbridge V2 message can be forwarded (not just the immediate AssetHub hop covered by `PR 11919`) to ensure the claimer `Location` supplied always matches the `SignedToAccountId32`/origin-converter output of whichever chain ultimately executes the XCM and traps assets.
- Add exhaustive integration tests for multi-hop failure scenarios (AssetHub → Penpal → failure) verifying that `AssetsTrapped`/`claim_assets` round-trips succeed for every supported destination and claimer configuration.

### Proof of Concept
Not independently reproduced beyond what the existing test suite already demonstrates. The evidentiary basis is:
1. `register_and_send_token_in_one_transaction_fails` — shows nonce/reward already finalized on BridgeHub while AssetHub-side message fails outright (`Processed{success:false}`). [5](#0-4) 
2. `prdoc/stable2603-3/pr_11919.prdoc` — documents a confirmed real instance where trapped funds from this exact flow were "effectively unrecoverable without a runtime upgrade" prior to the fix. [3](#0-2) 

Note: I was unable to complete verification of whether multi-hop forwarding (AssetHub → Penpal) paths have equivalent claimer-matching coverage, due to reaching the tool-call limit before reading the relevant `send_token_to_penpal_v2` failure-path test in full. This should be independently verified before treating the multi-hop variant as confirmed.

### Citations

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs (L214-245)
```rust
	impl<T: Config> Pallet<T> {
		pub fn process_message(relayer: T::AccountId, message: Message) -> DispatchResult {
			// Verify that the message was submitted from the known Gateway contract
			ensure!(T::GatewayAddress::get() == message.gateway, Error::<T>::InvalidGateway);

			let (nonce, relayer_fee) = (message.nonce, message.relayer_fee);

			// Verify the message has not been processed
			ensure!(!Nonce::<T>::get(nonce), Error::<T>::InvalidNonce);

			// Mark message as received
			Nonce::<T>::set(nonce);

			let message_id = T::MessageProcessor::process_message(relayer.clone(), message)
				.map_err(|e| match e {
					MessageProcessorError::ProcessMessage(e) => e,
					MessageProcessorError::ConvertMessage(e) => Error::<T>::from(e).into(),
					MessageProcessorError::SendMessage(e) => Error::<T>::from(e).into(),
				})?;

			// Pay relayer reward
			let tip = Tips::<T>::take(nonce).unwrap_or_default();
			let total_tip = relayer_fee.saturating_add(tip);
			if total_tip > 0 {
				T::RewardPayment::register_reward(&relayer, T::DefaultRewardKind::get(), total_tip);
			}

			// Emit event with the message_id
			Self::deposit_event(Event::MessageReceived { nonce, message_id });

			Ok(())
		}
```

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_inbound.rs (L370-497)
```rust
#[test]
fn register_and_send_token_in_one_transaction_fails() {
	let relayer_account = BridgeHubWestendSender::get();
	let relayer_reward = 1_500_000_000_000u128;

	let token: H160 = TOKEN_ID.into();
	let token_location = erc20_token_location(token);

	let bridge_owner = snowbridge_sovereign();

	let beneficiary_acc_id: H256 = H256::random();
	let beneficiary_acc_bytes: [u8; 32] = beneficiary_acc_id.into();
	let beneficiary =
		Location::new(0, AccountId32 { network: None, id: beneficiary_acc_id.into() });

	// To satisfy ED
	AssetHubWestend::fund_accounts(vec![(
		sp_runtime::AccountId32::from(beneficiary_acc_bytes),
		3_000_000_000_000,
	)]);

	let claimer_acc_id = H256::random();
	let claimer = Location::new(0, AccountId32 { network: None, id: claimer_acc_id.into() });
	let claimer_bytes = claimer.encode();

	set_up_eth_and_dot_pool();

	let token_transfer_value = 2_000_000_000_000u128;

	let dot_asset = Location::new(1, Here);
	let dot_fee: xcm::prelude::Asset =
		(dot_asset, bp_asset_hub_westend::CreateForeignAssetDeposit::get()).into();

	// Used to pay the asset creation deposit.
	let eth_asset_value = 9_000_000_000_000u128;
	let asset_deposit: xcm::prelude::Asset = (eth_location(), eth_asset_value).into();

	let assets = vec![
		NativeTokenERC20 { token_id: WETH.into(), value: 2_800_000_000_000u128 },
		NativeTokenERC20 { token_id: token.into(), value: token_transfer_value },
	];

	BridgeHubWestend::execute_with(|| {
		type RuntimeEvent = <BridgeHubWestend as Chain>::RuntimeEvent;
		let instructions = vec![
			ExchangeAsset {
				give: asset_deposit.clone().into(),
				want: dot_fee.clone().into(),
				maximal: false,
			},
			DepositAsset { assets: dot_fee.into(), beneficiary: bridge_owner.clone().into() },
			// register new token
			Transact {
				origin_kind: OriginKind::Xcm,
				fallback_max_weight: None,
				call: (
					CreateAssetCallIndex::get(),
					token_location.clone(),
					MultiAddress::<[u8; 32], ()>::Id(bridge_owner.clone().into()),
					1u128,
				)
					.encode()
					.into(),
			},
			// set Ethereum as the asset's reserve.
			Transact {
				origin_kind: OriginKind::Xcm,
				fallback_max_weight: None,
				call: (
					SetReservesCallIndex::get(),
					token_location.clone(),
					vec![ForeignAssetReserveData { reserve: ethereum(), teleportable: false }],
				)
					.encode()
					.into(),
			},
			ExpectTransactStatus(MaybeErrorCode::Success),
			RefundSurplus,
			// try to deposit new token, weth and leftover ether fees to beneficiary.
			DepositAsset { assets: Wild(AllCounted(3)), beneficiary: beneficiary.clone() },
		];
		let xcm: Xcm<()> = instructions.into();
		let versioned_message_xcm = VersionedXcm::V5(xcm);
		let origin = EthereumGatewayAddress::get();

		let message = Message {
			gateway: origin,
			nonce: 1,
			origin,
			assets,
			payload: Payload::Raw(versioned_message_xcm.encode()),
			claimer: Some(claimer_bytes),
			value: 3_500_000_000_000u128,
			execution_fee: 1_500_000_000_000u128,
			relayer_fee: relayer_reward,
		};

		EthereumInboundQueueV2::process_message(relayer_account.clone(), message).unwrap();

		assert_expected_events!(
			BridgeHubWestend,
			vec![
				RuntimeEvent::XcmpQueue(cumulus_pallet_xcmp_queue::Event::XcmpMessageSent { .. }) => {},
				// Check that the relayer reward was registered.
				RuntimeEvent::BridgeRelayers(pallet_bridge_relayers::Event::RewardRegistered { relayer, reward_kind, reward_balance }) => {
					relayer: *relayer == relayer_account,
					reward_kind: *reward_kind == BridgeReward::Snowbridge,
					reward_balance: *reward_balance == relayer_reward,
				},
			]
		);
	});

	AssetHubWestend::execute_with(|| {
		type RuntimeEvent = <AssetHubWestend as Chain>::RuntimeEvent;

		assert_expected_events!(
			AssetHubWestend,
			vec![
				// message should not be processed, since assets cannot be ReserveAssetDeposited
				// before the asset has been created.
				RuntimeEvent::MessageQueue(
					pallet_message_queue::Event::Processed { success: false, .. }
				) => {},
			]
		);
	});
}
```

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_inbound.rs (L1023-1156)
```rust
/// When an inbound message arrives without a (valid) claimer, the converter falls back
/// to the bridge owner sovereign account, anchored on the local network. This test
/// drives the full claim flow: invalid XCM payload causes the holding register to be
/// trapped against that fallback location on Asset Hub, and the bridge owner signed
/// origin then claims the trapped assets via `pallet_xcm::claim_assets`.
///
/// Before the fix, the fallback claimer used `network: None`, so the trap origin did
/// not match the location produced by Asset Hub's `SignedToAccountId32` converter
/// (which always tags the local network), and the claim would fail with `UnknownClaim`.
#[test]
fn fallback_claimer_traps_to_bridge_owner_and_claim_assets_succeeds() {
	use asset_hub_westend_runtime::xcm_config::RelayNetwork as AssetHubRelayNetwork;

	let relayer_account = BridgeHubWestendSender::get();
	let relayer_reward = 1_500_000_000_000u128;
	let bridge_owner = snowbridge_sovereign();

	// Fund the bridge owner so it has the existential deposit when claim_assets executes
	// (the claim deposits the assets back to the same account).
	AssetHubWestend::fund_accounts(vec![(bridge_owner.clone(), INITIAL_FUND)]);

	let value: u128 = 1_500_000_000_000;
	let execution_fee: u128 = 1_500_000_000_000;
	let weth_value: u128 = 2_800_000_000_000;

	let assets = vec![NativeTokenERC20 { token_id: WETH.into(), value: weth_value }];

	set_up_eth_and_dot_pool();

	BridgeHubWestend::execute_with(|| {
		type RuntimeEvent = <BridgeHubWestend as Chain>::RuntimeEvent;

		// Invalid XCM bytes — converter decodes this as an empty Xcm, leaving the
		// non-fee assets in holding to be trapped on AH.
		let invalid_xcm_payload = hex!("02806c072d50e2c7cd6821d1f084cbb4").to_vec();

		let message = Message {
			gateway: EthereumGatewayAddress::get(),
			nonce: 1,
			origin: EthereumGatewayAddress::get(),
			assets,
			payload: Payload::Raw(invalid_xcm_payload),
			// No claimer — exercises the bridge_owner fallback.
			claimer: None,
			value,
			execution_fee,
			relayer_fee: relayer_reward,
		};

		EthereumInboundQueueV2::process_message(relayer_account.clone(), message).unwrap();

		assert_expected_events!(
			BridgeHubWestend,
			vec![
				RuntimeEvent::XcmpQueue(cumulus_pallet_xcmp_queue::Event::XcmpMessageSent { .. }) => {},
				RuntimeEvent::BridgeRelayers(pallet_bridge_relayers::Event::RewardRegistered { relayer, reward_kind, reward_balance }) => {
					relayer: *relayer == relayer_account,
					reward_kind: *reward_kind == BridgeReward::Snowbridge,
					reward_balance: *reward_balance == relayer_reward,
				},
			]
		);
	});

	// Expected fallback claimer location: bridge owner anchored on the local network.
	let expected_claimer_location = Location::new(
		0,
		[AccountId32Junction {
			network: Some(AssetHubRelayNetwork::get().expect("RelayNetwork is Some")),
			id: bridge_owner.clone().into(),
		}],
	);

	let trapped_assets = AssetHubWestend::execute_with(|| {
		type RuntimeEvent = <AssetHubWestend as Chain>::RuntimeEvent;

		let events = AssetHubWestend::events();

		// Find the AssetsTrapped event and capture its origin and assets.
		let trap = events.iter().find_map(|event| match event {
			RuntimeEvent::PolkadotXcm(pallet_xcm::Event::AssetsTrapped {
				origin, assets, ..
			}) => Some((origin.clone(), assets.clone())),
			_ => None,
		});

		let (trap_origin, trap_assets) =
			trap.expect("assets should be trapped when XCM payload is invalid");

		// The crux of the fix: the trap origin is the bridge owner pinned to the local
		// network, not `network: None`.
		assert_eq!(
			trap_origin, expected_claimer_location,
			"trap origin must match the bridge_owner location anchored on the local network"
		);

		trap_assets
	});

	// Now drive the claim flow: the bridge owner signs claim_assets, the origin is
	// converted to the same location shape that the assets were trapped against, and
	// the claim succeeds. Before the fix the network mismatch made this impossible.
	AssetHubWestend::execute_with(|| {
		type RuntimeEvent = <AssetHubWestend as Chain>::RuntimeEvent;
		type RuntimeOrigin = <AssetHubWestend as Chain>::RuntimeOrigin;

		assert_ok!(<AssetHubWestend as AssetHubWestendPallet>::PolkadotXcm::claim_assets(
			RuntimeOrigin::signed(bridge_owner.clone()),
			bx!(trapped_assets.clone()),
			bx!(VersionedLocation::from(expected_claimer_location.clone())),
		));

		assert_expected_events!(
			AssetHubWestend,
			vec![
				RuntimeEvent::PolkadotXcm(pallet_xcm::Event::AssetsClaimed { origin, assets, .. }) => {
					origin: *origin == expected_claimer_location,
					assets: *assets == trapped_assets,
				},
			]
		);

		// The remaining ether and weth should be deposited into the bridge owner's
		// foreign-asset balances on AH.
		assert!(
			ForeignAssets::balance(eth_location(), bridge_owner.clone()) >= value,
			"bridge owner should hold at least `value` of ether after claim",
		);
		assert!(
			ForeignAssets::balance(weth_location(), bridge_owner.clone()) >= weth_value,
			"bridge owner should hold at least `weth_value` of weth after claim",
		);
	});
}
```

**File:** prdoc/stable2603-3/pr_11919.prdoc (L1-19)
```text
title: 'Snowbridge: Set default asset claimer to local network'
doc:
- audience: Runtime Dev
  description: |-
    The inbound-queue v2 message converter falls back to the Snowbridge sovereign
    account on AssetHub as the asset claimer when no explicit claimer is supplied.
    Previously this fallback used `AccountId32 { network: None, .. }`, which did
    not match the location AssetHub's signed-origin converter produces (it sets
    `network: Some(LocalNetwork)`). The trap-key hash stored on `AssetsTrapped`
    therefore could not be matched by a signed `polkadotXcm.claim_assets` call,
    making default-claimer trapped funds effectively unrecoverable without a
    runtime upgrade.

    This PR sets `network: Some(LocalNetwork::get())` on the fallback claimer so
    its `Location` agrees with what `SignedToAccountId32<_, _, LocalNetwork>`
    yields on AssetHub, and adds a test covering the no-claimer-supplied path.
crates:
- name: snowbridge-inbound-queue-primitives
  bump: patch
```
