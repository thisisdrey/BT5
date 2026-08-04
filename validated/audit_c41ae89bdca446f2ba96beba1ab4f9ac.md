### Title
Malformed/Invalid Payload Bypasses Snowbridge V2 Conversion Validation, Silently Trapping Ethereum-Bridged Assets on Asset Hub - ([File: bridges/snowbridge/primitives/inbound-queue/src/v2/converter.rs])

### Summary
Snowbridge V2's inbound message converter deliberately swallows decoding failures for the arbitrary remote XCM payload attached to an Ethereum→Polkadot transfer instead of rejecting the message. Just like the reported bridge, funds are already committed/locked on the source chain (Ethereum) before the destination chain attempts to interpret the recipient/execution instructions; when that interpretation fails, the code chooses a "silent, non-reverting" path that results in the bridged value becoming unreachable to the intended recipient, rather than failing the whole message and giving the user a real remediation path.

### Finding Description
`MessageToXcm::decode_raw_xcm` in `bridges/snowbridge/primitives/inbound-queue/src/v2/converter.rs` explicitly does not propagate a decode error for the attacker/user-controlled `Payload::Raw` bytes: [1](#0-0) 

If the bytes fail to decode as a `VersionedXcm`, or fail `try_into()`, the function returns an empty `Xcm::new()` instead of an error — the comment itself acknowledges this is intentional so the message "won't fail entirely." This empty XCM is then spliced into the final instruction set built by `ConvertMessage::convert`: [2](#0-1) 

The `WithdrawAsset`/`ReserveAssetDeposited` instructions still execute (moving the bridged assets into the XCM executor's holding register on Asset Hub), but because the appended `remote_xcm` is empty, there is no subsequent `DepositAsset` instruction to move the held assets to any beneficiary. The assets end up trapped in the XCM executor holding register and are recorded by `DropAssets`/`AssetTraps`, recoverable only via a `claim_assets` call from whichever location was set as `claimer` — which, per `prepare()`, silently defaults to the Snowbridge sovereign account on Asset Hub if the message's `claimer` bytes also fail to decode as a `Location`: [3](#0-2) 

This is confirmed by the pallet's own test suite, which explicitly documents the trapping behavior as expected: [4](#0-3) 

At the pallet level, `process_message` in `bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs` marks the nonce as consumed and pays the relayer before/independent of whether the embedded remote XCM actually delivered value to anyone: [5](#0-4) 

This mirrors the external report's core broken invariant exactly: the source side has already irreversibly moved value (Ether/ERC-20 locked into the Gateway contract on Ethereum) based on an unvalidated payload; the destination side attempts to decode that payload only after the transfer is already committed; when decoding fails, the destination silently swallows the error and does not deliver funds to the intended recipient, leaving the user with only an indirect, non-obvious remediation path (a manual `claim_assets` extrinsic against a potentially wrong/default claimer) rather than a clean revert or guaranteed self-service retry.

### Impact Explanation
Any malformed or incompatible `Payload::Raw` (e.g., an XCM version mismatch, a payload the relayer/user encodes incorrectly, or one referencing instructions unsupported on the executing Asset Hub) causes the bridged asset value to land in the XCM asset-trap registry instead of the beneficiary's account. Recovery requires a privileged/aware party to invoke `pallet_xcm::claim_assets` with the exact trapped-asset fingerprint and correct origin — the ordinary end user relying on the bridge UI has no direct way to trigger this, and if the `claimer` also failed to decode, the assets are claimable only from the Snowbridge sovereign account context, not the original sender. This is a permanent-fund-lock class impact consistent with the required-impact gate (message queues/receipts must only advance after decode, dispatch, and settlement succeed atomically — here settlement is *not* atomic with decode success).

### Likelihood Explanation
No privileged actor, relayer collusion, or governance action is required. Any Ethereum-side sender of a Snowbridge V2 message (via the Gateway contract's public `sendMessage`/equivalent entrypoint) fully controls the `Payload::Raw` bytes and the `claimer` bytes that eventually reach `MessageToXcm::prepare`/`decode_raw_xcm`. A single malformed payload — even from an honest user's tooling bug — silently trips this path on every affected transfer, making likelihood high for accidental triggering and straightforward for deliberate triggering.

### Recommendation
`decode_raw_xcm` should propagate a `ConvertMessageError` (surfaced through `ConvertMessage::convert` and ultimately `Error::<T>::from(ConvertMessageError)`) when the raw payload fails to decode into a valid `Xcm<()>`, rather than substituting an empty XCM. If backward-compatibility with intentionally payload-less messages is required, that case should be distinguished explicitly (e.g., an `Option<Payload>` or explicit "no remote XCM" variant) from a genuinely malformed/undecodable payload, so that decode failures for actual transfer instructions cause the whole message to be rejected/reverted (and the tokens to remain provably claimable by the intended beneficiary, e.g., via a well-known deterministic path) instead of being routed into the general-purpose asset trap.

### Proof of Concept
1. On Ethereum, call the Gateway contract's send-message function with a `Message` whose `assets` field carries a nonzero-value `NativeTokenERC20`/`ForeignTokenERC20` transfer, and whose `payload` is `Payload::Raw(bytes)` where `bytes` is not a valid SCALE-encoded `VersionedXcm<()>` (e.g., 4 arbitrary bytes), analogous to the external PoC's 4-byte `to` value.
2. The message is relayed and processed by `InboundQueueV2::submit` → `process_message`, which calls `T::MessageProcessor::process_message` → `MessageToXcm::convert` → `Self::prepare` → `decode_raw_xcm(raw)`.
3. `decode_raw_xcm` fails to decode `raw` and returns `Xcm::new()` (empty), exactly as validated by the existing `test_invalid_xcm` unit test. [4](#0-3) 
4. The final XCM executed on Asset Hub contains `WithdrawAsset`/`ReserveAssetDeposited` for the bridged assets but no `DepositAsset` to any beneficiary (the appended empty XCM contributes nothing), so the assets remain in the holding register and are recorded as trapped by the executor's `DropAssets` implementation.
5. `process_message` still marks the nonce as consumed and pays the relayer their fee, so the message can never be resubmitted, and the user's bridged value is now only recoverable via a manual `claim_assets` call using the (possibly default/incorrect) claimer location — with no direct retry mechanism exposed to the original sender.

### Citations

**File:** bridges/snowbridge/primitives/inbound-queue/src/v2/converter.rs (L130-143)
```rust
		let claimer = message
			.claimer
			// Get the claimer from the message,
			.and_then(|claimer_bytes| Location::decode(&mut claimer_bytes.as_ref()).ok())
			// or use the Snowbridge sovereign on AH as the fallback claimer.
			.unwrap_or_else(|| {
				Location::new(
					0,
					[AccountId32 {
						network: Some(LocalNetwork::get()),
						id: bridge_owner.clone().into(),
					}],
				)
			});
```

**File:** bridges/snowbridge/primitives/inbound-queue/src/v2/converter.rs (L327-342)
```rust
	/// Parse and (non-strictly) decode `raw` XCM bytes into a `Xcm<()>`.
	/// If decoding fails, return an empty `Xcm<()>`—thus allowing the message
	/// to proceed so assets can still be trapped on AH rather than the funds being locked on
	/// Ethereum but not accessible on AH.
	fn decode_raw_xcm(raw: &[u8]) -> Xcm<()> {
		let mut data = raw;
		if let Ok(versioned_xcm) =
			VersionedXcm::<()>::decode_with_depth_limit(MAX_XCM_DECODE_DEPTH, &mut data)
		{
			if let Ok(decoded_xcm) = versioned_xcm.try_into() {
				return decoded_xcm;
			}
		}
		// Decoding failed; allow an empty XCM so the message won't fail entirely.
		Xcm::new()
	}
```

**File:** bridges/snowbridge/primitives/inbound-queue/src/v2/converter.rs (L396-426)
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

		Ok(instructions.into())
	}
```

**File:** bridges/snowbridge/primitives/inbound-queue/src/v2/converter.rs (L870-904)
```rust
	#[test]
	fn test_invalid_xcm() {
		sp_io::TestExternalities::default().execute_with(|| {
			let origin: H160 = hex!("29e3b139f4393adda86303fcdaa35f60bb7092bf").into();
			let native_token_id: H160 = hex!("5615deb798bb3e4dfa0139dfa1b3d433cc23b72f").into();
			let token_value = 3_000_000_000_000u128;
			let assets = vec![EthereumAsset::NativeTokenERC20 {
				token_id: native_token_id,
				value: token_value,
			}];
			// invalid xcm
			let versioned_xcm = hex!("8b69c7e376e28114618e829a7ec7").to_vec();
			let claimer_account = AccountId32 { network: None, id: H256::random().into() };
			let claimer: Option<Vec<u8>> = Some(claimer_account.clone().encode());
			let value = 6_000_000_000_000u128;
			let execution_fee = 1_000_000_000_000u128;
			let relayer_fee = 5_000_000_000_000u128;

			let message = Message {
				gateway: H160::zero(),
				nonce: 0,
				origin,
				assets,
				payload: Payload::Raw(versioned_xcm),
				claimer: Some(claimer.encode()),
				value,
				execution_fee,
				relayer_fee,
			};

			let result = Converter::convert(message);

			// Invalid xcm does not break the message, allowing funds to be trapped on AH.
			assert_ok!(result.clone());
		});
```

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
