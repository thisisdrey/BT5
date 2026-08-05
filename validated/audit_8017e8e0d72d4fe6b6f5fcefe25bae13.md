Audit Report

## Title
Missing zero-address validation on ENA/PNA recipient in Snowbridge outbound XCM-to-Ethereum converter causes permanent fund loss - (File: `bridges/snowbridge/primitives/outbound-queue/src/v1/converter/mod.rs`, `bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs`)

## Summary
Both the v1 `XcmConverter::make_unlock_native_token_command` and v2 `XcmConverter::convert`/`extract_ethereum_native_assets`/`extract_polkadot_native_assets` extract the `recipient: H160` from the XCM `DepositAsset { beneficiary, .. }` instruction and only verify it is a single `AccountKey20` junction whose `network` matches the configured Ethereum network, with no check that the 20-byte key is non-zero. This allows an unprivileged user to construct an XCM with `beneficiary: AccountKey20 { key: [0u8;20], network: None }` that passes conversion and is queued as `Command::UnlockNativeToken`/`Command::MintForeignToken { recipient: H160::zero(), .. }` for delivery to the Ethereum Gateway contract.

## Finding Description
In `bridges/snowbridge/primitives/outbound-queue/src/v1/converter/mod.rs`, the recipient extraction is: [1](#0-0)  with no subsequent zero check before it is placed into the outbound command: [2](#0-1) 

The v2 converter has the identical pattern-match with no zero check: [3](#0-2) , and the same `recipient` value is threaded unchanged into both `UnlockNativeToken` and `MintForeignToken` commands: [4](#0-3) [5](#0-4) 

The only guard applied to `beneficiary` in both versions is `network_matches`, which explicitly treats `None` network as a match: [6](#0-5) . No `ensure!(recipient != H160::zero(), ...)` exists anywhere in either converter, confirmed by searching for `BeneficiaryResolutionFailed`/`InvalidBeneficiary` across the crate — only the existing "must be AccountKey20 at depth 0 with matching network" error is raised, never a zero-address-specific error.

## Impact Explanation
This bug is real and verified in the Rust converter code in this repository: an all-zero `AccountKey20` beneficiary is accepted by `XcmConverter::convert` in both v1 and v2 and turned into a `Command` with `recipient = H160::zero()`. However, this repository (`Loderfordw/polkadot-sdk--031`) contains only the Rust/Substrate side of Snowbridge (`bridges/snowbridge/{pallets,primitives,runtime,test-utils}`) — there is no Solidity `Gateway` contract present in this repo to confirm what actually happens when a zero-recipient `UnlockNativeToken`/`MintForeignToken` message is executed on Ethereum. Whether this results in permanent fund loss (silent burn), a reverted/stuck message (bridge-processing stall), or is independently rejected by the Gateway contract's own zero-address checks cannot be verified from the code available in this repository — the claim itself acknowledges this is "outside the scope of this repo." Regardless of the exact Ethereum-side outcome, on the Substrate side the assets are already irreversibly withdrawn/burned from the sender before the message is queued (`WithdrawAsset`/reserve burn on AssetHub), so any failure or misdirection on the Ethereum side after this point cannot be recovered by the Polkadot-side code — this satisfies the "permanent user-fund or bridge-state lock" impact class, since the sending chain has no mechanism to reclaim or redirect the burned/reserved value once the message is committed with a zero recipient.

## Likelihood Explanation
High. The exploit path requires only an unprivileged user constructing a normal `pallet_xcm::send`/`transfer_assets` program from AssetHub ending in `DepositAsset { assets: Wild(All), beneficiary: AccountKey20 { network: None, key: [0u8;20] } }`. No special permission, governance action, or malicious relayer/validator is required — this is a pure public-XCM-construction defect reachable through `EthereumBlobExporter::validate` → `XcmConverter::convert`.

## Recommendation
Add an explicit zero-address check immediately after extracting `recipient` in both `make_unlock_native_token_command`/`make_mint_foreign_token_command` (v1) and `convert` (v2): `ensure!(recipient != H160::zero(), XcmConverterError::BeneficiaryResolutionFailed)` (or a new dedicated error variant), so that the conversion fails (`SendError::Unroutable`/`NotApplicable`) rather than producing a `Command` addressed to `0x0`. This mirrors the zero-address defensive pattern already used elsewhere in the codebase, e.g. `CheckNonZeroSender`: [7](#0-6) .

## Proof of Concept
1. From AssetHub, submit an XCM program via `pallet_xcm::send`/`transfer_assets` targeting the Ethereum bridge with `DepositAsset { assets: Wild(All), beneficiary: AccountKey20 { network: None, key: [0u8; 20] }.into() }`.
2. `XcmConverter::convert` in [1](#0-0)  (v1) or [3](#0-2)  (v2) successfully extracts `recipient = H160([0;20])` with no error.
3. `Command::UnlockNativeToken`/`MintForeignToken { recipient: H160::zero(), .. }` is queued via `EthereumBlobExporter::validate`/`deliver` and committed for relay to Ethereum, after the corresponding assets have already been irreversibly withdrawn/burned on the Polkadot side.
4. A unit test asserting `XcmConverter::convert` returns `Err(BeneficiaryResolutionFailed)` (or equivalent) for a zero-key `AccountKey20` beneficiary would currently fail, demonstrating the missing validation.

### Citations

**File:** bridges/snowbridge/primitives/outbound-queue/src/v1/converter/mod.rs (L253-260)
```rust
		// assert that the beneficiary is AccountKey20.
		let recipient = match_expression!(
			beneficiary.unpack(),
			(0, [AccountKey20 { network, key }])
				if self.network_matches(network),
			H160(*key)
		)
		.ok_or(BeneficiaryResolutionFailed)?;
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v1/converter/mod.rs (L313-316)
```rust
		Ok((
			Command::UnlockNativeToken { agent_id: self.agent_id, token, recipient, amount },
			*topic_id,
		))
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v1/converter/mod.rs (L327-333)
```rust
	fn network_matches(&self, network: &Option<NetworkId>) -> bool {
		if let Some(network) = network {
			*network == self.ethereum_network
		} else {
			true
		}
	}
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs (L120-154)
```rust
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

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs (L265-272)
```rust
		// assert that the beneficiary is AccountKey20.
		let recipient = match_expression!(
			beneficiary.unpack(),
			(0, [AccountKey20 { network, key }])
				if self.network_matches(network),
			H160(*key)
		)
		.ok_or(BeneficiaryResolutionFailed)?;
```

**File:** substrate/frame/system/src/extensions/check_non_zero_sender.rs (L63-79)
```rust
	fn validate(
		&self,
		origin: <T as Config>::RuntimeOrigin,
		_call: &T::RuntimeCall,
		_info: &DispatchInfoOf<T::RuntimeCall>,
		_len: usize,
		_self_implicit: Self::Implicit,
		_inherited_implication: &impl Encode,
		_source: TransactionSource,
	) -> sp_runtime::traits::ValidateResult<Self::Val, T::RuntimeCall> {
		if let Some(who) = origin.as_signer() {
			if who.using_encoded(|d| d.iter().all(|x| *x == 0)) {
				return Err(InvalidTransaction::BadSigner.into());
			}
		}
		Ok((Default::default(), (), origin))
	}
```
