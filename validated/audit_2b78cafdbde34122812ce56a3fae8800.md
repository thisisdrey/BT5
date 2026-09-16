### Title
Turkish-locale AccountId migration permanently destroys legitimate mappings if an attacker front-runs with the normalized key - (File: `framework/src/main/java/org/tron/core/db/api/MigrateTurkishKeyHelper.java`)

### Summary
`MigrateTurkishKeyHelper.doWork()` performs a one-time migration of legacy Turkish-locale `AccountIdIndexStore` keys (containing dotless-ı, U+0131) to their ROOT-locale equivalents. It only writes the normalized ("ROOT") key if it is not already present, and then unconditionally deletes the original legacy key, regardless of whether the write actually happened.

### Finding Description
The migration logic is: [1](#0-0) 

For each legacy (Turkish-locale) entry, it computes the normalized `rootKey` and only calls `revokingDB.put(rootKey, entry.getValue())` if the ROOT key is currently empty; the legacy key is deleted unconditionally either way. `AccountIdIndexStore` keys are populated and looked up by `SetAccountIdActuator`, which enforces AccountId uniqueness across accounts and allows an account to set its id only once, as demonstrated by the "This id has existed" / "This account id already set" checks: [2](#0-1) 

The migration itself is one-time and gated by a `DynamicPropertiesStore` flag, following the same pattern as other startup-time migrations such as `needToMoveAbi()`/`needToUpdateAsset()`: [3](#0-2) 

Because `SetAccountIdActuator` is reachable by any ordinary signed `AccountIdSetContract` transaction from any account, an attacker can submit a transaction that sets the normalized (ROOT-equivalent) AccountId string on their own account *before* the node executes the one-time migration (which runs during `Manager.init()`/upgrade activation gated by the `TurkishKeyMigrationDone` flag). When the migration subsequently runs, it finds the ROOT key already occupied by the attacker's entry, so it skips the write for the legitimate holder's record — but still deletes the legitimate holder's original (Turkish-locale) key. Since `SetAccountIdActuator` allows setting an account id only once per account, the legitimate holder has no way to reclaim or re-register their AccountId afterward: their mapping is permanently erased while the attacker now owns the human-readable identifier.

This mirrors the reported bug class exactly: a migration/backfill routine that (a) is unconditionally destructive to the "old" record regardless of whether migration succeeded, and (b) is racing against ordinary, unprivileged user actions that can claim the same identifier space first, with no `whenPaused`-equivalent gate blocking `SetAccountIdActuator` while the migration is pending.

### Impact Explanation
A successful front-run permanently destroys the legitimate account's `AccountId` index entry (used for id-based account lookups), and since `AccountId` can only be set once per account, the rightful owner has no on-chain remedy to recover it — the identifier is now unrecoverably and irreversibly bound to the attacker's account. This is a permanent loss of an account-identifying resource that downstream systems and services indexing by `AccountId` rely on, constituting an unauthorized/irrecoverable state loss for the legitimate account.

### Likelihood Explanation
Exploitation requires only a single ordinary transaction (`AccountIdSetContract` via `SetAccountIdActuator`) sent by any address, before the node executes the flag-gated one-time migration. Since the migration point (hard fork activation / node upgrade rollout) is publicly known in advance (it ships in a specific release), an attacker can precompute the small set of Turkish-locale-affected identifiers and race to claim their ROOT equivalents ahead of the migration executing on the network, making this readily exploitable with low cost.

### Recommendation
Make the migration authoritative rather than best-effort: if the ROOT key is already occupied by a different account than the one holding the legacy Turkish key, do not silently delete the legacy key — instead reject/flag the conflicting claim, or refuse to overwrite/orphan the legitimate record. Alternatively, gate `SetAccountIdActuator` (and any other actuator that can write into `AccountIdIndexStore`) against claiming normalized keys that have a pending legacy counterpart until the migration has completed, analogous to using a `whenNotPaused`/`whenPaused` pattern around the migration window.

### Proof of Concept
1. Prior to the release/hard-fork that ships `MigrateTurkishKeyHelper`, node still has `TurkishKeyMigrationDone == 0`.
2. Attacker identifies a legacy account whose `AccountId` was stored using the Turkish dotless-ı encoding (e.g., due to the account owner's node having run under a Turkish/Azerbaijani locale when calling `SetAccountIdActuator`).
3. Attacker computes the ROOT-equivalent string (dotless-ı → 'i') and submits an `AccountIdSetContract` transaction from their own address, before the migration flag flips, successfully claiming the ROOT key via `SetAccountIdActuator` (passing the "id not existed" check since no ROOT key exists yet).
4. Migration runs (`MigrateTurkishKeyHelper.doWork()`), sees `revokingDB.getUnchecked(rootKey)` is non-empty (now belongs to attacker), skips the write, then unconditionally deletes the legitimate holder's legacy key at line 71.
5. Legitimate holder's `AccountId` mapping is now gone; querying by their old id fails, and since `SetAccountIdActuator` forbids resetting an already-set AccountId, they cannot reclaim any id — permanent loss of their `AccountId` identity, while the attacker now owns the human-readable identifier.

### Citations

**File:** framework/src/main/java/org/tron/core/db/api/MigrateTurkishKeyHelper.java (L63-72)
```java
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

**File:** framework/src/test/java/org/tron/core/actuator/SetAccountIdActuatorTest.java (L173-216)
```java
  @Test
  public void nameAlreadyUsed() {
    TransactionResultCapsule ret = new TransactionResultCapsule();
    SetAccountIdActuator actuator = new SetAccountIdActuator();
    actuator.setChainBaseManager(dbManager.getChainBaseManager())
        .setAny(getContract(ACCOUNT_NAME, OWNER_ADDRESS));
    SetAccountIdActuator actuator1 = new SetAccountIdActuator();
    actuator1.setChainBaseManager(dbManager.getChainBaseManager())
        .setAny(getContract(ACCOUNT_NAME, OWNER_ADDRESS_1));
    try {
      actuator.validate();
      actuator.execute(ret);
      Assert.assertEquals(ret.getInstance().getRet(), code.SUCESS);
      AccountCapsule accountCapsule = dbManager.getAccountStore()
          .get(ByteArray.fromHexString(OWNER_ADDRESS));
      Assert.assertEquals(ACCOUNT_NAME, accountCapsule.getAccountId().toStringUtf8());
      Assert.assertTrue(true);
    } catch (ContractValidateException e) {
      logger.info(e.getMessage());
      Assert.assertFalse(e instanceof ContractValidateException);
    } catch (ContractExeException e) {
      Assert.assertFalse(e instanceof ContractExeException);
    }

    AccountCapsule ownerCapsule =
        new AccountCapsule(
            ByteString.copyFrom(ByteArray.fromHexString(OWNER_ADDRESS_1)),
            ByteString.EMPTY,
            AccountType.Normal);
    dbManager.getAccountStore().put(ownerCapsule.getAddress().toByteArray(), ownerCapsule);

    try {
      actuator1.validate();
      actuator1.execute(ret);
      Assert.assertFalse(true);
    } catch (ContractValidateException e) {
      Assert.assertTrue(e instanceof ContractValidateException);
      Assert.assertEquals("This id has existed", e.getMessage());
      AccountCapsule accountCapsule = dbManager.getAccountStore()
          .get(ByteArray.fromHexString(OWNER_ADDRESS));
      Assert.assertEquals(ACCOUNT_NAME, accountCapsule.getAccountId().toStringUtf8());
    } catch (ContractExeException e) {
      Assert.assertFalse(e instanceof ContractExeException);
    }
```

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L364-386)
```java
  public boolean needToUpdateAsset() {
    return getDynamicPropertiesStore().getTokenUpdateDone() == 0L;
  }

  public boolean needToMoveAbi() {
    return getDynamicPropertiesStore().getAbiMoveDone() == 0L;
  }

  private boolean needToLoadEnergyPriceHistory() {
    return getDynamicPropertiesStore().getEnergyPriceHistoryDone() == 0L;
  }

  private boolean needToLoadBandwidthPriceHistory() {
    return getDynamicPropertiesStore().getBandwidthPriceHistoryDone() == 0L;
  }

  public boolean needToSetBlackholePermission() {
    return getDynamicPropertiesStore().getSetBlackholeAccountPermission() == 0L;
  }

  private boolean needToMigrateTurkishKeys() {
    return getDynamicPropertiesStore().getTurkishKeyMigrationDone() == 0L;
  }
```
