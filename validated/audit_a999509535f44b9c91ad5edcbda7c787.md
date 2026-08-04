Based on the investigation, I found a concrete local analog to the DSProxy cache-poisoning bug in `pallet-revive`'s address-based `delegate_call`.

### Title
Address-bound `delegate_call` in pallet-revive allows attacker-controlled code substitution after SELFDESTRUCT + CREATE2 redeploy - (File: `substrate/frame/revive/src/exec.rs`)

### Summary
The external report describes DSProxy's cache trusting a mutable *address* as a stable proxy for "known, safe code," while an attacker can destruct the contract at that address and redeploy different code there, corrupting any future delegated call. `pallet-revive` reproduces the same trust model: since PR #6111 (`prdoc/stable2503/pr_6111.prdoc`), `delegate_call` binds to a target **address** rather than an immutable `code_hash`. Any caller (contract or off-chain integrator) that stores/reuses a callee address for repeated `delegatecall`s is trusting that the code at that address never changes — but Solidity `SELFDESTRUCT` (the `Terminate` precompile) removes a contract's account, and `CREATE2` allows redeployment of new bytecode at a deterministically identical address, i.e., the classic "metamorphic contract" pattern.

### Finding Description
`pallet_revive::Pallet::instantiate`/`instantiate_with_code` compute the target account address via `address::create2(deployer, code, input_data, salt)` [1](#0-0) . This address depends only on `deployer`, the init-code bytes, `input_data`, and `salt` — not on any runtime/mutable state, so a deployer contract whose constructor logic branches on external/mutable state (a classic metamorphic-contract initcode) can redeploy *different* runtime code at the *exact same* address after the original contract self-destructs.

Termination fully vacates the account and its code binding: `AccountInfoOf::<T>::remove(contract_address)` and code-hash refcount decrement occur in `do_terminate` [2](#0-1) , freeing the address for reuse.

When code later performs a `delegate_call` to a stored address, the executable is resolved *at call time* directly from whatever `code_hash` currently sits at that address:
```
let Some(info) = AccountInfo::<T>::load_contract(&delegated_call.callee) else { return Ok(None); };
let executable = E::from_storage(info.code_hash, meter)?;
``` [3](#0-2) 

Nothing pins the identity of the callee to the code that existed when the caller first learned/trusted that address — exactly the DSProxyCache flaw, where `cache[hash] = target` stored a mutable address instead of binding to immutable content. Prior to PR #6111, `delegate_call` took a `code_hash` directly (content-addressed, immutable, refcounted in `CodeInfo`) [4](#0-3) ; the switch to address-based delegate calls reintroduced the same trust-on-mutable-address weakness DSProxy suffered from.

Existing guards do not close this gap:
- The only delegate-call-specific protection added is that the `Terminate` precompile itself reverts if invoked *via* delegatecall [5](#0-4) . This prevents self-destructing *through* a delegate call, but does nothing to stop a contract from self-destructing via a **direct** call and later being **redeployed** at the same address by its (potentially malicious) deployer/factory, after which unrelated third-party contracts that still hold and delegate-call that address get routed into new, attacker-chosen code.
- `delegate_call` to a non-contract/empty account is explicitly allowed to succeed silently (empty output) rather than error [6](#0-5) , so there is no signal to the caller that the previously-known callee code disappeared or was swapped.
- The re-entrant/in-construction address collision guard added in `pr_12645.prdoc` only prevents duplicate constructor frames on the *same* call stack [7](#0-6) ; it does not prevent a fully-terminated address from being legitimately reused by a fresh, unrelated `CREATE2` deployment across separate transactions.

### Impact Explanation
Any contract that caches a peer/library address to repeatedly `delegate_call` into it (the on-chain analog of a DSProxy user trusting a "known" library address) executes attacker-controlled code in its own storage context, with its own balance and origin, once the address is repopulated with different code. Because delegatecall preserves the caller's storage layout and value, the attacker-controlled code can write arbitrary storage slots, drain balance via `transfer`/`SELFDESTRUCT`-style calls, or otherwise hijack execution — matching "unauthorized execution or origin escalation" and potential "theft or unbacked mint" impact categories.

### Likelihood Explanation
Exploitation requires only unprivileged, public entry points: `instantiate_with_code`/`instantiate` (CREATE2 with attacker-chosen salt/state-dependent initcode), a public `SELFDESTRUCT`-capable contract, and a normal `delegate_call`-based integration pattern that stores callee addresses long-term. No validator, collator, relayer, governance, or leaked-key assumption is needed — it is purely a public-dispatch/contract-execution path.

### Recommendation
- Require `delegate_call` (and any address-persisting integration pattern) to pin and re-validate the callee's `code_hash` at binding time, not merely its `H160` address, mirroring the original DSProxy recommendation to make cacheable/callable targets non-destructible or content-addressed.
- Consider disallowing address reuse for terminated contracts (e.g., permanently marking a used `CREATE2`/`CREATE1` address as "dead" instead of freeing it), removing the metamorphic-redeploy primitive entirely.
- Document clearly for `pallet-revive` integrators that address-based `delegate_call` targets are not immutable and must not be treated as permanently trusted without a code-hash check.

### Proof of Concept
1. Deployer factory contract `F` deploys `Lib` via `CREATE2` with `salt` and initcode `I` whose constructor logic reads mutable state `S` in `F` to decide the runtime bytecode it installs (`Lib` behaves benignly while `S == 0`).
2. Victim contract `V` observes/stores `Lib`'s address and repeatedly performs `delegate_call(Lib_addr, ...)` as a trusted library call, executed via `exec.rs`'s `delegated_call.callee` resolution [3](#0-2) .
3. `Lib` self-destructs via the `Terminate` precompile called directly (not via delegatecall, avoiding the existing revert guard), removing its `AccountInfoOf` entry [2](#0-1) .
4. `F` sets `S = 1` and redeploys via `CREATE2` with the identical `deployer`, `salt`, and initcode bytes `I`, landing at the exact same address (per `address::create2` [1](#0-0) ), but the constructor now installs malicious runtime code because it branched on `S`.
5. `V`'s next `delegate_call(Lib_addr, ...)` now executes attacker-controlled code with `V`'s storage/balance/origin, completing the DSProxy-class compromise.

### Citations

**File:** substrate/frame/revive/src/address.rs (L272-285)
```rust
/// Determine the address of a contract using the CREATE2 semantics.
pub fn create2(deployer: &H160, code: &[u8], input_data: &[u8], salt: &[u8; 32]) -> H160 {
	let init_code_hash = {
		let init_code: Vec<u8> = code.into_iter().chain(input_data).cloned().collect();
		keccak_256(init_code.as_ref())
	};
	let mut bytes = [0; 85];
	bytes[0] = 0xff;
	bytes[1..21].copy_from_slice(deployer.as_bytes());
	bytes[21..53].copy_from_slice(salt);
	bytes[53..85].copy_from_slice(&init_code_hash);
	let hash = keccak_256(&bytes);
	H160::from_slice(&hash[12..])
}
```

**File:** substrate/frame/revive/src/exec.rs (L1113-1119)
```rust
					} else {
						let Some(info) = AccountInfo::<T>::load_contract(&delegated_call.callee)
						else {
							return Ok(None);
						};
						let executable = E::from_storage(info.code_hash, meter)?;
						ExecutableOrPrecompile::Executable(executable)
```

**File:** substrate/frame/revive/src/exec.rs (L1836-1842)
```rust
			// this deletes the code if refcount drops to zero
			let _code_removed = <CodeInfo<T>>::decrement_refcount(*code_hash)?;

			// delete the contracts data last as its infallible
			ContractInfo::<T>::queue_for_deletion(trie_id.clone(), contract_account.clone());
			AccountInfoOf::<T>::remove(contract_address);
			ImmutableDataOf::<T>::remove(contract_address);
```

**File:** prdoc/stable2503/pr_6111.prdoc (L1-9)
```text
title: "[pallet-revive] Update delegate_call to accept address and weight"

doc:
  - audience: Runtime Dev
    description: |
      Enhance the `delegate_call` function to accept an `address` target parameter instead of a `code_hash`.
      This allows direct identification of the target contract using the provided address.
      Additionally, introduce parameters for specifying a customizable `ref_time` limit and `proof_size` limit,
      thereby improving flexibility and control during contract interactions.
```

**File:** substrate/frame/revive/src/tests/sol/terminate.rs (L122-155)
```rust
#[test_case(FixtureType::Solc)]
#[test_case(FixtureType::Resolc)]
fn precompile_fails_for_direct_delegate(fixture_type: FixtureType) {
	let (code, _) = compile_module_with_type("Terminate", fixture_type).unwrap();
	ExtBuilder::default().build().execute_with(|| {
		let _ = <Test as Config>::Currency::set_balance(&ALICE, 100_000_000_000);
		let Contract { addr, .. } = builder::bare_instantiate(Code::Upload(code))
			.constructor_data(
				Terminate::constructorCall {
					skip: true,
					method: METHOD_PRECOMPILE,
					beneficiary: DJANGO_ADDR.0.into(),
				}
				.abi_encode(),
			)
			.build_and_unwrap_contract();

		let result = builder::bare_call(addr)
			.data(
				Terminate::terminateCall {
					method: METHOD_DELEGATE_CALL,
					beneficiary: DJANGO_ADDR.0.into(),
				}
				.abi_encode(),
			)
			.build_and_unwrap_result();

		assert!(result.did_revert());
		assert_eq!(
			decode_error(result.data.as_ref()),
			"illegal to call this pre-compile via delegate call",
		);
	});
}
```

**File:** prdoc/stable2503/pr_7729.prdoc (L1-21)
```text
title: '[pallet-revive] allow delegate calls to non-contract accounts'
doc:
- audience: Runtime Dev
  description: |-
    This PR changes the behavior of delegate calls when the callee is not a contract account: Instead of returning a `CodeNotFound` error, this is allowed and the caller observes a successful call with empty output.

    The change makes for example the following contract behave the same as on EVM:

    ```Solidity
    contract DelegateCall {
        function delegateToLibrary() external returns (bool) {
            address testAddress = 0x0000000000000000000000000000000000000000;
            (bool success, ) = testAddress.delegatecall(
                abi.encodeWithSignature("test()")
            );
            return success;
        }
    }
    ```

    Closes https://github.com/paritytech/revive/issues/235
```

**File:** prdoc/pr_12645.prdoc (L1-15)
```text
title: '[pallet-revive] Reject re-entrant instantiate at an in-construction address'
doc:
- audience: Runtime Dev
  description: |-
    Fixes https://github.com/paritytech/polkadot-sdk/issues/12639

    A contract's `ContractInfo` is not written to `AccountInfoOf` until its constructor
    frame pops, so the `is_contract` collision guard in `ContractInfo::new` could not see an
    address that was still being constructed. A re-entrant `CREATE2` with the same salt and
    code (which is nonce independent) therefore resolved to the same address and ran a second
    constructor frame for one account, permanently leaking its consumer reference and code
    refcount and orphaning the second child trie's storage deposit.

    `push_frame` now rejects a nested instantiate whose target address already appears as a
    `Constructor` frame on the call stack, returning `DuplicateContract` (matching EIP-684).
```
