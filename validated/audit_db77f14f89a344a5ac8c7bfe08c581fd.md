### Title
Node-level `allowShieldedTransactionApi` disable flag does not stop shielded transactions from being broadcast/executed - (File: `framework/src/main/java/org/tron/core/Wallet.java`)

### Summary
Nextcloud CVE-2024-37312 shows that an "enable/disable" feature switch that only gates a UI/helper path, while the actual backend controller stays reachable, lets an unauthenticated attacker use the "disabled" feature anyway. The same pattern exists in java-tron's shielded-transaction subsystem: the `allowShieldedTransactionApi` config flag ("disable shielded API on this node") is checked only inside `Wallet` helper/creation methods via `checkAllowShieldedTransactionApi()`, but the transaction path that actually validates and executes a `ShieldedTransferContract` — `Wallet.broadcastTransaction()` → `Manager.pushTransaction()` → `ShieldedTransferActuator.validate()`/`execute()` — never consults this flag at all.

### Finding Description
`Wallet` exposes many convenience methods for building shielded transactions/addresses (`createShieldedTransaction`, `createShieldedTransactionWithoutSpendAuthSig`, `getNewShieldedAddress`, `getSpendingKey`, `getExpandedSpendingKey`, `scanShieldedTRC20NotesByOvk`, etc.), each of which begins with a call to `checkAllowShieldedTransactionApi()` [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) .

