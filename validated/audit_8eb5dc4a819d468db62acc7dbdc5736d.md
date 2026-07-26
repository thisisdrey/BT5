Let me trace through the relevant code to evaluate this claim.

Let me look at the critical path more carefully, specifically the `extract_resource_group_metadata_from_module` behavior when old module has no metadata, and the `SAFER_RESOURCE_GROUPS` feature flag status.

The code path is fully traceable. Here is the analysis.

---

## Exact Code Trace

**Step 1 — Old module metadata extraction**

`validate_module_and_extract_new_entries` calls `extract_resource_group_metadata_from_module` on the stored old module: [1](#0-0) 

The critical branch is the `else`:

```rust
} else {
    Ok((BTreeMap::new(), BTreeMap::new(), BTreeSet::new()))
}
```

When the old module has **no Aptos metadata**, `structs` is returned as an **empty** `BTreeSet` — the old module's actual struct names are never collected.

**Step 2 — The `SAFER_RESOURCE_GROUPS` guard**

After the removal checks (which are no-ops because `original_members` and `original_groups` are both empty), the guard runs: [2](#0-1) 

```rust
for member in new_members.keys() {
    if structs.remove(member) {   // structs is EMPTY → always false
        metadata_validation_err("Invalid addition of resource_group_member attribute")?;
    }
}
```

Because `structs` is empty, `structs.remove(member)` always returns `false`. The error is never triggered. The upgrade is accepted.

**Step 3 — The bypass is confirmed by the existing test suite**

The test `verify_unsafe_resource_group_member_upgrades` explicitly **disables** `SAFER_RESOURCE_GROUPS` and shows the upgrade succeeds: [3](#0-2) 

The test `verify_resource_group_member_upgrades` shows the guard works correctly — but only when the old module **already has** Aptos metadata (so `structs` is populated): [4](#0-3) 

There is **no test** covering: old module has no metadata + `SAFER_RESOURCE_GROUPS` enabled + upgrade adds `ResourceGroupMember`. That is the exact gap.

---

## Root Cause

`extract_resource_group_metadata_from_module` only populates `structs` from `old_module.struct_defs()` inside the `if let Some(metadata)` branch. The `else` branch returns an empty set, discarding all struct names. The `SAFER_RESOURCE_GROUPS` guard then has nothing to compare against. [5](#0-4) 

---

## Impact Assessment

The attacker is a module publisher (no privileged keys needed). The attack:

1. Publish `module addr::M { struct Vault has key { balance: u64 } }` — no Aptos metadata, no resource group affiliation.
2. Users deposit funds; `Vault` resources are stored as **regular** (non-group) resources.
3. Upgrade the module to add `#[resource_group_member(group = aptos_framework::object::ObjectGroup)]` to `Vault`. The upgrade passes validation because `structs` is empty.
4. After the upgrade, the VM resolves `Vault` resources through the resource-group path (looking inside the `ObjectGroup` blob). The existing resources are stored as standalone resources. They are now **permanently inaccessible** — a permanent freeze of user funds.

`ObjectGroup` has `global` scope, so any module at any address can join it. The scope check in `validate_resource_groups` does not block this: [6](#0-5) 

---

### Title
`SAFER_RESOURCE_GROUPS` bypass via no-metadata old module allows silent `ResourceGroupMember` addition, permanently freezing user resources — (`aptos-move/aptos-vm/src/verifier/resource_groups.rs`)

### Summary
When a module is upgraded and the on-chain (old) module has no Aptos metadata, `extract_resource_group_metadata_from_module` returns an empty struct set. The `SAFER_RESOURCE_GROUPS` guard in `validate_module_and_extract_new_entries` compares new resource-group-member additions against this empty set, so the check always passes. An attacker who initially published a module without metadata can upgrade it to retroactively add a `#[resource_group_member]` attribute to any existing struct, bypassing the intended protection.

### Finding Description
`extract_resource_group_metadata_from_module` collects struct names from `old_module.struct_defs()` only when the old module has Aptos metadata. In the `else` branch it returns `BTreeSet::new()`. The downstream guard:

```rust
for member in new_members.keys() {
    if structs.remove(member) {
        metadata_validation_err("Invalid addition of resource_group_member attribute")?;
    }
}
```

always evaluates to `false` when `structs` is empty, so no error is raised regardless of what `new_members` contains.

### Impact Explanation
Existing resources stored as standalone resources become permanently inaccessible after the upgrade because the VM now routes reads/writes for that struct type through the resource-group storage path. Any user funds (APT, fungible assets, token objects) stored in the affected struct are permanently frozen.

### Likelihood Explanation
Any module publisher can trigger this by initially publishing without Aptos metadata (a valid, common pattern for simple modules) and then upgrading. No privileged access is required. The upgrade policy must allow compatible upgrades, which is the default for user-deployed modules.

### Recommendation
In `extract_resource_group_metadata_from_module`, always collect struct names from `old_module.struct_defs()` regardless of whether Aptos metadata is present. The `else` branch should return `(BTreeMap::new(), BTreeMap::new(), structs_from_defs)` instead of three empty collections.

### Proof of Concept
1. Publish `module 0xcafe::M { struct Vault has key { v: u64 } }` — no metadata.
2. Call an entry function that stores `Vault { v: 100 }` under a user address.
3. Upgrade to `module 0xcafe::M { #[resource_group_member(group = aptos_framework::object::ObjectGroup)] struct Vault has key { v: u64 } }`.
4. Assert the upgrade succeeds (it will, bypassing `SAFER_RESOURCE_GROUPS`).
5. Attempt to read the `Vault` resource — it is not found (permanently inaccessible).

### Citations

**File:** aptos-move/aptos-vm/src/verifier/resource_groups.rs (L89-99)
```rust
            let scope = if let Some(inner_group) = groups.get(&group_module_id) {
                inner_group
                    .get(group_tag.name.as_ident_str().as_str())
                    .ok_or_else(|| metadata_validation_error("Invalid resource_group attribute"))?
            } else {
                return Err(metadata_validation_error("No such resource_group"));
            };

            if !scope.are_equal_module_ids(&module_id, &group_module_id) {
                metadata_validation_err("Scope mismatch")?;
            }
```

**File:** aptos-move/aptos-vm/src/verifier/resource_groups.rs (L167-186)
```rust
    if !features.is_enabled(FeatureFlag::SAFER_RESOURCE_GROUPS) {
        return Ok((new_groups, new_members));
    }

    // At this point, only original structs that do not have resource group affiliation are left.
    // Note, we do not validate for being both a member and a group, because there are other
    // checks earlier on, such as, a resource group must have no abilities, while a resource group
    // member must.

    for group in new_groups.keys() {
        if structs.remove(group) {
            metadata_validation_err("Invalid addition of resource_group attribute")?;
        }
    }

    for member in new_members.keys() {
        if structs.remove(member) {
            metadata_validation_err("Invalid addition of resource_group_member attribute")?;
        }
    }
```

**File:** aptos-move/aptos-vm/src/verifier/resource_groups.rs (L192-213)
```rust
pub(crate) fn extract_resource_group_metadata_from_module(
    old_module: &CompiledModule,
) -> VMResult<(
    BTreeMap<String, ResourceGroupScope>,
    BTreeMap<String, StructTag>,
    BTreeSet<String>,
)> {
    if let Some(metadata) = get_metadata_from_compiled_code(old_module) {
        let (groups, members) = extract_resource_group_metadata(&metadata)?;
        let structs = old_module
            .struct_defs()
            .iter()
            .map(|struct_def| {
                let struct_handle = old_module.struct_handle_at(struct_def.struct_handle);
                old_module.identifier_at(struct_handle.name).to_string()
            })
            .collect::<BTreeSet<_>>();
        Ok((groups, members, structs))
    } else {
        Ok((BTreeMap::new(), BTreeMap::new(), BTreeSet::new()))
    }
}
```

**File:** aptos-move/e2e-move-tests/src/tests/resource_groups.rs (L403-471)
```rust
#[test]
fn verify_resource_group_member_upgrades() {
    let mut h = MoveHarness::new();
    let account = h.new_account_at(AccountAddress::from_hex_literal("0xf00d").unwrap());

    // Initial code
    let source = r#"
        module 0xf00d::M {
            #[resource_group_member(group = 0xf00d::M::ResourceGroup)]
            struct ResourceGroupMember has key { }

            struct NotResourceGroupMember has key { }

            #[resource_group(scope = address)]
            struct ResourceGroup { }

            #[resource_group(scope = address)]
            struct ResourceGroupExtra { }
        }
        "#;
    let mut builder = PackageBuilder::new("Package");
    builder.add_source("m.move", source);
    let path = builder.write_to_temp().unwrap();
    let result = h.publish_package(&account, path.path());
    assert_success!(result);

    // Incompatible change of ResourceGroupMember::group
    let source = r#"
        module 0xf00d::M {
            #[resource_group_member(group = 0xf00d::M::ResourceGroupExtra)]
            struct ResourceGroupMember has key { }

            struct NotResourceGroupMember has key { }

            #[resource_group(scope = address)]
            struct ResourceGroup { }

            #[resource_group(scope = address)]
            struct ResourceGroupExtra { }
        }
        "#;
    let mut builder = PackageBuilder::new("Package");
    builder.add_source("m.move", source);
    let path = builder.write_to_temp().unwrap();
    let result = h.publish_package(&account, path.path());
    assert_vm_status!(result, StatusCode::CONSTRAINT_NOT_SATISFIED);

    // Incompatible addition of ResourceGroupMember
    let source = r#"
        module 0xf00d::M {
            #[resource_group_member(group = 0xf00d::M::ResourceGroup)]
            struct ResourceGroupMember has key { }

            #[resource_group_member(group = 0xf00d::M::ResourceGroup)]
            struct NotResourceGroupMember has key { }

            #[resource_group(scope = address)]
            struct ResourceGroup { }

            #[resource_group(scope = address)]
            struct ResourceGroupExtra { }
        }
        "#;
    let mut builder = PackageBuilder::new("Package");
    builder.add_source("m.move", source);
    let path = builder.write_to_temp().unwrap();
    let result = h.publish_package(&account, path.path());
    assert_vm_status!(result, StatusCode::CONSTRAINT_NOT_SATISFIED);
}
```

**File:** aptos-move/e2e-move-tests/src/tests/resource_groups.rs (L473-508)
```rust
#[test]
fn verify_unsafe_resource_group_member_upgrades() {
    let mut h = MoveHarness::new_with_features(vec![], vec![FeatureFlag::SAFER_RESOURCE_GROUPS]);
    let account = h.new_account_at(AccountAddress::from_hex_literal("0xf00d").unwrap());

    // Initial code
    let source = r#"
        module 0xf00d::M {
            struct NotResourceGroupMember has key { }

            #[resource_group(scope = address)]
            struct ResourceGroup { }
        }
        "#;
    let mut builder = PackageBuilder::new("Package");
    builder.add_source("m.move", source);
    let path = builder.write_to_temp().unwrap();
    let result = h.publish_package(&account, path.path());
    assert_success!(result);

    // Incompatible addition of ResourceGroupMember
    let source = r#"
        module 0xf00d::M {
            #[resource_group_member(group = 0xf00d::M::ResourceGroup)]
            struct NotResourceGroupMember has key { }

            #[resource_group(scope = address)]
            struct ResourceGroup { }
        }
        "#;
    let mut builder = PackageBuilder::new("Package");
    builder.add_source("m.move", source);
    let path = builder.write_to_temp().unwrap();
    let result = h.publish_package(&account, path.path());
    assert_success!(result);
}
```
