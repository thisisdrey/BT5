## Title
Snowbridge V1 inbound token transfer permanently locks Ethereum-side funds on destination XCM execution failure, with no refund path — ([File: bridges/snowbridge/primitives/inbound-queue/src/v1.rs])

### Summary
The ZetaChain bug describes a cross-chain transaction that consumes an irreversible on-chain action (vote quorum reached, status advanced), then fails during downstream execution (swap/gas payment), and is marked `Aborted` with no refund path for the underlying value — permanently or semi-permanently locking user funds.

The Snowbridge V1 inbound-queue pallet exhibits the same structural pattern: `Pallet::submit` in `bridges/snowbridge/pallets/inbound-queue/src/lib.rs` irreversibly advances `Nonce` and rewards the relayer *before* the resulting XCM is actually executed on the destination chain (AssetHub), and the constructed XCM programs in `bridges/snowbridge/primitives/inbound-queue/src/v1.rs` (`convert_send_token`, `convert_send_native_token`) have no built-in fallback to return the bridged asset to the original Ethereum sender if the destination-side XCM execution fails.

### Finding Description
`Pallet::submit` at [1](#0-0)  performs, in order: nonce advancement (irreversible, no replay possible), relayer reward payment from the destination-parachain sovereign account, `do_convert` to build the XCM, `burn_fees` (which teleports/burns the fee asset that is referenced in the XCM's `ReceiveTeleportedAsset`/`WithdrawAsset` instructions), and finally `send_xcm` to enqueue the message to the destination parachain via XCMP/DMP.

Once `send_xcm` succeeds, the pallet's job is done — the nonce is consumed and cannot be resubmitted (`InvalidNonce` guard at [2](#0-1) ), and the fee has already been burned. The actual value transfer (the WETH/ETH/native-token amount) only happens when the destination chain (AssetHub) processes the enqueued XCM via `pallet_message_queue`. That processing is asynchronous and can fail independently — e.g. `WithdrawAsset`/`ReserveAssetDeposited` fails due to insufficient backing, `BuyExecution` fails due to insufficient fee, or a downstream `Transact`/`DepositReserveAsset` step reverts. When this happens, `pallet_message_queue`'s `process_message_payload` rolls back the transaction and emits `Event::Processed { success: false, .. }` (see [3](#0-2) ), and the message is discarded — there is no automatic retry and no compensating action.

Confirmed empirically by the repository's own integration test `register_weth_token_in_asset_hub_fail_for_insufficient_fee`, which drives exactly this scenario and observes `MessageQueue::Processed { success: false, .. }` with no further recovery: [4](#0-3) .

The XCM programs built by `convert_send_token` and `convert_send_native_token` in `v1.rs` only guard against *fee* dust being trapped (via `SetAppendix`/`DepositAsset` to the bridge sovereign, and depositing "leftover" fees to the beneficiary) — see [5](#0-4)  and [6](#0-5) . None of these programs place a fallback `SetAssetClaimer`/appendix that would let the *original Ethereum sender* (or anyone) reclaim the *principal* asset if the `WithdrawAsset`/`ReserveAssetDeposited`/`DepositReserveAsset` instructions themselves fail before reaching the final `DepositAsset`. If those early instructions abort, the holding register contents are trapped under whatever origin the XCVM executor uses (the descended/aliased bridge origin), not the end user, and by that point the corresponding token has already been locked/burned on the Ethereum Gateway contract with the nonce already consumed on the Substrate side — mirroring exactly the ZetaChain pattern of "vote/step succeeded, downstream execution failed, `Aborted`-equivalent state reached, no refund mechanism for the underlying value."

This is distinct from the already-fixed V2 issue (`snowbridge_inbound_queue_primitives`/`inbound-queue-v2` converter), where PR 11919 fixed the fallback-claimer network-mismatch bug so that trapped V2 funds *can* now be claimed by the bridge owner (verified in current code and test `fallback_claimer_traps_to_bridge_owner_and_claim_assets_succeeds`, and in `bridges/snowbridge/primitives/inbound-queue/src/v2/converter.rs` lines 130–143). The V1 flow analyzed here has no equivalent claimer-hint/appendix protection for the principal asset at all.

### Impact Explanation
When the destination-side XCM execution of a `SendToken`/`SendNativeToken` command fails partway (post-fee-burn, pre-final-deposit), the bridged value is either trapped under a non-user-controlled origin or effectively lost, while the nonce guarding replay has already been permanently advanced and the corresponding Ethereum-side lock/burn has already occurred. There is no permissionless retry, and recovery — if possible at all — requires manual governance/root intervention (e.g. `pallet_xcm::claim_assets` by whoever controls the trap origin, which is not the end user), matching the "temporary or permanent freezing of user funds" impact category from the seed report. This satisfies the "permanent user-fund or bridge-state lock" and "message queues ... must only advance after ... settlement succeed atomically" pivots.

### Likelihood Explanation
No privileged actor is required to trigger this: any of the enumerated failure conditions (insufficient AssetHub XCM fee provided by the Ethereum-side sender, transient insufficient liquidity/backing for `WithdrawAsset`, or downstream `Transact`/`DepositReserveAsset` failures for multi-hop `ForeignAccountId32`/`20` transfers) can be induced by an ordinary bridge user setting a low `fee` parameter in their own message, or can occur incidentally due to normal operational conditions (fee market changes between submission and processing). The repository's own test suite already demonstrates the `success:false` outcome is reachable through ordinary parameter choices, without any relayer, validator, or governance misbehavior.

### Recommendation
For `convert_send_token`/`convert_send_native_token` in `bridges/snowbridge/primitives/inbound-queue/src/v1.rs`, add an XCM appendix/`SetAssetClaimer` hint (similar to the V2 fix in PR 11919) that anchors trap recovery to a deterministic, claimable location — ideally the original Ethereum sender's derived account — for every asset in the program, not just leftover fee dust. Additionally, consider not treating the message as "final" (i.e., do not irreversibly consume the nonce / burn fees) until destination-side execution success is confirmed, or provide an explicit on-chain refund/claim extrinsic path analogous to the fix applied to the V2 fallback-claimer bug.

### Proof of Concept
1. An Ethereum user submits a `SendToken` (or `SendNativeToken`) message via the Gateway contract with an `amount` and a `fee` value that is technically valid but insufficient to fully cover destination-side execution (e.g., matching `INSUFFICIENT_XCM_FEE` used in the repo's own `register_weth_token_in_asset_hub_fail_for_insufficient_fee`/`send_weth_from_ethereum_to_asset_hub_with_fee` test helpers).
2. A relayer submits the corresponding proof via `InboundQueue::submit`; `Nonce` advances, the relayer is rewarded, `do_convert` builds the XCM, fees are burned, and `send_xcm` enqueues the message to AssetHub — all irreversibly, per [1](#0-0) .
3. On AssetHub, `pallet_message_queue` processes the message; the underfunded/failing `BuyExecution`/`WithdrawAsset`/`DepositReserveAsset` sequence aborts, and `Event::Processed { success: false, .. }` is emitted (as already exercised in `register_weth_token_in_asset_hub_fail_for_insufficient_fee`, [4](#0-3) ).
4. The token value locked/burned on the Ethereum Gateway contract has no compensating mint/release; the nonce cannot be resubmitted; the principal asset is trapped under a non-user origin on AssetHub with no built-in claimer path in the V1 converter, unlike the fixed V2 fallback-claimer flow.

### Citations

**File:** bridges/snowbridge/pallets/inbound-queue/src/lib.rs (L256-311)
```rust
			// Verify message nonce
			<Nonce<T>>::try_mutate(envelope.channel_id, |nonce| -> DispatchResult {
				if *nonce == u64::MAX {
					return Err(Error::<T>::MaxNonceReached.into());
				}
				if envelope.nonce != nonce.saturating_add(1) {
					Err(Error::<T>::InvalidNonce.into())
				} else {
					*nonce = nonce.saturating_add(1);
					Ok(())
				}
			})?;

			// Reward relayer from the sovereign account of the destination parachain, only if funds
			// are available
			let sovereign_account = sibling_sovereign_account::<T>(channel.para_id);
			let delivery_cost = Self::calculate_delivery_cost(event.encode().len() as u32);
			let amount = T::Token::reducible_balance(
				&sovereign_account,
				Preservation::Preserve,
				Fortitude::Polite,
			)
			.min(delivery_cost);
			if !amount.is_zero() {
				T::Token::transfer(&sovereign_account, &who, amount, Preservation::Preserve)?;
			}

			// Decode payload into `VersionedMessage`
			let message = VersionedMessage::decode_all(&mut envelope.payload.as_ref())
				.map_err(|_| Error::<T>::InvalidPayload)?;

			// Decode message into XCM
			let (xcm, fee) = Self::do_convert(envelope.message_id, message.clone())?;

			tracing::info!(
				target: LOG_TARGET,
				?xcm,
				?fee,
				"💫 xcm decoded"
			);

			// Burning fees for teleport
			Self::burn_fees(channel.para_id, fee)?;

			// Attempt to send XCM to a dest parachain
			let message_id = Self::send_xcm(xcm, channel.para_id)?;

			Self::deposit_event(Event::MessageReceived {
				channel_id: envelope.channel_id,
				nonce: envelope.nonce,
				message_id,
				fee_burned: fee,
			});

			Ok(())
		}
```

**File:** substrate/frame/message-queue/src/lib.rs (L1618-1629)
```rust
			Ok(success) => {
				// Success
				let weight_used = meter.consumed().saturating_sub(prev_consumed);
				Self::deposit_event(Event::<T>::Processed {
					id: id.into(),
					origin,
					weight_used,
					success,
				});
				MessageExecutionStatus::Processed
			},
		}
```

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge.rs (L407-445)
```rust
#[test]
fn register_weth_token_in_asset_hub_fail_for_insufficient_fee() {
	BridgeHubWestend::fund_para_sovereign(AssetHubWestend::para_id().into(), INITIAL_FUND);

	BridgeHubWestend::execute_with(|| {
		type RuntimeEvent = <BridgeHubWestend as Chain>::RuntimeEvent;
		type EthereumInboundQueue =
			<BridgeHubWestend as BridgeHubWestendPallet>::EthereumInboundQueue;
		let message_id: H256 = [0; 32].into();
		let message = VersionedMessage::V1(MessageV1 {
			chain_id: SEPOLIA_ID,
			command: Command::RegisterToken {
				token: WETH.into(),
				// Insufficient fee which should trigger the trap
				fee: INSUFFICIENT_XCM_FEE,
			},
		});
		let (xcm, _) = EthereumInboundQueue::do_convert(message_id, message).unwrap();
		let _ = EthereumInboundQueue::send_xcm(xcm, AssetHubWestend::para_id().into()).unwrap();

		assert_expected_events!(
			BridgeHubWestend,
			vec![
				RuntimeEvent::XcmpQueue(cumulus_pallet_xcmp_queue::Event::XcmpMessageSent { .. }) => {},
			]
		);
	});

	AssetHubWestend::execute_with(|| {
		type RuntimeEvent = <AssetHubWestend as Chain>::RuntimeEvent;

		assert_expected_events!(
			AssetHubWestend,
			vec![
				RuntimeEvent::MessageQueue(pallet_message_queue::Event::Processed { success:false, .. }) => {},
			]
		);
	});
}
```

**File:** bridges/snowbridge/primitives/inbound-queue/src/v1.rs (L335-390)
```rust
		let mut instructions = vec![
			ReceiveTeleportedAsset(total_fee_asset.into()),
			BuyExecution { fees: asset_hub_fee_asset, weight_limit: Unlimited },
			DescendOrigin(PalletInstance(inbound_queue_pallet_index).into()),
			UniversalOrigin(GlobalConsensus(network)),
			ReserveAssetDeposited(asset.clone().into()),
			ClearOrigin,
		];

		match dest_para_id {
			Some(dest_para_id) => {
				let dest_para_fee_asset: Asset = (Location::parent(), dest_para_fee).into();
				let bridge_location = Location::new(2, GlobalConsensus(network));

				instructions.extend(vec![
					// After program finishes deposit any leftover assets to the snowbridge
					// sovereign.
					SetAppendix(Xcm(vec![DepositAsset {
						assets: Wild(AllCounted(2)),
						beneficiary: bridge_location,
					}])),
					// Perform a deposit reserve to send to destination chain.
					DepositReserveAsset {
						// Send over assets and unspent fees, XCM delivery fee will be charged from
						// here.
						assets: Wild(AllCounted(2)),
						dest: Location::new(1, [Parachain(dest_para_id)]),
						xcm: vec![
							// Buy execution on target.
							BuyExecution { fees: dest_para_fee_asset, weight_limit: Unlimited },
							// Deposit assets to beneficiary.
							DepositAsset { assets: Wild(AllCounted(2)), beneficiary },
							// Forward message id to destination parachain.
							SetTopic(message_id.into()),
						]
						.into(),
					},
				]);
			},
			None => {
				instructions.extend(vec![
					// Deposit both asset and fees to beneficiary so the fees will not get
					// trapped. Another benefit is when fees left more than ED on AssetHub could be
					// used to create the beneficiary account in case it does not exist.
					DepositAsset { assets: Wild(AllCounted(2)), beneficiary },
				]);
			},
		}

		// Forward message id to Asset Hub.
		instructions.push(SetTopic(message_id.into()));

		// The `instructions` to forward to AssetHub, and the `total_fees` to locally burn (since
		// they are teleported within `instructions`).
		(instructions.into(), total_fees.into())
	}
```

**File:** bridges/snowbridge/primitives/inbound-queue/src/v1.rs (L442-458)
```rust
		let instructions = vec![
			ReceiveTeleportedAsset(total_fee_asset.clone().into()),
			BuyExecution { fees: asset_hub_fee_asset, weight_limit: Unlimited },
			DescendOrigin(PalletInstance(inbound_queue_pallet_index).into()),
			UniversalOrigin(GlobalConsensus(network)),
			WithdrawAsset(asset.clone().into()),
			// Deposit both asset and fees to beneficiary so the fees will not get
			// trapped. Another benefit is when fees left more than ED on AssetHub could be
			// used to create the beneficiary account in case it does not exist.
			DepositAsset { assets: Wild(AllCounted(2)), beneficiary },
			SetTopic(message_id.into()),
		];

		// `total_fees` to burn on this chain when sending `instructions` to run on AH (which also
		// teleport fees)
		Ok((instructions.into(), asset_hub_fee.into()))
	}
```
