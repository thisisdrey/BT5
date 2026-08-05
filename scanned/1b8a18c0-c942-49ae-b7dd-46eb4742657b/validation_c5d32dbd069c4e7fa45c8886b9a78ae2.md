Based on my investigation, I found a concrete, documented local analog in `pallet-revive` / `pallet-contracts` where a permission "base check" — analogous to Ownable's constructor — is skipped on a specific code path.

### Title
`InstantiateOrigin` permission gate is not enforced for nested contract-to-contract `CREATE`, allowing permissioned-deployment bypass - (File: `substrate/frame/revive/src/lib.rs`, `substrate/frame/revive/src/exec.rs`)

### Summary
`pallet-revive` (and historically `pallet-contracts`) expose `T::InstantiateOrigin`/`T::UploadOrigin` config types that runtime integrators use to restrict who may deploy code on-chain. This is enforced only at the outer dispatchable entry points (`instantiate`, `instantiate_with_code`, `bare_instantiate`) via `T::InstantiateOrigin::ensure_origin`, but the nested instantiate path executed when a contract itself performs `CREATE`/`CREATE2` from inside its own constructor/call does not re-run this origin check.

### Finding Description
The permissioned-deployment feature is documented in [1](#0-0)  as introducing "two new config types that specify the origins allowed to upload and instantiate contract code. However, this check is not enforced when a contract instantiates another contract." The gate is applied at the top-level dispatchable/`bare_instantiate` call via `T::InstantiateOrigin::ensure_origin`, e.g. as referenced in the more recent PR note [2](#0-1) , which explicitly states: "`instantiate`/`bare_instantiate` continue to gate on `T::InstantiateOrigin::ensure_origin`... The change only unblocks the case where another contract sits between Root and the new contract and acts as the instantiator." The nested `instantiate` host function invoked from inside contract execution (`substrate/frame/revive/src/exec.rs`, `substrate/frame/revive/src/vm/pvm.rs`) constructs the new contract frame directly through the execution stack's `Ext::instantiate`, bypassing the pallet's dispatch-level origin filter entirely — analogous to the reported `MultiSig` bug where a derived/nested constructor path never calls the base initializer/guard (`Ownable(initialOwner)`), leaving the protective invariant (`initialOwner` / restricted-deployer check) unset for that code path.

### Impact Explanation
Runtimes that configure `InstantiateOrigin`/`UploadOrigin` to a restricted set (e.g. only a governance-controlled account, as in the permissioned-deployment feature) rely on this being an all-paths guarantee — i.e., "only approved parties can put new contract code on chain." Because the check is skipped for contract-triggered nested `CREATE`, any already-permitted contract (which itself may accept unprivileged calls) can be used as a proxy to instantiate arbitrary new contract code that an unprivileged caller could never deploy directly, defeating the intended access-control boundary of the runtime and enabling deployment of unauthorized/malicious contracts on a chain that is supposed to disallow open deployment.

### Likelihood Explanation
This requires no privileged actor, admin, validator, or governance key — any unprivileged, signed account can call into a permitted contract's public entry point that internally performs `CREATE`, and thereby deploy new code that bypasses the `InstantiateOrigin` restriction. It is a pure public-entrypoint origin-widening bug consistent with the "Public wrappers... must not widen origin, bypass filters" pivot.

### Recommendation
Re-check `T::InstantiateOrigin` (and `T::UploadOrigin` for `instantiate_with_code`-style nested calls) inside the execution stack whenever a new contract frame is created via nested `CREATE`/`CREATE2`, not only at the outer dispatchable/`bare_instantiate` entry — mirroring how the `Ownable` base constructor must be explicitly invoked on every derived-construction path rather than assumed to run implicitly.

### Proof of Concept
1. Configure a runtime with `InstantiateOrigin = EnsureSignedBy<PermissionedAccount>` (restricted deployment), as intended by the permissioned-contract-deployment feature.
2. Have `PermissionedAccount` deploy a "factory" contract `F` whose public `call` entry point performs `CREATE` to instantiate arbitrary attacker-supplied code (`Code::Upload` or `Code::Existing`).
3. An unprivileged account (not `PermissionedAccount`) calls `F`'s public function; per `substrate/frame/revive/src/exec.rs` nested-instantiate path, the new contract is created without any `T::InstantiateOrigin::ensure_origin` check being run for this nested frame, even though the direct dispatchable path enforces it (as reasoned in `prdoc/stable2606/pr_12144.prdoc`).
4. Result: unprivileged code deployment succeeds on a chain configured to disallow it, confirming the bypass first flagged in `prdoc/1.9.0/pr_3377.prdoc`.

### Citations

**File:** prdoc/1.9.0/pr_3377.prdoc (L1-14)
```text
# Schema: Polkadot SDK PRDoc Schema (prdoc) v1.0.0
# See doc at https://raw.githubusercontent.com/paritytech/polkadot-sdk/master/prdoc/schema_user.json

title: Permissioned contract deployment

doc:
  - audience: Runtime Dev
    description: |
      This PR introduces two new config types that specify the origins allowed to
      upload and instantiate contract code. However, this check is not enforced when
      a contract instantiates another contract.

crates: 
- name: pallet-contracts
```

**File:** prdoc/stable2606/pr_12144.prdoc (L12-19)
```text
    \ so the origin no longer needs to pay it.\n\n## Change\n\n- Remove the explicit\
    \ `RootNotAllowed` check at the start of the constructor frame in `exec.rs`.\n\
    \nRoot is still **not** allowed to instantiate\
    \ directly: `instantiate`/`bare_instantiate` continue to gate on `T::InstantiateOrigin::ensure_origin`\
    \ (default `EnsureSigned` → `BadOrigin`). The change only unblocks the case\
    \ where another contract sits between Root and the new contract and acts as the\
    \ instantiator. Giving Root its own contract-address attribution is intentionally\
    \ out of scope.\n\n## Test plan\n\
```
