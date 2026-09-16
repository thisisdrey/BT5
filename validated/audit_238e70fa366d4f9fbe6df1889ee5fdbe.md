### Title
Instant, unbounded change to `consume_user_resource_percent` via `UpdateSettingContractActuator` with no timelock or bound on delta - ([File: actuator/src/main/java/org/tron/core/actuator/UpdateSettingContractActuator.java])

### Summary
The reported issue is a missing timelock on `setPlatformFee()` in the Joyn contracts, which lets a privileged owner instantly change a fee parameter that other users are economically exposed to. The closest reachable analog in java-tron is `UpdateSettingContractActuator`, which lets a smart-contract's origin/owner instantly change `consume_user_resource_percent` — the percentage of energy cost passed on to callers of that contract — with no delay, no bound on the magnitude of change, and no notice to users who are about to call the contract.

### Finding Description
`UpdateSettingContractActuator.execute()` unpacks an `UpdateSettingContract` and immediately writes the new `consume_user_resource_percent` value into the `ContractCapsule` stored for the target contract address, taking effect on the very next transaction that calls the contract: [1](#0-0) 

`validate()` only checks that the caller is the contract's origin address and that the percentage lies in `[0, 100]` — it enforces no minimum change interval, no maximum step size, and no cooldown between updates: [2](#0-1) 

This is directly reachable from an unprivileged transaction broadcaster (the contract's deployer/origin address, which requires no special network privilege) via the `wallet/updatesetting` HTTP endpoint and the `UpdateSetting` gRPC/JSON-RPC path: [3](#0-2) 

The test suite confirms two consecutive updates to the same contract are accepted back-to-back with no restriction (`twiceUpdateSettingContract`), demonstrating there is no rate limit or timelock enforced anywhere in the actuator: [4](#0-3) 

The `consume_user_resource_percent` value directly determines how much of the energy cost of a TVM execution is charged to the calling account versus the contract owner, and is read during actual VM execution (e.g., `VMActuator.java`, referenced via `getConsumeUserResourcePercent`), meaning the effect is felt by any caller in the very next transaction after the update.

### Impact Explanation
Because there is no timelock or cooldown, a contract owner can:
- Advertise/operate a contract with a low `consume_user_resource_percent` (favorable to callers) to attract usage, then instantly raise it to 100 right before/at the same time users submit their transactions, forcing callers to pay unexpectedly higher energy fees for the same interaction they expected to be cheap.
- Flip the value transaction-by-transaction (front-running incoming calls), making the actual resource cost non-deterministic and unpredictable for any caller who cannot atomically bundle their read of the current setting with their contract call.

This does not itself let the contract owner steal TRX/TRC10 directly, but it lets them unilaterally and instantly shift resource-cost burden onto unsuspecting callers, which is an economic/DoS-style griefing vector against ordinary users interacting with the contract, with no chain-level protection (only application-level convention, since Solidity level "Ownable" wrappers around this are contract-specific, not enforced by java-tron protocol).

### Likelihood Explanation
Any contract owner (which requires no elevated node privilege — any account that deployed or owns a contract) can trigger this by simply broadcasting an `UpdateSettingContract` transaction, which is a normal signed transaction validated only by `UpdateSettingContractActuator.validate()`. No SR/witness/committee privilege is needed, and no delay is enforced, so exploitation likelihood is high whenever a contract owner has incentive to grief interacting accounts.

### Recommendation
Add a timelock/cooldown to `UpdateSettingContractActuator`:
- Store the timestamp of the last update to `consume_user_resource_percent` per contract in `ContractCapsule`/`ContractStore`.
- In `validate()`, reject updates that occur within a minimum interval (e.g., N blocks/hours) of the previous update.
- Optionally, cap the maximum delta permitted per update and/or require the new setting to take effect only after a delay (e.g., N blocks later) rather than immediately, giving callers time to observe/react to the change before it affects their transactions.

### Proof of Concept
1. Deploy a contract `C` with `consume_user_resource_percent = 0` (all energy cost borne by contract owner).
2. Wait for users to begin calling `C` expecting cheap execution.
3. As `C`'s owner, broadcast `UpdateSettingContract{contract_address=C, consume_user_resource_percent=100}` via `wallet/updatesetting` — per `UpdateSettingContractActuator.execute()` this is applied immediately to `ContractStore` with no delay: [1](#0-0) .
4. The very next call to `C` by any user is charged 100% of the energy cost, with no advance warning, as demonstrated by the immediate-effect assertions in `twiceUpdateSettingContract`: [4](#0-3) .

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/UpdateSettingContractActuator.java (L40-49)
```java
    try {
      UpdateSettingContract usContract = any.unpack(UpdateSettingContract.class);
      long newPercent = usContract.getConsumeUserResourcePercent();
      byte[] contractAddress = usContract.getContractAddress().toByteArray();
      ContractCapsule deployedContract = contractStore.get(contractAddress);

      contractStore.put(contractAddress, new ContractCapsule(
          deployedContract.getInstance().toBuilder().setConsumeUserResourcePercent(newPercent)
              .build()));
      RepositoryImpl.removeLruCache(contractAddress);
```

**File:** actuator/src/main/java/org/tron/core/actuator/UpdateSettingContractActuator.java (L93-113)
```java
    long newPercent = contract.getConsumeUserResourcePercent();
    if (newPercent > ActuatorConstant.ONE_HUNDRED || newPercent < 0) {
      throw new ContractValidateException(
          "percent not in [0, 100]");
    }

    byte[] contractAddress = contract.getContractAddress().toByteArray();
    ContractCapsule deployedContract = contractStore.get(contractAddress);

    if (deployedContract == null) {
      throw new ContractValidateException(
          "Contract does not exist");
    }

    byte[] deployedContractOwnerAddress = deployedContract.getInstance().getOriginAddress()
        .toByteArray();

    if (!Arrays.equals(ownerAddress, deployedContractOwnerAddress)) {
      throw new ContractValidateException(
          ACCOUNT_EXCEPTION_STR + readableOwnerAddress + "] is not the owner of the contract");
    }
```

**File:** framework/src/main/java/org/tron/core/services/http/UpdateSettingServlet.java (L26-41)
```java
  protected void doPost(HttpServletRequest request, HttpServletResponse response) {
    try {
      PostParams params = PostParams.getPostParams(request);
      UpdateSettingContract.Builder build = UpdateSettingContract.newBuilder();
      JsonFormat.merge(params.getParams(), build, params.isVisible());
      Transaction tx = wallet
          .createTransactionCapsule(build.build(), ContractType.UpdateSettingContract)
          .getInstance();
      JSONObject jsonObject = JSONObject.parseObject(params.getParams());
      tx = Util.setTransactionPermissionId(jsonObject, tx);

      response.getWriter().println(Util.printCreateTransaction(tx, params.isVisible()));
    } catch (Exception e) {
      Util.processError(e, response);
    }
  }
```

**File:** framework/src/test/java/org/tron/core/actuator/UpdateSettingContractActuatorTest.java (L227-266)
```java
  @Test
  public void twiceUpdateSettingContract() {
    UpdateSettingContractActuator actuator =
        new UpdateSettingContractActuator();
    actuator.setChainBaseManager(dbManager.getChainBaseManager())
        .setAny(getContract(OWNER_ADDRESS, CONTRACT_ADDRESS, TARGET_PERCENT));

    UpdateSettingContractActuator secondActuator =
        new UpdateSettingContractActuator();
    secondActuator.setChainBaseManager(dbManager.getChainBaseManager())
        .setAny(getContract(OWNER_ADDRESS, CONTRACT_ADDRESS, 90L));

    TransactionResultCapsule ret = new TransactionResultCapsule();
    try {
      // first
      actuator.validate();
      actuator.execute(ret);

      Assert.assertEquals(ret.getInstance().getRet(), Protocol.Transaction.Result.code.SUCESS);
      Assert.assertEquals(
          dbManager.getContractStore().get(ByteArray.fromHexString(CONTRACT_ADDRESS))
              .getConsumeUserResourcePercent(
                  dbManager.getDynamicPropertiesStore().disableJavaLangMath()), TARGET_PERCENT);

      // second
      secondActuator.validate();
      secondActuator.execute(ret);

      Assert.assertEquals(ret.getInstance().getRet(), Protocol.Transaction.Result.code.SUCESS);
      Assert.assertEquals(
          dbManager.getContractStore().get(ByteArray.fromHexString(CONTRACT_ADDRESS))
              .getConsumeUserResourcePercent(
                  dbManager.getDynamicPropertiesStore().disableJavaLangMath()), 90L);

    } catch (ContractValidateException e) {
      Assert.assertFalse(e instanceof ContractValidateException);
    } catch (ContractExeException e) {
      Assert.assertFalse(e instanceof ContractExeException);
    }
  }
```
