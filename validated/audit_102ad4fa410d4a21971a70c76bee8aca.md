[1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L148-151)
```java
  @Override
  public boolean validate() throws ContractValidateException {

    if (chainBaseManager == null) {
```

**File:** framework/src/main/java/org/tron/common/runtime/RuntimeImpl.java (L52-60)
```java
    if (actuator2 != null) {
      actuator2.validate(context);
      actuator2.execute(context);
    } else {
      for (Actuator act : actuatorList) {
        act.validate();
        act.execute(context.getProgramResult().getRet());
      }
    }
```

**File:** framework/src/test/java/org/tron/core/db/ManagerTest.java (L890-929)
```java
  @Test
  public void getVerifyTxsSkipsBlockWhenPermissionTxAlreadyConsumed() throws Exception {
    // Scenario: a permission-change tx (A) for owner X has been processed and consumed,
    // so it is no longer in pendingTransactions but ownerAddressSet still contains X.
    // A later transfer tx (B) from X with the old signature enters pending with
    // isVerified=true. A malicious SR produces a block containing only B (no A).
    // getVerifyTxs must place B into the re-verify list rather than calling
    // setVerified(true) just because B matches the pending entry.
    TransferContract bContract = TransferContract.newBuilder()
        .setOwnerAddress(ByteString.copyFrom("f1".getBytes()))
        .setAmount(7).build();
    TransactionCapsule bTx = new TransactionCapsule(bContract, ContractType.TransferContract);
    String hexOwner = ByteArray.toHexString("f1".getBytes());

    dbManager.getPendingTransactions().clear();
    dbManager.getPendingTransactions().add(bTx);

    Field field = Manager.class.getDeclaredField("ownerAddressSet");
    field.setAccessible(true);
    @SuppressWarnings("unchecked")
    Set<String> ownerAddressSet = (Set<String>) field.get(dbManager);
    Set<String> backup = new HashSet<>(ownerAddressSet);
    ownerAddressSet.clear();
    ownerAddressSet.add(hexOwner);

    try {
      List<Transaction> blockTxs = new ArrayList<>();
      blockTxs.add(bTx.getInstance());
      BlockCapsule capsule = new BlockCapsule(0, ByteString.EMPTY, 0, blockTxs);

      List<TransactionCapsule> txs = dbManager.getVerifyTxs(capsule);

      Assert.assertEquals(1, txs.size());
      Assert.assertEquals(bTx.getTransactionId(), txs.get(0).getTransactionId());
    } finally {
      ownerAddressSet.clear();
      ownerAddressSet.addAll(backup);
      dbManager.getPendingTransactions().clear();
    }
  }
```
