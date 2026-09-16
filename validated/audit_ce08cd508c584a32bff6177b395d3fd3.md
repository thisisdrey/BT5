### Title
Unbounded `toAccounts`/`fromAccounts` growth in `DelegatedResourceAccountIndexCapsule` enables OUT_OF_GAS/latency DoS on delegation index reads and writes - (File: actuator/src/main/java/org/tron/core/actuator/FreezeBalanceActuator.java)

### Summary
When the legacy delegation-index path is active (`!dynamicPropertiesStore.supportAllowDelegateOptimization()`), `FreezeBalanceActuator.delegateResource()` appends a new entry to an account's `DelegatedResourceAccountIndexCapsule.toAccounts`/`fromAccounts` list every time the account delegates bandwidth/energy to a previously-unseen receiver address, with no upper bound on list size, mirroring the reported `activeProtectionIndexes` growth pattern (an unbounded per-account collection that gets rebuilt/iterated with each mutating call).

### Finding Description
`FreezeBalanceActuator.delegateResource()` reads the owner's and receiver's `DelegatedResourceAccountIndexCapsule`, and — if the target address is not already present — appends it via `ownerIndexCapsule.addToAccount(...)` / `receiverIndexCapsule.addFromAccount(...)`: [1](#0-0) 

`addToAccount`/`addFromAccount` in the capsule perform a full protobuf rebuild (`toBuilder().addToAccounts(...).build()`) each call, meaning cost per delegation grows with the current list size, and there is no maximum-size check anywhere in the validate/execute path (unlike `VoteWitnessActuator`, which enforces `MAX_VOTE_NUMBER` on the votes list): [2](#0-1) 

An attacker who controls only an ordinary account can repeatedly:
1. Freeze a small `frozenBalance` (dust amount is sufficient) via `FreezeBalanceContract` with `getDelegatedContractForBandwidth`/`getDelegatedContractForCpu`, using a new, unique receiver address each time.
2. Because the receiver address is new, the `contains()` check fails and a new entry is unconditionally appended to the owner's `toAccountsList` (and the new receiver's `fromAccountsList`).

There is no limit analogous to `MAX_VOTE_NUMBER` for votes, so this list can be grown to an arbitrary size purely by paying the (fixed, small) TRX cost of `FreezeBalanceContract` transactions, similar to how the report describes growing `activeProtectionIndexes` cheaply via many small protection purchases. The existing test `testMultiFreezeDelegatedBalanceForBandwidth` demonstrates this growth mechanically (100+ entries added without any cap enforced): [3](#0-2) 

Once large, this list is:
- Rewritten (full copy) on every subsequent `FreezeBalance`/`UnfreezeBalance` mutation touching that account (`UnfreezeBalanceActuator` also rebuilds `toAccountsList`/`fromAccountsList` via a new `ArrayList` and `setAllToAccounts`/`setAllFromAccounts`), making per-call cost grow linearly with the accumulated list size: [4](#0-3) 
- Fully returned by unauthenticated read APIs (`getDelegatedResourceAccountIndex` over gRPC/HTTP), forcing the node to serialize and transmit the entire unbounded list on every query: [5](#0-4) [6](#0-5) 

### Impact Explanation
This does not directly cause `OUT_OF_GAS` for a fixed-energy VM opcode the way the Solidity report describes (java-tron actuators are not gas-metered the same way EVM contract calls are for state actuators), but it does create a growth pattern where a single account's `DelegatedResourceAccountIndex` value becomes very large. Effects:
- Every subsequent freeze/delegate/unfreeze transaction from that account (or targeting that account as receiver) becomes progressively more expensive to process (protobuf rebuild cost scales with list size), degrading throughput for that account's transactions and increasing block-processing time proportional to the attack.
- The `getDelegatedResourceAccountIndex` query API (reachable by any anonymous client) returns the full list, so a large-enough list increases response payload size and processing/serialization cost per query, which can be repeatedly triggered for free by any API caller, amplifying node resource consumption.
- This falls into the "resource-exhaustion via unbounded per-account collection with no cap, reachable by an unprivileged transaction sender" bug class, analogous to the reported issue, though the concrete consequence here is degraded processing/response latency rather than a guaranteed permanent revert of a state-manager function (java-tron has no equivalent global `lockCapital()`-style function that must iterate this specific list to completion for correctness).

### Likelihood Explanation
- Reachability depends on `supportAllowDelegateOptimization()` being false. This is a committee-controlled `DynamicPropertiesStore` flag (`ALLOW_DELEGATE_OPTIMIZATION` proposal), and I was not able to confirm from the indexed code whether this optimization is enabled by default on mainnet today; if it is enabled, delegation instead uses per-pair keys (`delegate()`/`convert()` in `DelegatedResourceAccountIndexStore`) which avoids the monolithic-list growth and this specific vector is not exploitable in that configuration.
- `FreezeBalanceContract` itself is part of the legacy (pre "new resource model") freezing mechanism; if it has been fully deprecated/disabled on the live network in favor of `FreezeBalanceV2`/`DelegateResourceActuator` (which use the bounded per-pair `V2_FROM_PREFIX`/`V2_TO_PREFIX` DB-key scheme, not a monolithic list), the vulnerable code path may be effectively unreachable.
- Because of this uncertainty about live configuration, and because the impact (linear-cost growth / larger query payloads) is weaker than a guaranteed permanent-revert/DoS, this should be treated as a **potential/conditional** finding pending confirmation of `AllowDelegateOptimization`'s live-network default and whether legacy `FreezeBalanceContract` calls remain accepted.

### Recommendation
- Enforce a maximum size (analogous to `MAX_VOTE_NUMBER`) on `DelegatedResourceAccountIndexCapsule.toAccountsList`/`fromAccountsList` in `FreezeBalanceActuator.validate()`/`delegateResource()` when the legacy (non-optimized) delegation-index path is active.
- Confirm whether `FreezeBalanceContract` and the non-optimized delegation-index path are still reachable on mainnet; if `AllowDelegateOptimization` is already permanently enabled and legacy freeze is disabled, this reduces to a historical/latent issue rather than a live exploit path.
- If reachable, consider migrating fully to the bounded per-pair key scheme (`V2_FROM_PREFIX`/`V2_TO_PREFIX`) used by `DelegateResourceProcessor`, removing the monolithic list representation entirely.

### Proof of Concept
1. Ensure `supportAllowDelegateOptimization()` is false (default/legacy state) and `FreezeBalanceContract` is accepted.
2. From a single funded account, repeatedly submit `FreezeBalanceContract` transactions with `receiver_address` set to a freshly generated address each time and a small `frozen_balance` (dust amount), each with `delegate` semantics (as exercised by `getDelegatedContractForBandwidth`): [7](#0-6) 
3. Each call appends to `ownerIndexCapsule.toAccountsList` with no cap, as verified by `testMultiFreezeDelegatedBalanceForBandwidth` growing the list to 100+ entries with no rejection.
4. Repeat at scale; observe growing transaction processing cost for subsequent freeze/unfreeze operations on the account and growing response size/latency for `getDelegatedResourceAccountIndex` queries against that address.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/FreezeBalanceActuator.java (L319-345)
```java
    //modify DelegatedResourceAccountIndexStore
    if (!dynamicPropertiesStore.supportAllowDelegateOptimization()) {

      DelegatedResourceAccountIndexCapsule ownerIndexCapsule =
          delegatedResourceAccountIndexStore.get(ownerAddress);
      if (ownerIndexCapsule == null) {
        ownerIndexCapsule = new DelegatedResourceAccountIndexCapsule(
            ByteString.copyFrom(ownerAddress));
      }
      List<ByteString> toAccountsList = ownerIndexCapsule.getToAccountsList();
      if (!toAccountsList.contains(ByteString.copyFrom(receiverAddress))) {
        ownerIndexCapsule.addToAccount(ByteString.copyFrom(receiverAddress));
      }
      delegatedResourceAccountIndexStore.put(ownerAddress, ownerIndexCapsule);

      DelegatedResourceAccountIndexCapsule receiverIndexCapsule
          = delegatedResourceAccountIndexStore.get(receiverAddress);
      if (receiverIndexCapsule == null) {
        receiverIndexCapsule = new DelegatedResourceAccountIndexCapsule(
            ByteString.copyFrom(receiverAddress));
      }
      List<ByteString> fromAccountsList = receiverIndexCapsule
          .getFromAccountsList();
      if (!fromAccountsList.contains(ByteString.copyFrom(ownerAddress))) {
        receiverIndexCapsule.addFromAccount(ByteString.copyFrom(ownerAddress));
      }
      delegatedResourceAccountIndexStore.put(receiverAddress, receiverIndexCapsule);
```

**File:** chainbase/src/main/java/org/tron/core/capsule/DelegatedResourceAccountIndexCapsule.java (L57-61)
```java
  public void addFromAccount(ByteString fromAccount) {
    this.delegatedResourceAccountIndex = this.delegatedResourceAccountIndex.toBuilder()
        .addFromAccounts(fromAccount)
        .build();
  }
```

**File:** framework/src/test/java/org/tron/core/actuator/FreezeBalanceActuatorTest.java (L275-296)
```java
  @Test
  public void testMultiFreezeDelegatedBalanceForBandwidth() {
    dbManager.getDynamicPropertiesStore().saveAllowDelegateResource(1);
    dbManager.getDynamicPropertiesStore().saveAllowDelegateOptimization(1L);
    dbManager.getDynamicPropertiesStore().saveLatestBlockHeaderTimestamp(10000L);
    long frozenBalance = 1_000_000_000L;
    long duration = 3;
    final int RECEIVE_COUNT = 100;
    String[] RECEIVE_ADDRESSES = new String[RECEIVE_COUNT + 1];

    DelegatedResourceAccountIndexCapsule ownerIndexCapsule =
        new DelegatedResourceAccountIndexCapsule(
            ByteString.copyFrom(ByteArray.fromHexString(OWNER_ADDRESS)));
    for (int i = 0; i < RECEIVE_COUNT + 1; i++) {
      ECKey ecKey = new ECKey(Utils.getRandom());
      RECEIVE_ADDRESSES[i] = ByteArray.toHexString(ecKey.getAddress());
      if (i != RECEIVE_COUNT) {
        ownerIndexCapsule.addToAccount(ByteString.copyFrom(ecKey.getAddress()));
      }
    }
    dbManager.getDelegatedResourceAccountIndexStore().put(
        ByteArray.fromHexString(OWNER_ADDRESS), ownerIndexCapsule);
```

**File:** actuator/src/main/java/org/tron/core/actuator/UnfreezeBalanceActuator.java (L162-182)
```java
        //modify DelegatedResourceAccountIndexStore
        if (!dynamicStore.supportAllowDelegateOptimization()) {
          DelegatedResourceAccountIndexCapsule ownerIndexCapsule =
              delegatedResourceAccountIndexStore.get(ownerAddress);
          if (ownerIndexCapsule != null) {
            List<ByteString> toAccountsList = new ArrayList<>(ownerIndexCapsule
                .getToAccountsList());
            toAccountsList.remove(ByteString.copyFrom(receiverAddress));
            ownerIndexCapsule.setAllToAccounts(toAccountsList);
            delegatedResourceAccountIndexStore.put(ownerAddress, ownerIndexCapsule);
          }

          DelegatedResourceAccountIndexCapsule receiverIndexCapsule =
              delegatedResourceAccountIndexStore.get(receiverAddress);
          if (receiverIndexCapsule != null) {
            List<ByteString> fromAccountsList = new ArrayList<>(receiverIndexCapsule
                .getFromAccountsList());
            fromAccountsList.remove(ByteString.copyFrom(ownerAddress));
            receiverIndexCapsule.setAllFromAccounts(fromAccountsList);
            delegatedResourceAccountIndexStore.put(receiverAddress, receiverIndexCapsule);
          }
```

**File:** framework/src/main/java/org/tron/core/Wallet.java (L1040-1051)
```java
  public DelegatedResourceAccountIndex getDelegatedResourceAccountIndex(ByteString address) {
    if (address == null || address.size() != DecodeUtil.ADDRESS_SIZE / 2) {
      return DelegatedResourceAccountIndex.getDefaultInstance();
    }
    DelegatedResourceAccountIndexCapsule accountIndexCapsule =
        chainBaseManager.getDelegatedResourceAccountIndexStore().getIndex(address.toByteArray());
    if (accountIndexCapsule != null) {
      return accountIndexCapsule.getInstance();
    } else {
      return DelegatedResourceAccountIndex.getDefaultInstance();
    }
  }
```

**File:** framework/src/main/java/org/tron/core/services/RpcApiService.java (L528-538)
```java
    @Override
    public void getDelegatedResourceAccountIndex(BytesMessage request,
        StreamObserver<org.tron.protos.Protocol.DelegatedResourceAccountIndex> responseObserver) {
      try {
        responseObserver
          .onNext(wallet.getDelegatedResourceAccountIndex(request.getValue()));
      } catch (Exception e) {
        responseObserver.onError(getRunTimeException(e));
      }
      responseObserver.onCompleted();
    }
```
