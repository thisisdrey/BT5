### Title
Witness (SR candidate) impersonation via unchecked duplicate `url` in `WitnessCreateContract`/`WitnessUpdateContract` - (File: actuator/src/main/java/org/tron/core/actuator/WitnessCreateActuator.java)

### Summary
`WitnessCreateActuator.validate()` and `WitnessUpdateActuator.validate()` only enforce uniqueness on the witness's `owner_address` and syntactic validity of the `url` field (length/format via `TransactionUtil.validUrl`). Neither actuator checks whether the submitted `url` (the public identity/branding metadata voters and wallets use to recognize a Super Representative candidate) already belongs to another witness. Any account can therefore register or update itself as a witness using the exact same `url` as an existing, legitimate SR candidate, impersonating it in front of voters, in the same way the referenced Gitcoin `RoundImplementation.initialize()` bug allowed an attacker to deploy a round with identical metadata (`roundMetaPtr`) without verifying that the deployer legitimately owns that identity.

### Finding Description
`WitnessCreateActuator.validate()` performs: [1](#0-0) 
It validates the `url` is well-formed (`TransactionUtil.validUrl`) and checks only that `witnessStore.has(ownerAddress)` is false (i.e., that this owner address hasn't already registered as a witness). It never checks whether any other witness in `WitnessStore` already uses the same `url`.

Similarly, `WitnessUpdateActuator.validate()`: [2](#0-1) 
only validates the URL's format and that the caller already owns a witness record — it does not check for collisions with other witnesses' URLs either.

Because the `url` is the field exposed by wallets/exchanges/voting UIs to identify and brand an SR candidate (the same role `roundMetaPtr`/`applicationMetaPtr` play for a Gitcoin round in the referenced report), an attacker can:
1. Create a new account and fund it enough to cover `AccountUpgradeCost`.
2. Submit a `WitnessCreateContract` (or later a `WitnessUpdateContract`) copying the exact `url` of a legitimate, already-elected SR candidate.
3. The chain accepts the doppelganger witness under a different `owner_address` with identical public identity metadata, with nothing in `execute()`/`createWitness()` differentiating it either: [3](#0-2) 

### Impact Explanation
Wallets, voting dashboards, and exchanges typically render SR candidates using the `url` metadata to help users choose whom to vote for. A perfect metadata clone under a different address lets an attacker siphon votes (`VoteWitnessContract`) that were intended for the legitimate SR toward the attacker's address, diverting DPoS voting power and the associated block-reward/voting-reward distribution away from the honest candidate to the impersonator — analogous to the original report's "votes on the doppelganger contract will not count towards distribution of the honest round." This constitutes unauthorized redirection of value (voting rewards / influence over consensus) achievable by any funded account, without compromising any key.

### Likelihood Explanation
The attack requires only a single signed `WitnessCreateContract` (or `WitnessUpdateContract`) transaction from an attacker-controlled, funded account — no special privilege, no cryptographic compromise, and no cooperation from the victim SR is needed. The `AccountUpgradeCost` fee is the only economic bar, which is a fixed, publicly known cost, making the attack straightforward and repeatable against any witness whose `url` is public (which is a public and required field for every SR/candidate).

### Recommendation
Add a uniqueness check on `url` in both `WitnessCreateActuator.validate()` and `WitnessUpdateActuator.validate()`, e.g., iterate `WitnessStore` (or maintain a secondary url→address index similar to `AccountIndexStore`) and reject the transaction with a `ContractValidateException` if any other witness already uses the same `url`. This mirrors the existing uniqueness enforcement pattern already used for account names in `UpdateAccountActuator` (`AccountIndexStore.has(accountName)`).

### Proof of Concept
1. Legitimate SR "Alice" is registered: `WitnessCreateContract{owner_address=Alice, url="https://alice-sr.example"}` → succeeds, `witnessStore` now has `Alice → url="https://alice-sr.example"`.
2. Attacker funds a fresh account `Mallory` with balance ≥ `dynamicStore.getAccountUpgradeCost()`.
3. Attacker submits `WitnessCreateContract{owner_address=Mallory, url="https://alice-sr.example"}`.
4. `WitnessCreateActuator.validate()` passes: `DecodeUtil.addressValid` OK, `TransactionUtil.validUrl` OK (same string as Alice's, already valid), `witnessStore.has(Mallory)` is false (no dedup check on `url`), account exists, balance sufficient.
5. `execute()`/`createWitness()` stores a second witness entry `Mallory → url="https://alice-sr.example"` with no relation checked to Alice's entry: [4](#0-3) 
6. Wallets/voting UIs surfacing candidates by `url` now show two entries with identical branding at different addresses; voters who intend to vote for Alice may end up casting `VoteWitnessContract` votes for Mallory's address instead, diverting voting weight/rewards to the attacker.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/WitnessCreateActuator.java (L83-101)
```java
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
```

**File:** actuator/src/main/java/org/tron/core/actuator/WitnessCreateActuator.java (L121-149)
```java
  private void createWitness(final WitnessCreateContract witnessCreateContract)
      throws BalanceInsufficientException {
    AccountStore accountStore = chainBaseManager.getAccountStore();
    DynamicPropertiesStore dynamicStore = chainBaseManager.getDynamicPropertiesStore();
    WitnessStore witnessStore = chainBaseManager.getWitnessStore();
    //Create Witness by witnessCreateContract
    final WitnessCapsule witnessCapsule = new WitnessCapsule(
        witnessCreateContract.getOwnerAddress(),
        0,
        witnessCreateContract.getUrl().toStringUtf8());

    logger.debug("createWitness,address[{}]", witnessCapsule.createReadableString());
    witnessStore.put(witnessCapsule.createDbKey(), witnessCapsule);
    AccountCapsule accountCapsule = accountStore
        .get(witnessCapsule.createDbKey());
    accountCapsule.setIsWitness(true);
    if (dynamicStore.getAllowMultiSign() == 1) {
      accountCapsule.setDefaultWitnessPermission(dynamicStore);
    }
    accountStore.put(accountCapsule.createDbKey(), accountCapsule);
    long cost = dynamicStore.getAccountUpgradeCost();
    adjustBalance(accountStore, witnessCreateContract.getOwnerAddress().toByteArray(), -cost);
    if (dynamicStore.supportBlackHoleOptimization()) {
      dynamicStore.burnTrx(cost);
    } else {
      adjustBalance(accountStore, accountStore.getBlackhole(), +cost);
    }
    dynamicStore.addTotalCreateWitnessCost(cost);
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/WitnessUpdateActuator.java (L83-93)
```java
    if (!accountStore.has(ownerAddress)) {
      throw new ContractValidateException("account does not exist");
    }

    if (!TransactionUtil.validUrl(contract.getUpdateUrl().toByteArray())) {
      throw new ContractValidateException("Invalid url");
    }

    if (!witnessStore.has(ownerAddress)) {
      throw new ContractValidateException("Witness does not exist");
    }
```
