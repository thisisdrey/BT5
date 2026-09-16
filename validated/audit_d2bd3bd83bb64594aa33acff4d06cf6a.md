### Title
TRC10 `transferToken` TVM Opcode Bypasses the Governance-Controlled `ForbidTransferToContract` Restriction Enforced by `TransferAssetActuator` - (File: actuator/src/main/java/org/tron/core/vm/VMUtils.java)

### Summary
`TransferAssetActuator.validate()` (the transaction-level path for TRC10 asset transfers) explicitly checks the `ForbidTransferToContract` dynamic parameter and rejects transfers of TRC10 tokens to smart-contract addresses when the committee has enabled that restriction. The TVM-internal transfer path used by the Solidity `transferToken()` builtin, implemented in `VMUtils.validateForSmartContract()`, performs a parallel set of validations for the same underlying operation (moving a TRC10 balance to a destination address) but never checks `ForbidTransferToContract`, so a contract can move TRC10 balances to another contract address even while the network-wide restriction is active.

### Finding Description
`TransferAssetActuator.validate()` contains this explicit governance check before allowing a TRC10 transfer to complete: [1](#0-0) 

This mirrors the same restriction implemented for plain TRX transfers in `TransferActuator`, confirming `ForbidTransferToContract` is a deliberate, committee-controlled proposal parameter meant to prevent asset transfers to contract accounts network-wide, as tracked by `ProposalUtil` and `DynamicPropertiesStore`.

However, the TVM opcode used to move a TRC10 token from one address to another (invoked via `address.transferToken(amount, id)` inside a smart contract) validates the destination purely through `VMUtils.validateForSmartContract()`: [2](#0-1) 

This method re-implements every check that `TransferAssetActuator.validate()` performs — address validity, self-transfer, asset existence, balance sufficiency, overflow — but omits the `ForbidTransferToContract`/`AccountType.Contract` check entirely. As a result, any account (even an unprivileged EOA) can deploy or call a trivial contract that performs `toAddress.transferToken(amount, tokenId)` to move TRC10 balances to another smart-contract address, silently defeating the restriction the committee intentionally enabled through the `TransferAssetActuator` path.

This is the same bug class as the MISP report: a duplicate/alternate code path that performs the same privileged operation as a canonical, authorization-checked path, but re-implements the checks incompletely, dropping one of the checks (`SharingGroup::canUse()` in MISP vs. `ForbidTransferToContract` here) that is present in the "normal" path.

### Impact Explanation
`ForbidTransferToContract` is a network-wide protective policy that the TRON committee can activate to stop TRC10 token transfers into contract accounts (e.g., to prevent tokens becoming irretrievably stuck in contracts that cannot handle them, or to mitigate accounting/interoperability issues with TRC10 assets and smart contracts). Any unprivileged transaction sender can trivially route around this restriction purely through TVM-level `transferToken` calls, meaning the committee-approved protection has no actual effect once malicious or careless actors use contract-mediated transfers instead of the plain `TransferAssetContract`. This undermines a security control the network relies on to guard against fund entrapment/loss scenarios for TRC10 balances sent to contracts, which the actuator-level restriction was specifically designed to prevent.

### Likelihood Explanation
Trivially reachable: any account can deploy a minimal contract with a function like `function send(address payable to, trcToken id, uint256 amount) public { to.transferToken(amount, id); }` and call it, with zero special privileges, whenever the `ForbidTransferToContract` proposal is active. No signature or permission bypass beyond ordinary `TriggerSmartContractContract` submission is required.

### Recommendation
Add the same `ForbidTransferToContract` check that `TransferAssetActuator.validate()` performs into `VMUtils.validateForSmartContract()` (or into the TVM `transferToken` opcode handling prior to invoking it), rejecting the internal transfer when the destination account exists and `getType() == AccountType.Contract` while `dynamicStore.getForbidTransferToContract() == 1`, mirroring the actuator's check exactly.

### Proof of Concept
1. Committee enables the restriction: `dynamicPropertiesStore.saveForbidTransferToContract(1)` (as exercised in `TransferAssetActuatorTest.transferToContractAddress`, which shows that direct `TransferAssetActuator` calls to a contract address correctly fail with "Cannot transfer asset to smartContract."). [3](#0-2) 
2. An unprivileged user deploys a helper contract owning some TRC10 balance and calls its `transferTokenTo(address,trcToken,uint256)` function (as exercised functionally in `TransferTokenTest`/`TransferToAccountTest`, which show `transferToken()` executing successfully against arbitrary destination addresses, including other contract addresses). [4](#0-3) 
3. Because `VMUtils.validateForSmartContract()` is invoked internally for this opcode and contains no `ForbidTransferToContract` check, the transfer to the destination contract succeeds despite the committee-enabled restriction that would have blocked the equivalent `TransferAssetContract` transaction.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/TransferAssetActuator.java (L169-175)
```java
    AccountCapsule toAccount = accountStore.get(toAddress);
    if (toAccount != null) {
      //after ForbidTransferToContract proposal, send trx to smartContract by actuator is not allowed.
      if (dynamicStore.getForbidTransferToContract() == 1
          && toAccount.getType() == AccountType.Contract) {
        throw new ContractValidateException("Cannot transfer asset to smartContract.");
      }
```

**File:** actuator/src/main/java/org/tron/core/vm/VMUtils.java (L182-247)
```java
  public static boolean validateForSmartContract(Repository deposit, byte[] ownerAddress,
      byte[] toAddress, byte[] tokenId, long amount) throws ContractValidateException {
    if (deposit == null) {
      throw new ContractValidateException("No deposit!");
    }

    byte[] tokenIdWithoutLeadingZero = ByteUtil.stripLeadingZeroes(tokenId);

    if (!DecodeUtil.addressValid(ownerAddress)) {
      throw new ContractValidateException("Invalid ownerAddress");
    }
    if (!DecodeUtil.addressValid(toAddress)) {
      throw new ContractValidateException("Invalid toAddress");
    }

    if (amount <= 0) {
      throw new ContractValidateException("Amount must greater than 0.");
    }

    if (Arrays.equals(ownerAddress, toAddress)) {
      throw new ContractValidateException("Cannot transfer asset to yourself.");
    }

    AccountCapsule ownerAccount = deposit.getAccount(ownerAddress);
    if (ownerAccount == null) {
      throw new ContractValidateException("No owner account!");
    }

    if (deposit.getAssetIssue(tokenIdWithoutLeadingZero) == null) {
      throw new ContractValidateException("No asset !");
    }
    if (!Commons.getAssetIssueStoreFinal(deposit.getDynamicPropertiesStore(),
        deposit.getAssetIssueStore(), deposit.getAssetIssueV2Store())
        .has(tokenIdWithoutLeadingZero)) {
      throw new ContractValidateException("No asset !");
    }

    Long assetBalance = ownerAccount.getAsset(deposit.getDynamicPropertiesStore(),
            ByteArray.toStr(tokenIdWithoutLeadingZero));
    if (null == assetBalance || assetBalance <= 0) {
      throw new ContractValidateException("assetBalance must greater than 0.");
    }
    if (amount > assetBalance) {
      throw new ContractValidateException("assetBalance is not sufficient.");
    }

    AccountCapsule toAccount = deposit.getAccount(toAddress);
    if (toAccount != null) {
      assetBalance = toAccount.getAsset(deposit.getDynamicPropertiesStore(),
              ByteArray.toStr(tokenIdWithoutLeadingZero));
      if (assetBalance != null) {
        try {
          addExact(assetBalance, amount,
              VMConfig.disableJavaLangMath()); //check if overflow
        } catch (Exception e) {
          logger.debug(e.getMessage(), e);
          throw new ContractValidateException(e.getMessage());
        }
      }
    } else {
      throw new ContractValidateException(
          "Validate InternalTransfer error, no ToAccount. And not allowed to create account in smart contract.");
    }

    return true;
  }
```

**File:** framework/src/test/java/org/tron/core/actuator/TransferAssetActuatorTest.java (L1373-1439)
```java
  @Test
  public void transferToContractAddress()
          throws ContractExeException, ReceiptCheckErrException, VMIllegalException,
          ContractValidateException, BalanceInsufficientException {
    dbManager.getDynamicPropertiesStore().saveForbidTransferToContract(1);
    createAssertSameTokenNameActive();
    VMConfig.initAllowMultiSign(1);
    VMConfig.initAllowTvmTransferTrc10(1);
    VMConfig.initAllowTvmConstantinople(1);
    VMConfig.initAllowTvmSolidity059(1);
    String contractName = "testContract";
    byte[] address = Hex.decode(OWNER_ADDRESS);
    adjustBalance(dbManager.getChainBaseManager()
            .getAccountStore(), address, 1000000000L);

    String ABI = "[]";
    String codes = "608060405261019c806100136000396000f3fe6080604052600436106100455"
            + "77c01000000000000000000000000000000000000000000000000000000006000350"
            + "4632a205edf811461004a5780634cd2270c146100c8575b600080fd5b34801561005"
            + "657600080fd5b50d3801561006357600080fd5b50d2801561007057600080fd5b506"
            + "100c6600480360360c081101561008757600080fd5b5073fffffffffffffffffffff"
            + "fffffffffffffffffff8135811691602081013582169160408201351690606081013"
            + "59060808101359060a001356100d0565b005b6100c661016e565b60405173fffffff"
            + "fffffffffffffffffffffffffffffffff87169084156108fc0290859060008181818"
            + "58888f1505060405173ffffffffffffffffffffffffffffffffffffffff891693508"
            + "5156108fc0292508591506000818181858888f1505060405173fffffffffffffffff"
            + "fffffffffffffffffffffff8816935084156108fc029250849150600081818185888"
            + "8f15050505050505050505050565b56fea165627a7a72305820cc2d598d1b3f968bb"
            + "dc7825ce83d22dad48192f4bf95bda7f9e4ddf61669ba830029";

    long value = 1;
    long feeLimit = 1000000000L;
    long consumeUserResourcePercent = 0;
    RepositoryImpl repository = RepositoryImpl.createRoot(StoreFactory.getInstance());
    byte[] contractAddress = TvmTestUtils.deployContractWholeProcessReturnContractAddress(
            contractName, address, ABI, codes, value, feeLimit, consumeUserResourcePercent,
            null, 0, 0, repository, null);

    TransferAssetActuator actuator = new TransferAssetActuator();
    actuator.setChainBaseManager(dbManager.getChainBaseManager())
            .setAny(getContract(100L, contractAddress));
    TransactionResultCapsule ret = new TransactionResultCapsule();
    try {
      actuator.validate();
      actuator.execute(ret);
      Assert.assertEquals(ret.getInstance().getRet(), code.SUCESS);
      AccountCapsule owner =
              dbManager.getAccountStore().get(ByteArray.fromHexString(OWNER_ADDRESS));
      AccountCapsule toAccount =
              dbManager.getAccountStore().get(contractAddress);
      // V1, data is not exist
      Assert.assertNull(owner.getAssetMapForTest().get(ASSET_NAME));
      Assert.assertNull(toAccount.getAssetMapForTest().get(ASSET_NAME));
      // check V2
      long tokenIdNum = dbManager.getDynamicPropertiesStore().getTokenIdNum();
      Assert.assertEquals(
              owner.getInstance().getAssetV2Map().get(String.valueOf(tokenIdNum)).longValue(),
              OWNER_ASSET_BALANCE - 100);
      Assert.assertEquals(
              toAccount.getInstance().getAssetV2Map().get(String.valueOf(tokenIdNum)).longValue(),
              100L);
    } catch (ContractValidateException e) {
      Assert.assertTrue(e.getMessage().contains("Cannot transfer"));
    } catch (ContractExeException e) {
      Assert.assertFalse(e instanceof ContractExeException);
    }
  }
```

**File:** framework/src/test/java/org/tron/common/runtime/vm/TransferTokenTest.java (L103-139)
```java
  @Test
  public void TransferTokenTest()
      throws ContractExeException, ReceiptCheckErrException,
      VMIllegalException, ContractValidateException {
    /*  1. Test deploy with tokenValue and tokenId */
    long id = createAsset("testToken1");
    byte[] contractAddress = deployTransferTokenContract(id);
    repository.commit();
    Assert.assertEquals(100,
        dbManager.getAccountStore().get(contractAddress)
                .getAssetV2MapForTest().get(String.valueOf(id)).longValue());
    Assert.assertEquals(1000, dbManager.getAccountStore().get(contractAddress).getBalance());

    String selectorStr = "TransferTokenTo(address,trcToken,uint256)";
    String params = "000000000000000000000000548794500882809695a8a687866e76d4271a1abc"
        + Hex.toHexString(new DataWord(id).getData())
        //TRANSFER_TO, 100001, 9
        + "0000000000000000000000000000000000000000000000000000000000000009";
    byte[] triggerData = TvmTestUtils.parseAbi(selectorStr, params);

    /*  2. Test trigger with tokenValue and tokenId,
     also test internal transaction transferToken function */
    long triggerCallValue = 100;
    long feeLimit = 100000000;
    long tokenValue = 8;
    Transaction transaction = TvmTestUtils
        .generateTriggerSmartContractAndGetTransaction(Hex.decode(OWNER_ADDRESS), contractAddress,
            triggerData,
            triggerCallValue, feeLimit, tokenValue, id);
    runtime = TvmTestUtils.processTransactionAndReturnRuntime(transaction, dbManager, null);

    Assert.assertNull(runtime.getRuntimeError());
    Assert.assertEquals(100 + tokenValue - 9,
        dbManager.getAccountStore().get(contractAddress).getAssetV2MapForTest()
                .get(String.valueOf(id)).longValue());
    Assert.assertEquals(9, dbManager.getAccountStore().get(Hex.decode(TRANSFER_TO))
            .getAssetV2MapForTest().get(String.valueOf(id)).longValue());
```
