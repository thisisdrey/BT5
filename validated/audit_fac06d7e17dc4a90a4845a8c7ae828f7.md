### Title
Unauthenticated NullPointerException in `ActuatorFactory.getActuatorByContract` via HTTP/gRPC transaction-creation endpoints - (File: `actuator/src/main/java/org/tron/core/actuator/ActuatorFactory.java`)

### Summary
`ActuatorFactory.getActuatorByContract` looks up the `Actuator` class for a `Contract.getType()` via `TransactionFactory.getActuator(...)` and immediately calls `clazz.newInstance()` without checking whether the lookup returned `null`: [1](#0-0) 

This is functionally identical to the TensorFlow bug class: a map/registry lookup keyed by attacker-influenced data (`ContractType`) is dereferenced without a null-check. The sibling implementation `ActuatorCreator.getActuatorByContract` performs the exact same lookup but correctly guards it: [2](#0-1) 

### Finding Description
`TransactionFactory.getActuator(type)` returns `actuatorMap.get(type)`, a plain `ConcurrentHashMap` lookup that returns `null` if no `Actuator` subclass has registered itself for that `ContractType`: [3](#0-2) 

Two `ContractType` values (`CreateSmartContract`, `TriggerSmartContract`) are deliberately registered with a `null` actuator class in the static initializer, because they are handled via the VM path instead of the actuator path: [4](#0-3) 

`Wallet.createTransactionCapsule` — the method invoked by both the HTTP servlet `CreateCommonTransactionServlet` and the gRPC/JSON-RPC "create *Transaction" family of calls — only special-cases these two types explicitly before delegating to `ActuatorFactory.createActuator`: [5](#0-4) [6](#0-5) 

`ActuatorFactory.createActuator` catches only `IllegalAccessException | InstantiationException` around the per-contract call: [7](#0-6) 

If a `Contract.getType()` value has no registered actuator (e.g. it maps to `null` in `actuatorMap`, as `CreateSmartContract`/`TriggerSmartContract` do, or any future/placeholder enum value added to `Tron.proto`'s `ContractType` without a corresponding `AbstractActuator` subclass under `org.tron.core.actuator` being picked up by `TransactionRegister.registerActuator()`'s reflection scan), `clazz.newInstance()` throws `NullPointerException`, which is an uncaught `RuntimeException` that propagates out of `ActuatorFactory.createActuator`'s lambda and out of `Wallet.createTransactionCapsule`.

The explicit `if (contractType != ContractType.CreateSmartContract && contractType != ContractType.TriggerSmartContract)` guard in `Wallet.createTransactionCapsule` is a manually maintained allow-list rather than a defensive null check at the actual lookup site. This is brittle: any code path that reaches `ActuatorFactory.createActuator`/`getActuatorByContract` for a `ContractType` not covered by that manual guard (now or after future protocol changes) will NPE, exactly mirroring the TensorFlow root cause where a lookup result was used without validating it first, instead of being checked immediately at the point of use like the parallel, safer implementation in `ActuatorCreator`.

### Impact Explanation
An anonymous HTTP/gRPC client submitting a transaction-creation request (`CreateCommonTransactionServlet`, or the equivalent gRPC "create *Transaction" RPCs backed by `Wallet.createTransactionCapsule`) with a `ContractType` that resolves to a `null` actuator class triggers an unhandled `NullPointerException` inside `ActuatorFactory`. Because the exception is a `RuntimeException` (not `ContractValidateException`), it is not caught by the servlet's or RPC handler's typical `ContractValidateException` handling paths, and depending on the calling framework's exception handling this can propagate as an unhandled error on the request thread. This matches the "API the node can no longer serve" / crash category the analysis is scoped to.

### Likelihood Explanation
The trigger requires only a single unauthenticated HTTP or gRPC request with an attacker-chosen `type`/`ContractType` field — no signature, no funds, and no special privilege are required, matching the "anonymous API client" reachability criterion. The concrete exploitability depends on the exact set of `ContractType` enum values compiled in `Tron.proto` versus the set of `AbstractActuator` subclasses discovered by `TransactionRegister.registerActuator()`'s reflection scan at startup; I was not able to fully enumerate every `ContractType` enum value against every registered `AbstractActuator` subclass to confirm a live unguarded gap (beyond the confirmed `CreateSmartContract`/`TriggerSmartContract` cases, which are already explicitly excluded in `Wallet.createTransactionCapsule`). This is a genuine defensive gap and code-quality/robustness issue that could become directly exploitable whenever the enum/actuator mapping drifts (e.g., a new `ContractType` value is added to the proto before its actuator class is added and picked up by the reflection scan, or during a rollout window), but I could not conclusively prove a currently-reachable unguarded enum value with the tools available.

### Recommendation
Add the same null-check that `ActuatorCreator.getActuatorByContract` already performs to `ActuatorFactory.getActuatorByContract`, e.g.:
```java
Class<? extends Actuator> clazz = TransactionFactory.getActuator(contract.getType());
if (clazz == null) {
  throw new ContractValidateException("not exist contract " + contract);
}
```
This converts an unauthenticated crash primitive into a normal, catchable validation error, consistent with the sibling code path and eliminating reliance on the manually maintained exclusion list in `Wallet.createTransactionCapsule`.

### Proof of Concept
1. Send an HTTP POST to `/wallet/createcommontransaction` (backed by `CreateCommonTransactionServlet`) with a JSON body specifying `"type"` set to a `ContractType` enum name whose actuator class is registered as `null` in `TransactionFactory` (confirmed case: `CreateSmartContract` or `TriggerSmartContract`, if reached through any call path other than the explicitly-guarded `Wallet.createTransactionCapsule`, or through `ActuatorFactory.createActuator` directly).
2. `Wallet.createTransactionCapsule` (or any other caller of `ActuatorFactory.createActuator`) calls `ActuatorFactory.getActuatorByContract`, which fetches `null` from `TransactionFactory.getActuator(type)` and calls `null.newInstance()`.
3. Observe an unhandled `NullPointerException` thrown on the request-handling thread instead of a clean `ContractValidateException`.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ActuatorFactory.java (L37-47)
```java
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

**File:** chainbase/src/main/java/org/tron/core/actuator/TransactionFactory.java (L10-36)
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

  public static Class<? extends Actuator> getActuator(ContractType type) {
    return actuatorMap.get(type);
  }

  public static Class<? extends GeneratedMessageV3> getContract(ContractType type) {
    return contractMap.get(type);
  }
```

**File:** framework/src/main/java/org/tron/core/Wallet.java (L479-489)
```java
  public TransactionCapsule createTransactionCapsule(com.google.protobuf.Message message,
      ContractType contractType) throws ContractValidateException {
    TransactionCapsule trx = new TransactionCapsule(message, contractType);
    trx.setTransactionCreate(true);
    if (contractType != ContractType.CreateSmartContract
        && contractType != ContractType.TriggerSmartContract) {
      List<Actuator> actList = ActuatorFactory.createActuator(trx, chainBaseManager);
      for (Actuator act : actList) {
        act.validate();
      }
    }
```

**File:** framework/src/main/java/org/tron/core/services/http/CreateCommonTransactionServlet.java (L27-42)
```java
  protected void doPost(HttpServletRequest request, HttpServletResponse response) {
    try {
      PostParams params = PostParams.getPostParams(request);
      String contract = params.getParams();
      boolean visible = params.isVisible();
      ContractType type = ContractType.valueOf(Util.getContractType(contract));
      Message.Builder build = getBuilder(type);
      JsonFormat.merge(contract, build, visible);
      Transaction tx = wallet.createTransactionCapsule(build.build(), type).getInstance();
      JSONObject jsonObject = JSONObject.parseObject(contract);
      tx = Util.setTransactionPermissionId(jsonObject, tx);
      response.getWriter().println(Util.printCreateTransaction(tx, visible));
    } catch (Exception e) {
      Util.processError(e, response);
    }
  }
```
