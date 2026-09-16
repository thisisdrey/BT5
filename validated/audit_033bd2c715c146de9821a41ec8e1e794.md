### Title
Node-level ALLOW_SHIELDED_TRANSACTION_API restriction is not enforced in ShieldedTransferActuator, letting anyone broadcast shielded transactions the node operator intended to disable - ([File: actuator/src/main/java/org/tron/core/actuator/ShieldedTransferActuator.java])

### Summary
Similar to the CloudStack `extraconfig` bug — where a feature meant to be gated by an administrator-controlled switch could be invoked directly regardless of that switch — `java-tron` has a node-level flag `allowShieldedTransactionApi` (`isAllowShieldedTransactionApi()`) that is documented/used to disable shielded-transaction *API* functionality on a given node, but the core transaction path (`ShieldedTransferActuator.validate()`/`execute()`) never checks it. Only the chain-wide committee flag `ALLOW_SHIELDED_TRANSACTION` is enforced there.

### Finding Description
`ShieldedTransferActuator.validate()` only checks the on-chain, committee-controlled dynamic property: [1](#0-0) 

The node-level `Args`/`NodeConfig` setting `allowShieldedTransactionApi` exists specifically to let an operator restrict shielded-transaction handling on their own node (e.g. for legal/compliance/local policy reasons), but this flag is consumed only by `Wallet.java`'s helper/API methods that build/service shielded requests — not by the actuator itself. This is explicitly confirmed by a test that documents the gap: [2](#0-1) 

Because a raw, already-constructed `ShieldedTransferContract` transaction can be submitted through the normal broadcast path (which invokes the actuator directly, not the gated `Wallet` helper methods), an attacker/anonymous client can bypass the node operator's `allowShieldedTransactionApi=false` setting entirely and still have their shielded transaction validated and executed on that node, as long as the network-wide `ALLOW_SHIELDED_TRANSACTION` committee flag is on. This mirrors the CloudStack issue: a feature intended to be gated by an administrative switch (`extraconfig` enablement / here, the node's local API switch) can be reached and exercised by an ordinary, unprivileged caller who supplies the configuration/payload directly instead of going through the gated administrative path.

I was unable to fully confirm within index limits whether `Wallet.java`'s `broadcastTransaction` path is completely disjoint from the flag-checking helper methods (the `Wallet.java` broadcast method could not be located in the index due to size limits), so the exact reachability of "direct broadcast bypassing the API flag" should be verified against the full source before treating this as certain — I recommend starting a Devin session with full repo access to confirm the broadcast/dispatch call graph.

### Impact Explanation
If confirmed, this allows an unprivileged transaction broadcaster to force a node to process shielded (zk-SNARK) transactions that its operator explicitly disabled at the node/API level, defeating an administrator-configured compliance/security control. Depending on deployment intent (e.g., nodes required by local regulation to reject privacy transactions), this could expose the operator to compliance violations, and, more concerning from a chain-integrity standpoint, causes inconsistent enforcement of a supposedly node-wide restriction across broadcastTransaction vs. wallet-API paths.

### Likelihood Explanation
Likelihood is low-to-medium: it requires network-wide `ALLOW_SHIELDED_TRANSACTION` to already be enacted by the committee (a precondition outside attacker control), and it requires an operator who has deliberately set `allowShieldedTransactionApi=false` expecting it to fully block shielded transaction processing. The test in the repo already demonstrates the gap exists and is by-design/known ("This verifies that the API flag only gates wallet/helper APIs, not the core transaction validation logic"), suggesting this may be intentional behavior rather than an unrecognized vulnerability, which reduces confidence this is an unpatched bug versus a documented design decision.

### Recommendation
Clarify the intended semantics of `allowShieldedTransactionApi`. If it is meant to fully disable shielded-transaction processing on a node (not just the convenience wallet API), enforce the check inside `ShieldedTransferActuator.validate()` (and any other entry point that can execute a `ShieldedTransferContract`), not only in `Wallet.java` helper methods. If it is only meant to gate the higher-level RPC/HTTP helper endpoints (by design), then this is not a vulnerability and documentation should make that explicit to avoid operator confusion.

### Proof of Concept
Given the existing test already demonstrates this at the unit level: [3](#0-2) 
1. Node operator sets `allowShieldedTransactionApi = false` in node config, intending to disable shielded transaction support on this node.
2. Committee has already enacted `ALLOW_SHIELDED_TRANSACTION = 1` network-wide.
3. An attacker builds a valid `ShieldedTransferContract` transaction offline (using any node where the API is enabled, or their own tooling) and broadcasts the raw transaction to the restricted node via the standard broadcast path.
4. `ShieldedTransferActuator.validate()`/`execute()` only checks `dynamicStore.supportShieldedTransaction()`, ignoring the node's local `allowShieldedTransactionApi` setting, so the transaction is accepted and processed despite the operator's restriction.

### Citations

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