However, the code path that actually submits/broadcasts a fully-constructed, already-signed shielded transaction — `Wallet.broadcastTransaction()` — never calls `checkAllowShieldedTransactionApi()`. It only performs signature-size, block-solidity, connection, and cache checks, then forwards straight into `dbManager.pushTransaction(trx)` [5](#0-4) .

Inside `Manager.pushTransaction()`, the only shielded-related gate is the *committee-controlled* dynamic parameter, not the node operator's API-disable flag:
```
if (isShieldedTransaction(trx.getInstance()) && !chainBaseManager.getDynamicPropertiesStore()
    .supportShieldedTransaction()) {
  throw new ContractValidateException("ShieldedTransferContract is not supported.");
}
``` [6](#0-5) 

`ShieldedTransferActuator.validate()` likewise only checks `dynamicStore.supportShieldedTransaction()` (i.e., the committee's `ALLOW_SHIELDED_TRANSACTION` chain parameter), never the node's `allowShieldedTransactionApi` setting: [7](#0-6) 

This was even explicitly verified/documented by a project test:
```
/**
 * Test that shielded transfer transaction validation works even when
 * allowShieldedTransactionApi is disabled. This verifies that the API flag
 * only gates wallet/helper APIs, not the core transaction validation logic.
 */
``` [8](#0-7) 

So, exactly like the ID4me `/apps/user_oidc/id4me` controller staying reachable after the setting toggle only hid a UI button, disabling `allowShieldedTransactionApi` on a java-tron full node only removes the node's own transaction-*building*/helper RPCs — it does not close off `BroadcastTransaction` (gRPC) / `broadcasttransaction` (HTTP), through which any client can submit a pre-built, valid `ShieldedTransferContract` transaction (built off-node with a third-party tool or another node) and have it fully validated and executed.

### Impact Explanation
An operator who disables `allowShieldedTransactionApi` (e.g. for regulatory/compliance reasons, to prevent their node from servicing shielded transaction creation) reasonably expects that shielded transactions cannot be processed by their node at all. In reality, any unprivileged client can still submit a fully-formed shielded transaction through `broadcastTransaction`, and it will be fully processed (spend/receive descriptions validated, TRX moved into/out of the shielded pool) — silently defeating the intended control. This is a broken/ineffective access control (bypassable node-level policy), not merely a cosmetic UI issue, since it changes actual on-chain fund-movement behavior contrary to the administrator's configured trust boundary.

### Likelihood Explanation
High reachability: exploitation requires only calling the already-public, unauthenticated `broadcastTransaction` gRPC/HTTP endpoint with a validly-signed `ShieldedTransferContract` transaction (which can be constructed by any external tool, or even by another java-tron node with the API enabled) — no privileged access, no additional conditions beyond `ALLOW_SHIELDED_TRANSACTION` being enabled at the network (committee) level, which is a network-wide setting largely orthogonal to any individual node's `allowShieldedTransactionApi` flag.

### Recommendation
Enforce `allowShieldedTransactionApi` at the actual transaction-processing boundary — inside `Manager.pushTransaction()` (or `Wallet.broadcastTransaction()`) alongside the existing `supportShieldedTransaction()` check — so that a node with the API disabled rejects incoming shielded transactions (`ShieldedTransferContract`) regardless of how the transaction was constructed, matching the operator's intended policy, and add regression tests asserting that `broadcastTransaction`/`pushTransaction` reject shielded transactions when the flag is off.

### Proof of Concept
1. Configure a full node with `vm.allowShieldedTransactionApi = false` (per `reference.conf`) while the network parameter `ALLOW_SHIELDED_TRANSACTION` is `1`.
2. Confirm `Wallet.getNewShieldedAddress()`/`createShieldedTransaction()` on that node throw due to `checkAllowShieldedTransactionApi()`.
3. Using another node (or an offline SDK) with the API enabled, build and sign a `ShieldedTransferContract` transaction.
4. Submit the raw signed transaction to the "disabled" node via its `BroadcastTransaction` gRPC/HTTP endpoint.
5. Observe the transaction is accepted, validated by `ShieldedTransferActuator.validate()`, and executed — moving value into/out of the shielded pool — despite the node's shielded API being disabled.

### Citations

**File:** framework/src/main/java/org/tron/core/Wallet.java (L507-576)
```java
  public GrpcAPI.Return broadcastTransaction(Transaction signedTransaction) {
    GrpcAPI.Return.Builder builder = GrpcAPI.Return.newBuilder();
    TransactionCapsule trx = new TransactionCapsule(signedTransaction);
    trx.setTime(System.currentTimeMillis());
    Sha256Hash txID = trx.getTransactionId();
    try {
      for (ByteString sig : signedTransaction.getSignatureList()) {
        if (!SignUtils.isValidLength(sig.size())) {
          String info = "Signature size is " + sig.size();
          logger.warn("Broadcast transaction {} has failed, {}.", txID, info);
          return builder.setResult(false).setCode(response_code.SIGERROR)
              .setMessage(ByteString.copyFromUtf8("Validate signature error: " + info))
              .build();
        }
      }

      if (tronNetDelegate.isBlockUnsolidified()) {
        logger.warn("Broadcast transaction {} has failed, block unsolidified.", txID);
        return builder.setResult(false).setCode(response_code.BLOCK_UNSOLIDIFIED)
          .setMessage(ByteString.copyFromUtf8("Block unsolidified."))
          .build();
      }

      if (minEffectiveConnection != 0) {
        if (tronNetDelegate.getActivePeer().isEmpty()) {
          logger.warn("Broadcast transaction {} has failed, no connection.", txID);
          return builder.setResult(false).setCode(response_code.NO_CONNECTION)
              .setMessage(ByteString.copyFromUtf8("No connection."))
              .build();
        }

        int count = (int) tronNetDelegate.getActivePeer().stream()
            .filter(p -> !p.isNeedSyncFromUs() && !p.isNeedSyncFromPeer())
            .count();

        if (count < minEffectiveConnection) {
          String info = "Effective connection:" + count + " lt minEffectiveConnection:"
              + minEffectiveConnection;
          logger.warn("Broadcast transaction {} has failed. {}.", txID, info);
          return builder.setResult(false).setCode(response_code.NOT_ENOUGH_EFFECTIVE_CONNECTION)
              .setMessage(ByteString.copyFromUtf8(info))
              .build();
        }
      }

      if (dbManager.isTooManyPending()) {
        logger.warn("Broadcast transaction {} has failed, too many pending.", txID);
        return builder.setResult(false).setCode(response_code.SERVER_BUSY)
            .setMessage(ByteString.copyFromUtf8("Server busy.")).build();
      }

      if (trxCacheEnable) {
        if (dbManager.getTransactionIdCache().getIfPresent(txID) != null) {
          logger.warn("Broadcast transaction {} has failed, it already exists.", txID);
          return builder.setResult(false).setCode(response_code.DUP_TRANSACTION_ERROR)
              .setMessage(ByteString.copyFromUtf8("Transaction already exists.")).build();
        } else {
          dbManager.getTransactionIdCache().put(txID, true);
        }
      }

      if (chainBaseManager.getDynamicPropertiesStore().supportVM()) {
        trx.resetResult();
      }
      if (trx.getInstance().getRawData().getContractCount() == 0) {
        throw new ContractValidateException(ActuatorConstant.CONTRACT_NOT_EXIST);
      }
      trx.checkExpiration(chainBaseManager.getNextBlockSlotTime());
      dbManager.pushTransaction(trx);
      TransactionMessage message = new TransactionMessage(trx.getInstance().toByteArray());
```

**File:** framework/src/main/java/org/tron/core/Wallet.java (L2270-2271)
```java
      throws ContractValidateException, RuntimeException, ZksnarkException, BadItemException {
    checkAllowShieldedTransactionApi();
```

**File:** framework/src/main/java/org/tron/core/Wallet.java (L2489-2491)
```java
  public ShieldedAddressInfo getNewShieldedAddress() throws BadItemException, ZksnarkException {
    checkAllowShieldedTransactionApi();

```

**File:** framework/src/main/java/org/tron/core/Wallet.java (L2522-2524)
```java
  public BytesMessage getSpendingKey() throws ZksnarkException {
    checkAllowShieldedTransactionApi();

```

**File:** framework/src/main/java/org/tron/core/Wallet.java (L4126-4130)
```java
  public DecryptNotesTRC20 scanShieldedTRC20NotesByOvk(long startNum, long endNum,
      byte[] ovk, byte[] shieldedTRC20ContractAddress)
      throws ZksnarkException, BadItemException {
    checkAllowShieldedTransactionApi();

```

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L892-895)
```java
    if (isShieldedTransaction(trx.getInstance()) && !chainBaseManager.getDynamicPropertiesStore()
        .supportShieldedTransaction()) {
      throw new ContractValidateException("ShieldedTransferContract is not supported.");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ShieldedTransferActuator.java (L214-222)
```java
    if (dynamicStore.getAllowSameTokenName() != 1) {
      throw new ContractValidateException("shielded transaction is not allowed before "
          + "ALLOW_SAME_TOKEN_NAME is opened by the committee");
    }

    if (!dynamicStore.supportShieldedTransaction()) {
      throw new ContractValidateException("Not support Shielded Transaction, need to be opened by"
          + " the committee");
    }
```

**File:** framework/src/test/java/org/tron/core/actuator/ShieldedTransferActuatorTest.java (L1353-1404)
```java
  /**
   * Test that shielded transfer transaction validation works even when
   * allowShieldedTransactionApi is disabled. This verifies that the API flag
   * only gates wallet/helper APIs, not the core transaction validation logic.
   */
  @Test
  public void shieldedTransferValidationWorksWhenApiDisabled() {
    boolean orig = Args.getInstance().isAllowShieldedTransactionApi();
    // Disable the shielded API (this should NOT affect transaction validation)
    Args.getInstance().setAllowShieldedTransactionApi(false);

    dbManager.getDynamicPropertiesStore().saveAllowShieldedTransaction(1);
    dbManager.getDynamicPropertiesStore().saveTotalShieldedPoolValue(AMOUNT);

    try {
      ZenTransactionBuilder builder = new ZenTransactionBuilder(wallet);
      SpendingKey sk = SpendingKey.random();
      ExpandedSpendingKey expsk = sk.expandedSpendingKey();
      PaymentAddress address = sk.defaultAddress();
      Note note = new Note(address, AMOUNT);
      IncrementalMerkleVoucherContainer voucher = createSimpleMerkleVoucherContainer(note.cm());
      byte[] anchor = voucher.root().getContent().toByteArray();
      dbManager.getMerkleContainer()
          .putMerkleTreeIntoStore(anchor, voucher.getVoucherCapsule().getTree());
      builder.addSpend(expsk, note, anchor, voucher);

      addZeroValueOutputNote(builder);

      long fee = dbManager.getDynamicPropertiesStore().getShieldedTransactionCreateAccountFee();
      String addressNotExist =
          Wallet.getAddressPreFixString() + "8ba2aaae540c642e44e3bed5522c63bbc21f0000";

      builder.setTransparentOutput(ByteArray.fromHexString(addressNotExist), AMOUNT - fee);

      TransactionCapsule transactionCap = builder.build();
      Contract contract =
          transactionCap.getInstance().toBuilder().getRawDataBuilder().getContract(0);
      ShieldedTransferActuator actuator = new ShieldedTransferActuator();
      actuator.setChainBaseManager(dbManager.getChainBaseManager()).setContract(contract)
          .setTx(transactionCap);

      // Validation should succeed even when API is disabled
      actuator.validate();
    } catch (ContractValidateException e) {
      Assert.fail("Shielded transfer validation should not throw ContractValidateException: "
          + e.getMessage());
    } catch (Exception e) {
      Assert.fail("Shielded transfer should not throw Exception: " + e.getMessage());
    } finally {
      Args.getInstance().setAllowShieldedTransactionApi(orig);
    }
  }
```
