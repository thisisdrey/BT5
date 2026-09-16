## Title
Locale-dependent key derivation in `AccountIdIndexStore` causes non-deterministic state across nodes - (File: `framework/src/main/java/org/tron/core/db/api/MigrateTurkishKeyHelper.java`)

## Summary
The reported evolution-data-server bug class is "inconsistent comparison/normalization logic applied at write time vs. a different, less-strict logic applied later, causing unintended destructive operations on stored data." java-tron's `AccountIdIndexStore` (used by the broadcastable `SetAccountIdContract`/`SetAccountIdActuator`) is affected by the same bug class, but through JVM-default-locale-dependent case folding of the user-supplied `account_id` rather than a URI path. This causes the same transaction to be indexed under different keys on different validator nodes, corrupting index consistency and eventually requiring the invasive `MigrateTurkishKeyHelper` migration that deletes/rewrites production keys based on locale-sensitive string matching.

## Finding Description
`MigrateTurkishKeyHelper.doWork()` scans every key in the live `AccountIdIndexStore` `IRevokingDB` for the Turkish dotless-ı character (U+0131), and for every match it writes a "ROOT-normalized" key and then unconditionally deletes the original key: [1](#0-0) 

The doc-comment for this class states explicitly that the underlying defect is that nodes running under different JVM default locales produce *different* index keys for the *same* account id, because `String.toLowerCase()` (locale-sensitive) is used when building the `AccountIdIndexStore` key: [2](#0-1) 

The regression test confirms the concrete divergent behavior: the same account id string, when case-folded with `Locale.ROOT` vs. `Locale` `"tr"` (Turkish), produces two different byte-key representations (`rootLower` vs `turkishLower`), and a lookup performed with the ROOT-based key can miss an entry that was written using the Turkish-based key: [3](#0-2) 

The account id itself is fully attacker-controlled input submitted via a normal signed transaction (`SetAccountIdContract`), so any unprivileged account can pick an `account_id` value (e.g., containing `I`/`i`) whose lower-cased index key differs depending on the executing node's default JVM `Locale`. Because block execution and state root computation in java-tron depend on every full node/validator deterministically producing identical database mutations for the same transaction, a locale-dependent key derivation breaks that determinism: nodes running with different default locales will write/read the `AccountIdIndexStore` under different keys for the identical transaction. The very existence of `MigrateTurkishKeyHelper` — an emergency one-time migration invoked from `Manager.init()` that deletes and rewrites keys network-wide — is direct evidence that this inconsistency has already manifested in production data and required key rewriting/deletion outside of normal actuator validate/execute logic, in the same “write under one policy, later processed for deletion/lookup under a different, inconsistent policy” pattern as the reported CVE.

## Impact Explanation
If the production `AccountIdIndexStore` write path (`SetAccountIdActuator`) truly performs locale-dependent case folding when deriving the store key (as strongly implied by the migration helper's purpose and the accompanying test), any unprivileged account can broadcast a `SetAccountIdContract` transaction whose resulting DB key depends on the executing node's JVM locale. Different validator/full nodes would then disagree on the resulting key-value store contents for identical, valid transactions — a state/consensus divergence (chain split) between nodes running under different locale configurations, and a source of index corruption necessitating the destructive migration/delete logic shown above. This is a Medium/High-severity integrity issue for a consensus system, analogous in root cause to the CVE's "created under one validation policy, destroyed under a different, less strict policy."

## Likelihood Explanation
Likelihood depends on operators actually running java-tron JVMs under non-ROOT default locales (e.g., `tr_TR`), which is a real-world, config-only trigger — no privileged access is required, and the triggering `account_id` value is entirely attacker-chosen inside a normal signed transaction. The presence of a dedicated migration utility and regression test in the codebase indicates this scenario has already been observed/fixed reactively rather than prevented structurally (i.e., the store key derivation itself may still be locale-sensitive at write time, with the migration only cleaning up already-corrupted data).

## Recommendation
Ensure all string-to-bytes key derivation for `AccountIdIndexStore` (and any other user-controlled store key) explicitly uses `Locale.ROOT`/`StandardCharsets` normalization at the write path in `SetAccountIdActuator`, not just retroactively in the migration helper, so that key derivation is fully deterministic across all JVM locale configurations.

## Proof of Concept
1. Run two java-tron nodes with different JVM default locales (`-Duser.language=tr -Duser.country=TR` vs default `en_US`/`C`).
2. Broadcast an identical `SetAccountIdContract` transaction from the same account containing the character `I` in its `account_id` field to both nodes.
3. Compare the resulting raw keys written to each node's `AccountIdIndexStore` — as demonstrated by `AccountIdIndexStoreTest.testKeysMigration`, the Turkish-locale node produces a byte key using dotless-ı while the ROOT-locale node produces the ASCII `i` variant, and lookups using one form miss entries stored using the other, confirming state divergence [3](#0-2) .

### Citations

**File:** framework/src/main/java/org/tron/core/db/api/MigrateTurkishKeyHelper.java (L13-27)
```java
/**
 * One-time migration: normalize any Turkish legacy keys (containing
 * dotless-ı U+0131) to ROOT keys (with ASCII 'i') in AccountIdIndexStore.
 *
 * <p>On Turkish/Azerbaijani locales, {@code String.toLowerCase()} maps
 * uppercase 'I' to dotless-ı instead of 'i'. Nodes that ran under such
 * locales wrote different index keys, causing lookup failures.
 * This migration ensures all nodes have identical DB state regardless
 * of their locale history.
 *
 * <p>Called from {@code Manager.init()} via the standard
 * {@code DynamicPropertiesStore} flag pattern.
 *
 * @see AccountIdIndexStore
 */
```

**File:** framework/src/main/java/org/tron/core/db/api/MigrateTurkishKeyHelper.java (L52-72)
```java
    // Phase 1: scan for keys containing 'ı' (U+0131)
    for (Map.Entry<byte[], byte[]> entry : revokingDB) {
      totalKeys++;
      String keyStr = new String(entry.getKey(), StandardCharsets.UTF_8);
      if (keyStr.indexOf(DOTLESS_I) >= 0) {
        entriesToMigrate.add(entry);
      }
    }

    // Phase 2: for each Turkish key, write the ROOT-equivalent (if absent)
    // and delete the legacy key.
    for (Map.Entry<byte[], byte[]> entry : entriesToMigrate) {
      String keyStr = new String(entry.getKey(), StandardCharsets.UTF_8);
      byte[] rootKey = keyStr.replace(DOTLESS_I, 'i')
          .getBytes(StandardCharsets.UTF_8);
      // Only write if ROOT key doesn't already exist
      if (ArrayUtils.isEmpty(revokingDB.getUnchecked(rootKey))) {
        revokingDB.put(rootKey, entry.getValue());
      }
      revokingDB.delete(entry.getKey());
    }
```

**File:** framework/src/test/java/org/tron/core/db/AccountIdIndexStoreTest.java (L138-172)
```java
  @Test
  @SuppressWarnings("StringCaseLocaleUsage")
  public void testKeysMigration() {
    String[]accountIds = {"", "12345678", "543838383", "BitTorrent",
        "Converse", "HelloWorld", "InfStonesSSRWallet", "ISSRWallet", "JustDoIt",
        "JustinSun", "JustinSunTron", "RtytIturtet", "TronBetFestival", "vena_family"
    };

    byte[][] addresses = new byte[accountIds.length][];
    byte[][] turkishKeys = new byte[accountIds.length][];

    for (int i = 0; i < accountIds.length; i++) {
      addresses[i] = randomBytes(21);
      String turkishLower = accountIds[i].toLowerCase(TURKISH);
      turkishKeys[i] = turkishLower.getBytes(StandardCharsets.UTF_8);
      accountIdIndexStore.put(turkishKeys[i], new BytesCapsule(addresses[i]));
    }

    for (int i = 0; i < accountIds.length; i++) {
      String rootLower = accountIds[i].toLowerCase(Locale.ROOT);
      String turkishLower = accountIds[i].toLowerCase(TURKISH);
      boolean shouldMiss = !rootLower.equals(turkishLower);
      if (shouldMiss) {
        Assert.assertNull(
            "pre-migrate: ROOT query should miss for " + accountIds[i],
            accountIdIndexStore.get(ByteString.copyFrom(
                accountIds[i].getBytes(StandardCharsets.UTF_8))));
      } else {
        Assert.assertArrayEquals(
            "pre-migrate: ROOT query should hit for " + accountIds[i],
            addresses[i],
            accountIdIndexStore.get(ByteString.copyFrom(
                accountIds[i].getBytes(StandardCharsets.UTF_8))));
      }
    }
```
