### Title
Multi-sig `Active` permission operations bitmap is bypassable for privileged financial actions (freeze, vote, delegate resource, withdraw reward) executed via TVM native opcodes - ([File: actuator/src/main/java/org/tron/core/vm/program/Program.java])

### Summary
TRON's multi-signature account permission system lets an owner restrict a given `Active` key to only a subset of transaction types via the `operations` bitmap on `Permission` [1](#0-0) . This bitmap is checked once, against the *outer* transaction's `ContractType` (e.g. `FreezeBalanceV2Contract`, `VoteWitnessContract`, `DelegateResourceContract`), both at broadcast/signing time and at block-verification time [2](#0-1)  and in `WalletUtil.checkPermissionOperations` [3](#0-2) . However, the exact same financial capabilities (freeze/unfreeze balance, vote for witness, delegate/undelegate resource, withdraw reward) are also reachable as TVM opcodes invoked from inside a `TriggerSmartContract` transaction, e.g. `Program.freeze()` [4](#0-3) , and the analogous `vote`, `unfreeze`, `freezeBalanceV2`, `unfreezeBalanceV2`, `delegateResource`, `withdrawReward` methods in the same file. These native-contract processors (`FreezeBalanceProcessor`, `FreezeBalanceV2Processor`, `VoteWitnessProcessor`, `DelegateResourceProcessor`, `WithdrawRewardProcessor`, etc.) never re-check the signing key's `Permission.operations` bitmap for the specific financial action being performed — they only operate against `getContextAddress()`/`getCallerAddress()`.

This mirrors the reported class of bug: two independent code paths grant the same privileged financial capability, but only one of them enforces the intended, narrower authorization scope, while the other path (reachable by any actor holding a key with just the `TriggerSmartContract` bit enabled) bypasses it entirely.

### Finding Description
When an account owner configures a limited `Active` permission (e.g., a key intended only to sign `TransferContract` and `TriggerSmartContract` transactions, deliberately *not* granting `FreezeBalanceV2Contract`, `VoteWitnessContract`, `DelegateResourceContract`, or `WithdrawBalanceContract` bits), the intent is that this key cannot freeze balance, vote for witnesses, delegate resources, or withdraw staking rewards on the owner's behalf.

The enforcement of this restriction happens exactly once, at signature/permission validation time, by inspecting the outer transaction's `Transaction.Contract.getType()` against the permission's 32-byte `operations` bitmap: `WalletUtil.checkPermissionOperations` compares `contract.getTypeValue()` (i.e. `FreezeBalanceV2Contract`, `VoteWitnessContract`, etc.) to the bitmap [3](#0-2) . This same helper is invoked from `TransactionCapsule.checkPermission` during both `addSign`/`validateSignature` [2](#0-1)  and consensus-time `validateSignature` [5](#0-4) .

But if the same limited key is permitted to sign a `TriggerSmartContract` transaction (a bit almost every "restricted" key needs enabled for normal contract usage), that transaction's outer type is `TriggerSmartContract`, not `FreezeBalanceV2Contract`/`VoteWitnessContract`/etc. The permission check therefore only validates that `TriggerSmartContract` is allowed — it never looks at what the contract bytecode does internally. Once inside the VM, `Program.freeze()`, `Program.freezeBalanceV2()`, the vote-related opcode handler, `Program.delegateResource()`, and `Program.withdrawReward()` all execute the equivalent native actuator logic (`FreezeBalanceProcessor`, `FreezeBalanceV2Processor`, `VoteWitnessProcessor`, `DelegateResourceProcessor`, `WithdrawRewardProcessor`) using `getContextAddress()`/`getCallerAddress()` as owner, with **no check of the calling key's `Permission.operations` bitmap for the underlying financial action type**. See the freeze opcode handler as a representative example, which simply builds the processor param from the program context and executes it without any permission re-check [4](#0-3) .

Consequently, a key that is only supposed to be able to sign transfers and generic contract calls can, by calling (or being tricked into calling) a smart contract that internally invokes the freeze/vote/delegate/withdraw TVM opcodes, perform the exact same privileged financial state changes that the account owner explicitly withheld from that key via the multisig `operations` bitmap.

### Impact Explanation
This breaks the core security guarantee of TRON's multi-signature/permission system: that an `Active` permission's `operations` bitmap precisely scopes what a given key can do. An attacker who compromises, or is issued, a "restricted" active key (e.g. a hot wallet key intended only for token transfers) can use it to freeze/unfreeze the account's TRX balance, change witness votes, delegate/undelegate bandwidth or energy resources to arbitrary addresses, or withdraw staking rewards — actions the account owner never authorized for that key. This constitutes unauthorized account operation and potential asset/resource manipulation (locking funds via freeze, redirecting delegated resources, diverting reward withdrawal), which is the kind of impact the report's "duplicate functionality bypassing intended access control" class targets.

### Likelihood Explanation
Any account that uses TRON's multi-sig permission feature (`AllowMultiSign`) with a key scoped to allow `TriggerSmartContract` but not the specific financial contract types is exposed. Since `TriggerSmartContract` is a near-universal permission granted to operational/hot keys (to allow normal contract interactions), this is a realistic, commonly-occurring configuration rather than an edge case, and requires only a single signed `TriggerSmartContract` transaction calling a contract that exercises the freeze/vote/delegate/withdraw opcodes — well within reach of an unprivileged transaction broadcaster holding that scoped key.

### Recommendation
Enforce the multisig `operations` permission scope for the underlying financial action, not just the outer transaction contract type, when native staking/voting/reward opcodes are invoked from inside the TVM. Concretely, before executing `FreezeBalanceProcessor`, `FreezeBalanceV2Processor`, `UnfreezeBalanceProcessor`, `UnfreezeBalanceV2Processor`, `VoteWitnessProcessor`, `DelegateResourceProcessor`, `UnDelegateResourceProcessor`, and `WithdrawRewardProcessor` from `Program.java`, re-validate that the permission used to sign the enclosing transaction also authorizes the corresponding native `ContractType` (`FreezeBalanceContract`/`FreezeBalanceV2Contract`, `VoteWitnessContract`, `DelegateResourceContract`, `WithdrawBalanceContract`, etc.), analogous to how `WalletUtil.checkPermissionOperations` already gates the equivalent standalone actuators.

### Proof of Concept
1. Account X enables multi-sign (`AllowMultiSign`) and configures an `Active` permission P for key K with the `operations` bitmap enabling only `TransferContract` and `TriggerSmartContract`, explicitly leaving the `FreezeBalanceV2Contract`, `VoteWitnessContract`, `DelegateResourceContract`, and `WithdrawBalanceContract` bits unset — intending K to be usable only for payments/contract calls, never staking/voting.
2. Deploy (or reuse) a smart contract exposing wrapper functions like the ones exercised in `FreezeTest.sol`/`VoteTest.java`, whose Solidity code calls `target.freeze(amount, res)`, `target.unfreeze(res)`, `vote(srList, tpList)`, delegate/undelegate resource, and `withdrawreward()` opcodes on behalf of `msg.sender` (i.e. account X) [6](#0-5) [7](#0-6) .
3. Using key K, sign and broadcast a `TriggerSmartContract` transaction (`permissionId` = P) calling that contract's `freeze`/`voteWitness`/`unfreeze`/`withdrawReward` function.
4. Signature/permission validation in `TransactionCapsule.checkPermission` only checks that P allows `TriggerSmartContract` [2](#0-1) , which succeeds; the transaction is accepted.
5. Inside VM execution, `Program.freeze()`/vote/withdraw executes the native processor directly on account X's state with no re-check of P's `operations` bitmap for `FreezeBalanceV2Contract`/`VoteWitnessContract`/`WithdrawBalanceContract` [4](#0-3) , so account X's balance is frozen / votes change / rewards withdrawn — actions key K was never authorized to perform.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L124-145)
```java
    ByteString operations = permission.getOperations();
    if (permission.getType() != PermissionType.Active) {
      if (!operations.isEmpty()) {
        throw new ContractValidateException(
            permission.getType() + " permission needn't operations");
      }
      return true;
    }
    //check operations
    if (operations.isEmpty() || operations.size() != 32) {
      throw new ContractValidateException("operations size must 32");
    }

    byte[] types1 = dynamicStore.getAvailableContractType();
    for (int i = 0; i < 256; i++) {
      boolean b = (operations.byteAt(i / 8) & (1 << (i % 8))) != 0;
      boolean t = ((types1[(i / 8)] & 0xff) & (1 << (i % 8))) != 0;
      if (b && !t) {
        throw new ContractValidateException(i + " isn't a validate ContractType");
      }
    }
    return true;
```

**File:** chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java (L468-491)
```java
  public static boolean validateSignature(Transaction transaction,
      byte[] hash, AccountStore accountStore, DynamicPropertiesStore dynamicPropertiesStore)
      throws PermissionException, SignatureException, SignatureFormatException {
    Transaction.Contract contract = transaction.getRawData().getContractList().get(0);
    int permissionId = contract.getPermissionId();
    byte[] owner = getOwner(contract);
    AccountCapsule account = accountStore.get(owner);
    Permission permission = null;
    if (account == null) {
      if (permissionId == 0) {
        permission = AccountCapsule.getDefaultPermission(ByteString.copyFrom(owner));
      }
      if (permissionId == 2) {
        permission = AccountCapsule
            .createDefaultActivePermission(ByteString.copyFrom(owner), dynamicPropertiesStore);
      }
    } else {
      permission = account.getPermissionById(permissionId);
    }
    if (permission == null) {
      throw new PermissionException("permission isn't exit");
    }
    checkPermission(permissionId, permission, contract);
    long weight = checkWeight(permission, transaction.getSignatureList(), hash, null);
```

**File:** chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java (L635-645)
```java
  private static void checkPermission(int permissionId, Permission permission, Transaction.Contract contract) throws PermissionException {
    if (permissionId != 0) {
      if (permission.getType() != PermissionType.Active) {
        throw new PermissionException("Permission type is error");
      }
      //check operations
      if (!checkPermissionOperations(permission, contract)) {
        throw new PermissionException("Permission denied");
      }
    }
  }
```

**File:** chainbase/src/main/java/org/tron/common/utils/WalletUtil.java (L27-37)
```java
  public static boolean checkPermissionOperations(Permission permission, Contract contract)
      throws PermissionException {
    ByteString operations = permission.getOperations();
    if (operations.size() != 32) {
      throw new PermissionException(String.format("operations size must 32, actual: %d",
          operations.size()));
    }
    int contractType = contract.getTypeValue();
    boolean b = (operations.byteAt(contractType / 8) & (1 << (contractType % 8))) != 0;
    return b;
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L1927-1961)
```java
  public boolean freeze(DataWord receiverAddress, DataWord frozenBalance, DataWord resourceType) {
    Repository repository = getContractState().newRepositoryChild();
    byte[] owner = getContextAddress();
    byte[] receiver = receiverAddress.toTronAddress();

    increaseNonce();
    InternalTransaction internalTx = addInternalTx(null, owner, receiver,
        frozenBalance.longValue(), null,
        "freezeFor" + convertResourceToString(resourceType), nonce, null);

    FreezeBalanceParam param = new FreezeBalanceParam();
    param.setOwnerAddress(owner);
    param.setReceiverAddress(receiver);
    boolean needCheckFrozenTime = CommonParameter.getInstance()
        .getCheckFrozenTime() == 1; // for test
    param.setFrozenDuration(needCheckFrozenTime
        ? repository.getDynamicPropertiesStore().getMinFrozenTime() : 0);
    param.setResourceType(parseResourceCode(resourceType));
    try {
      FreezeBalanceProcessor processor = new FreezeBalanceProcessor();
      param.setFrozenBalance(frozenBalance.sValue().longValueExact());
      processor.validate(param, repository);
      processor.execute(param, repository);
      repository.commit();
      return true;
    } catch (ContractValidateException e) {
      logger.warn("TVM Freeze: validate failure. Reason: {}", e.getMessage());
    } catch (ArithmeticException e) {
      logger.warn("TVM Freeze: frozenBalance out of long range.");
    }
    if (internalTx != null) {
      internalTx.reject();
    }
    return false;
  }
```

**File:** framework/src/test/java/org/tron/common/runtime/vm/FreezeTest.sol (L21-38)
```text
    // selector: 0x30e1e4e5
    // Freeze TRX for target, then return time remaining until expiry
    function freeze(address payable target, uint256 amount, uint256 res)
        external returns (uint256)
    {
        target.freeze(amount, res);           // TRON opcode 0xd5 (FREEZE)
        // STATICCALL to this.getExpireTime(target, res), then subtract
        return block.timestamp
            - address(this).getExpireTime(target, res);
    }

    // selector: 0x7b46b80b
    function unfreeze(address payable target, uint256 res)
        external returns (uint256)
    {
        target.unfreeze(res);                 // TRON opcode 0xd6 (UNFREEZE)
        return 1;
    }
```

**File:** framework/src/test/java/org/tron/common/runtime/vm/VoteTest.java (L49-86)
```java
  /**
   * contract TestVote {
   *     constructor() public payable {}
   *     function freeze(address payable receiver, uint amount, uint res) external {
   *       receiver.freeze(amount, res);
   *     }
   *     function unfreeze(address payable receiver, uint res) external {
   *       receiver.unfreeze(res);
   *     }
   *     function voteWitness(address[] calldata srList,
   *         uint[] calldata tpList) external returns(bool) {
   *       return vote(srList, tpList);
   *     }
   *     function withdrawReward() external returns(uint) {
   *       return withdrawreward();
   *     }
   *     function queryRewardBalance() external view returns(uint) {
   *       return rewardBalance();
   *     }
   *     function isWitness(address sr) external view returns(bool) {
   *       return isSrCandidate(sr);
   *     }
   *     function queryVoteCount(address from, address to) external view returns(uint) {
   *       return voteCount(from, to);
   *     }
   *     function queryTotalVoteCount(address owner) external view returns(uint) {
   *       return totalVoteCount(owner);
   *     }
   *     function queryReceivedVoteCount(address owner) external view returns(uint) {
   *       return receivedVoteCount(owner);
   *     }
   *     function queryUsedVoteCount(address owner) external view returns(uint) {
   *       return usedVoteCount(owner);
   *     }
   *     function killme(address payable target) external {
   *       selfdestruct(target);
   *     }
   *   }
```
