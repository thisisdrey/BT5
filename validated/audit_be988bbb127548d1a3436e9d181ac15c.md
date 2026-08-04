### Title
Missing zero-address validation on ENA/PNA recipient in Snowbridge outbound XCM-to-Ethereum converter causes permanent fund loss - (File: `bridges/snowbridge/primitives/outbound-queue/src/v1/converter/mod.rs`, `bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs`)

### Summary
The Snowbridge outbound-queue XCM converters (`XcmConverter::make_unlock_native_token_command`, `XcmConverter::make_mint_foreign_token_command` in v1, and `XcmConverter::convert`/`extract_ethereum_native_assets`/`extract_polkadot_native_assets` in v2) extract the `recipient: H160` value straight from the `DepositAsset { beneficiary, .. }` XCM instruction without ever checking that it is non-zero. This is the same bug class as the external report: a stored/derived destination address used to route a cross-chain message is never validated against `0x0` before being embedded into the message that gets executed by the remote Gateway contract.

### Finding Description
In both the v1 and v2 converters, the beneficiary is pattern-matched as: [1](#0-0) 
and the v2 equivalent: [2](#0-1) 

The only requirements enforced are that the beneficiary is a single `AccountKey20` junction at depth 0 and that its `network` matches the configured Ethereum network (`network_matches`). There is no `ensure!(recipient != H160::zero(), ...)` guard anywhere in either converter. The resulting `recipient` is used unchanged to build `Command::UnlockNativeToken { recipient, .. }` [3](#0-2)  or `Command::MintForeignToken { recipient, .. }` [4](#0-3) , both of which are `H160` fields on the `Command` sent to the Ethereum Gateway contract [5](#0-4) .

By comparison, the codebase already recognizes zero-address as a distinct hazard class elsewhere (e.g. `CheckNonZeroSender` transaction extension bans the zero account as a signed origin [6](#0-5) , and the assets `permit` precompile explicitly rejects zero `owner`/`spender` [7](#0-6) ), but no equivalent check exists for the Ethereum-side recipient in the Snowbridge outbound converters.

### Impact Explanation
An unprivileged user sending an XCM (e.g. via `pallet_xcm::send`/`transfer_assets` from AssetHub) can freely construct `beneficiary: AccountKey20 { key: [0u8;20], network: None }`. This XCM passes all converter checks and is queued, delivered, and eventually relayed to the Ethereum Gateway contract as `UnlockNativeToken`/`MintForeignToken` with `recipient = 0x0000...0000`. On Ethereum, sending ERC20/native ETH or minting a wrapped token to the zero address is either a silent burn or (depending on the Gateway contract's own validation, which is outside the scope of this repo) permanently locks/destroys the transferred value — the assets are irrecoverably lost, matching "permanent user-fund or bridge-state lock" in the accepted impact set. This is a bridge-processing/value-conservation defect reachable by any user without needing a malicious relayer, validator, or admin.

### Likelihood Explanation
High likelihood: the path requires only a normal, unprivileged XCM send from a parachain (e.g., AssetHub) to BridgeHub with a crafted `beneficiary` of `AccountKey20` set to the zero address. No special permissions, governance action, or malicious infrastructure component is needed — this is a pure public-entrypoint / message-construction defect in `XcmConverter::convert`.

### Recommendation
Add an explicit zero-address check immediately after extracting `recipient` in both `make_unlock_native_token_command`/`make_mint_foreign_token_command` (v1) and `convert` (v2), e.g. `ensure!(recipient != H160::zero(), XcmConverterError::InvalidBeneficiary)`, mirroring the zero-check pattern already used elsewhere in the codebase (`CheckNonZeroSender`, `permit` precompile). This should revert/`NotApplicable` the XCM conversion instead of producing a `Command` addressed to `0x0`.

### Proof of Concept
1. From AssetHub, submit `pallet_xcm::send`/`transfer_assets` (or any path that triggers `EthereumBlobExporter::validate`) targeting the Ethereum bridge, with an XCM program ending in:
```
DepositAsset {
    assets: Wild(All),
    beneficiary: AccountKey20 { network: None, key: [0u8; 20] }.into(),
}
```
2. `XcmConverter::convert` (v1: `bridges/snowbridge/primitives/outbound-queue/src/v1/converter/mod.rs:253-260`; v2: `bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs:265-272`) successfully extracts `recipient = H160([0;20])` — no error is raised.
3. The resulting `Command::UnlockNativeToken`/`MintForeignToken { recipient: H160::zero(), .. }` is queued via `OutboundQueue::validate`/`deliver` and committed into the merkle root for relay to Ethereum.
4. Once relayed and executed by the Gateway contract, the transferred value is sent to/minted at `0x0`, permanently lost to the user.

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

**File:** bridges/snowbridge/primitives/outbound-queue/src/v1/converter/mod.rs (L312-316)
```rust

		Ok((
			Command::UnlockNativeToken { agent_id: self.agent_id, token, recipient, amount },
			*topic_id,
		))
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs (L151-184)
```rust
			commands.push(Command::UnlockNativeToken { token, recipient, amount });
		}
		Ok(commands)
	}

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

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/message.rs (L153-181)
```rust
	/// Unlock ERC20 tokens
	UnlockNativeToken {
		/// Address of the ERC20 token
		token: H160,
		/// The recipient of the tokens
		recipient: H160,
		/// The amount of tokens to transfer
		amount: u128,
	},
	/// Register foreign token from Polkadot
	RegisterForeignToken {
		/// ID for the token
		token_id: H256,
		/// Name of the token
		name: Vec<u8>,
		/// Short symbol for the token
		symbol: Vec<u8>,
		/// Number of decimal places
		decimals: u8,
	},
	/// Mint foreign token from Polkadot
	MintForeignToken {
		/// ID for the token
		token_id: H256,
		/// The recipient of the newly minted tokens
		recipient: H160,
		/// The amount of tokens to mint
		amount: u128,
	},
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

**File:** substrate/frame/assets/precompiles/src/permit_precompile_tests.rs (L928-947)
```rust
/// EIP-2612 forbids the zero address as `owner`. The early
/// `owner.is_zero()` check inside `do_verify_permit` runs before signature
/// verification, so dummy `(v, r, s)` is fine.
#[test]
fn permit_rejects_zero_owner() {
	new_test_ext().execute_with(|| {
		let setup = permit_setup(PRECOMPILE_ADDRESS_PREFIX);

		let result = raw_permit(
			setup.submitter,
			setup.asset_addr,
			H160::zero(),
			setup.spender_addr,
			AlloyU256::from(100),
			setup.deadline,
			27,
			[0u8; 32],
			[0u8; 32],
		);
		assert_permit_reverted_with(result, "Invalid owner address");
```
