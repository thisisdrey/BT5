`payStandbyWitness()` (in [1](#0-0)  is invoked during every maintenance cycle to distribute the 127-standby-witness reward, and it calls `WitnessStore.getWitnessStandby()`, which retrieves **every** witness record via `getAllWitnesses()` and runs a full `Comparator`-based sort (`WitnessStore.sortWitnesses`) over the entire, unbounded witness set before truncating to `WITNESS_STANDBY_LENGTH`.

### Title
Unbounded witness-list sort in `WitnessStore.getWitnessStandby` is reachable by anyone creating witness accounts, enabling maintenance-cycle CPU griefing - (File: `chainbase/src/main/java/org/tron/core/store/WitnessStore.java`)

### Summary
`WitnessCreateActuator.validate()` places no cap on the total number of witnesses that can exist on-chain — it only checks that the caller's balance covers `getAccountUpgradeCost()` (a fixed TRX fee) and that the address has not already registered as a witness [2](#0-1) . Any account holding enough TRX can freely broadcast `WitnessCreateContract` transactions, so the size of the witness set stored in `WitnessStore` is entirely attacker-controllable and unbounded, unlike Union Finance's `MAX_TRUST_LIMIT`-capped staker/borrower lists.

### Finding Description
`WitnessStore.getWitnessStandby(boolean)` loads **all** witnesses with `getAllWitnesses()` and then calls `sortWitnesses(all, isSortOpt)`, which performs a full comparator sort (`Comparator.comparingLong(...).reversed().thenComparing(...)`) over the entire list before slicing the top `WITNESS_STANDBY_LENGTH` entries [3](#0-2) . This is directly analogous to the Union Finance `SumOfTrust.sol`/`CreditLimitByMedian.sol` pattern: an expensive sort over a list whose length is not actually capped by validation, unlike the promised `MAX_TRUST_LIMIT`.

This sort is invoked from `MortgageService.payStandbyWitness()`, which is called every maintenance cycle (block application path via the consensus/maintenance manager) to pay the top-127 standby witnesses [1](#0-0) . It is also directly exposed through the read-only RPC/HTTP API path `Wallet.getPaginatedNowWitnessList`, which likewise loads and sorts the full witness list on every call [4](#0-3) .

Because `WitnessCreateActuator` does not bound the total witness count, and the upgrade cost is a fixed, attacker-affordable fee (config default, not scaling with existing witness count), an attacker with sufficient TRX can register an arbitrarily large number of witness accounts, inflating the list that `getAllWitnesses()`/`sortWitnesses` must process on every maintenance cycle and on every `getPaginatedNowWitnessList` API call.

### Impact Explanation
Because `payStandbyWitness()` runs unconditionally as part of block/maintenance processing (not gated by any resource/energy metering, unlike TVM contract calls), a sufficiently inflated witness set increases the CPU cost of every maintenance cycle across **all** full nodes and SRs, not just the caller. If the witness count grows large enough, this can slow block/maintenance processing on every node, degrading throughput or, in the worst case, causing maintenance cycles to overrun the block-production interval, risking missed blocks/liveness issues network-wide. The same unbounded structure is also directly exercised by the public query API `getPaginatedNowWitnessList`, which any anonymous API client can call at will, worsening resource consumption on any queried node.

### Likelihood Explanation
Likelihood is limited primarily by the fixed TRX cost of registering each new witness (`getAccountUpgradeCost()`), which raises the capital bar compared to the original Solidity report (where staking in was gated only by minimal on-chain participation). However, unlike Union Finance's `MAX_TRUST_LIMIT = 100` hard cap validated by testing, java-tron enforces **no cap at all** on the number of witnesses, so a well-funded attacker can grow the list without bound over time, and the impact scales with attacker capital rather than being capped by protocol design.

### Recommendation
Introduce an explicit maximum cap on the total number of registered witnesses (analogous to `MAX_TRUST_LIMIT`) enforced in `WitnessCreateActuator.validate()`, and/or replace the full-list sort in `WitnessStore.getWitnessStandby`/`sortWitnesses` and `Wallet.getPaginatedNowWitnessList` with a bounded selection algorithm (e.g., a fixed-size top-k heap) so that cost no longer scales unbounded with the number of registered witnesses. Benchmark maintenance-cycle and API latency against a realistic worst-case witness count to size the cap with margin, mirroring the Union Finance mitigation approach.

### Proof of Concept
1. Attacker funds N accounts, each with at least `getAccountUpgradeCost()` TRX (currently a fixed config value, e.g. 9999 TRX in typical configs).
2. Attacker broadcasts N `WitnessCreateContract` transactions from these accounts; `WitnessCreateActuator.validate()` accepts all of them since no witness-count ceiling exists [5](#0-4) .
3. Each maintenance cycle, `MortgageService.payStandbyWitness()` calls `witnessStore.getWitnessStandby(...)`, which now must sort N (attacker-inflated) witness entries instead of the expected small SR set [6](#0-5) .
4. Simultaneously, any anonymous client calling `getPaginatedNowWitnessList` via gRPC/HTTP forces the same full-list sort on the serving node [7](#0-6) .
5. As N grows, the CPU/time cost of these operations grows correspondingly, unbounded by any protocol-level cap.

### Citations

**File:** chainbase/src/main/java/org/tron/core/service/MortgageService.java (L53-67)
```java
  public void payStandbyWitness() {
    List<WitnessCapsule> witnessStandbys = witnessStore.getWitnessStandby(
        dynamicPropertiesStore.allowWitnessSortOptimization());
    long voteSum = witnessStandbys.stream().mapToLong(WitnessCapsule::getVoteCount).sum();
    if (voteSum < 1) {
      return;
    }
    long totalPay = dynamicPropertiesStore.getWitness127PayPerBlock();
    double eachVotePay = (double) totalPay / voteSum;
    for (WitnessCapsule w : witnessStandbys) {
      long pay = (long) (w.getVoteCount() * eachVotePay);
      payReward(w.getAddress().toByteArray(), pay);
      logger.debug("Pay {} stand reward {}.", Hex.toHexString(w.getAddress().toByteArray()), pay);
    }
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/WitnessCreateActuator.java (L53-109)
```java
  @Override
  public boolean validate() throws ContractValidateException {
    if (this.any == null) {
      throw new ContractValidateException(ActuatorConstant.CONTRACT_NOT_EXIST);
    }
    if (chainBaseManager == null) {
      throw new ContractValidateException(ActuatorConstant.STORE_NOT_EXIST);
    }
    AccountStore accountStore = chainBaseManager.getAccountStore();
    DynamicPropertiesStore dynamicStore = chainBaseManager.getDynamicPropertiesStore();
    WitnessStore witnessStore = chainBaseManager.getWitnessStore();
    if (!this.any.is(WitnessCreateContract.class)) {
      throw new ContractValidateException(
          "contract type error, expected type [WitnessCreateContract],real type[" + any
              .getClass() + "]");
    }
    final WitnessCreateContract contract;
    try {
      contract = this.any.unpack(WitnessCreateContract.class);
    } catch (InvalidProtocolBufferException e) {
      throw new ContractValidateException(e.getMessage());
    }

    byte[] ownerAddress = contract.getOwnerAddress().toByteArray();
    String readableOwnerAddress = StringUtil.createReadableString(ownerAddress);

    if (!DecodeUtil.addressValid(ownerAddress)) {
      throw new ContractValidateException("Invalid address");
    }

    if (!TransactionUtil.validUrl(contract.getUrl().toByteArray())) {
      throw new ContractValidateException("Invalid url");
    }

    AccountCapsule accountCapsule = accountStore.get(ownerAddress);

    if (accountCapsule == null) {
      throw new ContractValidateException("account[" + readableOwnerAddress
          + ActuatorConstant.NOT_EXIST_STR);
    }
    /* todo later
    if (ArrayUtils.isEmpty(accountCapsule.getAccountName().toByteArray())) {
      throw new ContractValidateException("accountStore name not set");
    } */

    if (witnessStore.has(ownerAddress)) {
      throw new ContractValidateException(
          WITNESS_EXCEPTION_STR + readableOwnerAddress + "] has existed");
    }

    if (accountCapsule.getBalance() < dynamicStore
        .getAccountUpgradeCost()) {
      throw new ContractValidateException("balance < AccountUpgradeCost");
    }

    return true;
  }
```

**File:** chainbase/src/main/java/org/tron/core/store/WitnessStore.java (L44-63)
```java
  public List<WitnessCapsule> getWitnessStandby(boolean isSortOpt) {
    List<WitnessCapsule> ret;
    List<WitnessCapsule> all = getAllWitnesses();
    sortWitnesses(all, isSortOpt);
    if (all.size() > Parameter.ChainConstant.WITNESS_STANDBY_LENGTH) {
      ret = new ArrayList<>(all.subList(0, Parameter.ChainConstant.WITNESS_STANDBY_LENGTH));
    } else {
      ret = new ArrayList<>(all);
    }
    // trim voteCount = 0
    ret.removeIf(w -> w.getVoteCount() < 1);
    return ret;
  }

  public static void sortWitnesses(List<WitnessCapsule> witnesses, boolean isSortOpt) {
    witnesses.sort(Comparator.comparingLong(WitnessCapsule::getVoteCount).reversed()
        .thenComparing(isSortOpt
            ? Comparator.comparing(WitnessCapsule::createReadableString).reversed()
            : Comparator.comparingInt((WitnessCapsule w) -> w.getAddress().hashCode()).reversed()));
  }
```

**File:** framework/src/main/java/org/tron/core/Wallet.java (L793-815)
```java
    List<WitnessCapsule> witnessCapsuleList = chainBaseManager.getWitnessStore().getAllWitnesses();
    if (offset >= witnessCapsuleList.size()) {
      return null;
    }

    VotesStore votesStore = chainBaseManager.getVotesStore();
    // Count the vote changes for each witness in the current epoch, it is maybe negative.
    Map<ByteString, Long> countWitness = countVote(votesStore);

    // Iterate through the witness list to apply vote changes and calculate the real-time vote count
    witnessCapsuleList.forEach(witnessCapsule -> {
      long voteCount = countWitness.getOrDefault(witnessCapsule.getAddress(), 0L);
      witnessCapsule.setVoteCount(witnessCapsule.getVoteCount() + voteCount);
    });

    // Use the same sorting logic as in the Maintenance period
    WitnessStore.sortWitnesses(witnessCapsuleList,
        chainBaseManager.getDynamicPropertiesStore().allowWitnessSortOptimization());

    List<WitnessCapsule> sortedWitnessList = witnessCapsuleList.stream()
        .skip(offset)
        .limit(limit)
        .collect(Collectors.toList());
```
