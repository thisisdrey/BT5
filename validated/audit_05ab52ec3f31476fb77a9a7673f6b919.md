### Title
NullPointerException in `ActuatorFactory.getActuatorByContract` from unchecked null Actuator class lookup — (`File: actuator/src/main/java/org/tron/core/actuator/ActuatorFactory.java`)

### Summary
`ActuatorFactory.getActuatorByContract` resolves the `Actuator` implementation class for a transaction's `ContractType` via `TransactionFactory.getActuator(contract.getType())` and immediately calls `.newInstance()` on the result **without checking for `null`**, unlike the sibling class `ActuatorCreator`, which performs the equivalent lookup and explicitly guards against a `null` class before instantiation.

### Finding Description
`TransactionFactory` maintains a `ConcurrentHashMap<ContractType, Class<? extends Actuator>>` (`actuatorMap`) that is populated by the static registration block and by each `AbstractActuator` subclass's constructor calling `TransactionFactory.register(...)`. [1](#0-0) 

Critically, the static initializer registers `ContractType.CreateSmartContract` and `ContractType.TriggerSmartContract` with an explicit `null` actuator class (only their protobuf message classes are registered), because these types are executed through the TVM runtime rather than the generic `Actuator` model: [2](#0-1) 

`ActuatorCreator.getActuatorByContract` correctly anticipates that `getActuator()` can resolve to `null` and throws a `ContractValidateException` in that case: [3](#0-2) 

However, `ActuatorFactory.getActuatorByContract` — the analogous, independently-maintained code path — omits this null check and calls `clazz.newInstance()` directly on a potentially `null` reference: [4](#0-3) 

The enclosing `createActuator` loop only catches `IllegalAccessException | InstantiationException`, so a `NullPointerException` raised by the incorrectly-resolved (`null`) actuator class reference propagates uncaught out of `createActuator`: [5](#0-4) 

This is a structurally identical bug class to the referenced advisory: a name/type is "resolved" through a registry/lookup that can legitimately return an unbound reference, and the calling code fails to detect the unresolved case before dereferencing it, leading to an unhandled runtime failure (CWE-404/CWE-476-adjacent).

### Impact Explanation
If `ActuatorFactory.createActuator` is invoked on a `TransactionCapsule` whose contract list contains a `CreateSmartContract` or `TriggerSmartContract` contract (the two types intentionally mapped to a `null` actuator class), the call throws an uncaught `NullPointerException` instead of a handled `ContractValidateException`. Any caller that does not itself guard against this exception will fail unexpectedly. `ActuatorFactory.createActuator` is referenced from production code in `RuntimeImpl.java` and `Wallet.java`, both of which sit on transaction-processing / smart-contract-trigger code paths reachable by ordinary signed transactions and wallet API calls. An uncaught `NullPointerException` at these points can abort transaction/block processing or an API request path unexpectedly, which is consistent with the DoS bug class in the referenced advisory (uncontrolled resource/processing termination due to an incorrectly resolved reference).

### Likelihood Explanation
The precondition (a `ContractType` mapped to a `null` actuator class) is deterministic and always present for `CreateSmartContract`/`TriggerSmartContract` per the static registration table — no race condition or privileged access is required to reach the unresolved-reference state. The unresolved likelihood depends on which exact call sites in `RuntimeImpl.java`/`Wallet.java` invoke `ActuatorFactory.createActuator` (versus the null-safe `ActuatorCreator`) and whether those call sites wrap the call in a broader `try/catch(Exception)`; I was not able to fully read the body of `RuntimeImpl.java`/`Wallet.java` around the matched call sites within the available tool budget to confirm whether an outer catch-all shields the caller from the NPE. This should be verified directly before treating the impact as fully confirmed.

### Recommendation
Add the same null-check guard used in `ActuatorCreator.getActuatorByContract` to `ActuatorFactory.getActuatorByContract`, throwing a proper `ContractValidateException` (or equivalent handled exception) when `TransactionFactory.getActuator(contract.getType())` resolves to `null`, instead of allowing an unchecked `NullPointerException` to propagate.

### Proof of Concept
1. Construct a `TransactionCapsule` whose raw data contains a contract of type `ContractType.CreateSmartContract` (or `TriggerSmartContract`).
2. Call `ActuatorFactory.createActuator(transactionCapsule, chainBaseManager)` directly, or trigger the code path in `RuntimeImpl.java`/`Wallet.java` that invokes it with such a transaction.
3. Observe that `TransactionFactory.getActuator(ContractType.CreateSmartContract)` returns `null` (per the static registration in `TransactionFactory.java` lines 15-18), and `clazz.newInstance()` at `ActuatorFactory.java` line 52 throws a `NullPointerException`, which is not caught by the surrounding `catch (IllegalAccessException | InstantiationException e)` block at lines 42-44, propagating to the caller.

### Citations

**File:** chainbase/src/main/java/org/tron/core/actuator/TransactionFactory.java (L10-28)
```java
public class TransactionFactory {

  private static final Map<ContractType, Class<? extends Actuator>> actuatorMap = new ConcurrentHashMap<>();
  private static final Map<ContractType, Class<? extends GeneratedMessageV3>> contractMap = new ConcurrentHashMap<>();

  static {
    register(ContractType.CreateSmartContract, null, CreateSmartContract.class);
    register(ContractType.TriggerSmartContract, null, TriggerSmartContract.class);
  }

  public static void register(ContractType type, Class<? extends Actuator> actuatorClass,
      Class<? extends GeneratedMessageV3> clazz) {
    if (type != null && actuatorClass != null) {
      actuatorMap.put(type, actuatorClass);
    }
    if (type != null && clazz != null) {
      contractMap.put(type, clazz);
    }
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ActuatorCreator.java (L60-71)
```java
  private Actuator getActuatorByContract(Contract contract,
      TransactionCapsule tx)
      throws IllegalAccessException, InstantiationException, ContractValidateException {
    Class<? extends Actuator> clazz = TransactionFactory.getActuator(contract.getType());
    if (clazz == null) {
      throw new ContractValidateException("not exist contract " + contract);
    }
    AbstractActuator abstractActuator = (AbstractActuator) clazz.newInstance();
    abstractActuator.setChainBaseManager(chainBaseManager).setContract(contract)
        .setForkUtils(forkController).setTx(tx);
    return abstractActuator;
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ActuatorFactory.java (L27-47)
```java
  public static List<Actuator> createActuator(TransactionCapsule transactionCapsule,
      ChainBaseManager chainBaseManager) {
    List<Actuator> actuatorList = Lists.newArrayList();
    if (null == transactionCapsule || null == transactionCapsule.getInstance()) {
      logger.info("TransactionCapsule or Transaction is null");
      return actuatorList;
    }

    Preconditions.checkNotNull(chainBaseManager, "manager is null");
    Protocol.Transaction.raw rawData = transactionCapsule.getInstance().getRawData();
    rawData.getContractList()
        .forEach(contract -> {
          try {
            actuatorList
                .add(getActuatorByContract(contract, chainBaseManager, transactionCapsule));
          } catch (IllegalAccessException | InstantiationException e) {
            e.printStackTrace();
          }
        });
    return actuatorList;
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ActuatorFactory.java (L49-56)
```java
  private static Actuator getActuatorByContract(Contract contract, ChainBaseManager manager,
      TransactionCapsule tx) throws IllegalAccessException, InstantiationException {
    Class<? extends Actuator> clazz = TransactionFactory.getActuator(contract.getType());
    AbstractActuator abstractActuator = (AbstractActuator) clazz.newInstance();
    abstractActuator.setChainBaseManager(manager).setContract(contract)
        .setForkUtils(manager.getForkController()).setTx(tx);
    return abstractActuator;
  }
```
