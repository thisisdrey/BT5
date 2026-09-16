## Title
Unbounded, permissionless growth of `DelegatedResourceAccountIndex` allows any account to grief a victim's `GetDelegatedResourceAccountIndexV2` query path - ([File: chainbase/src/main/java/org/tron/core/store/DelegatedResourceAccountIndexStore.java])

### Summary
`DelegateResourceContract`/`DelegateResourceContract` (v2 model) lets any account append an entry to a *target* account's delegation index simply by delegating a minimal amount of frozen TRX to that address, with no cap on how many distinct delegator entries a single receiver can accumulate. This mirrors the OpenQ bug pattern: an unprivileged caller can cheaply and repeatedly grow a data structure keyed to someone else's account, degrading that account's later reads/interactions.

### Finding Description
`DelegateResourceActuator.validate()`/`DelegateResourceProcessor.validate()` only require `delegateBalance >= 1 TRX` and a valid, non-contract receiver address — there is no limit on the number of distinct delegator→receiver pairs that can be created against one receiver. [1](#0-0) [2](#0-1) 

Each successful delegation writes a new key under the `V2_TO_PREFIX + receiver + owner` (and `V2_FROM_PREFIX`) namespace in `DelegatedResourceAccountIndexStore`, one entry per unique delegator: [3](#0-2) 

When the index for an address is later read (e.g. via the `GetDelegatedResourceAccountIndexV2` API or `getV2Index`), the store does a full `prefixQuery` scan over all keys sharing that receiver's prefix and rebuilds/sorts the whole list on every call: [4](#0-3) 

This is exposed unauthenticated over HTTP/gRPC: [5](#0-4) 

An attacker can create arbitrarily many new accounts (self-funded is enough since only 1 TRX minimum is required per `FreezeBalanceV2`/`DelegateResourceContract`), freeze 1 TRX for bandwidth or energy on each, and delegate it to the same target/victim receiver address. Each such delegation is a cheap, independent transaction that unconditionally succeeds and appends one more entry to the victim's index — there is no dedup collapsing repeat delegators beyond what already exists, and no maximum count check analogous to `TOKEN_ADDRESS_LIMIT` in the OpenQ report.

### Impact Explanation
Once the victim's `V2_TO_PREFIX`/`V2_FROM_PREFIX` entry set grows very large, every subsequent `getV2Index`/`GetDelegatedResourceAccountIndexV2` (and `PBFT`/`Solidity` mirrored) query against that address triggers an unbounded `prefixQuery` + full in-memory sort, degrading or effectively denying that specific API query path for the victim (and consuming disproportionate node resources per query). This is analogous to the OpenQ griefing pattern where an attacker fills a shared, capacity-bound (or here, capacity-unbound-but-costly) list tied to someone else's resource using cheap calls, degrading legitimate usage of that resource by everyone else.

### Likelihood Explanation
Likelihood is moderate: the cost to the attacker is proportional to `1 TRX * numberOfFakeAccounts` (recoverable — the frozen/delegated TRX is not spent, only locked, and `FreezeBalanceV2`/`DelegateResourceContract` fees are effectively bandwidth/energy costs), and the operation is fully permissionless with no reference to whitelist/limit checks. It only affects a specific address's index query performance, not consensus or global chain integrity, so exploitation is easy but the blast radius is limited to that address's DelegatedResourceAccountIndex reads.

### Recommendation
Introduce an upper bound on the number of distinct delegator entries per receiver (and per owner) in `DelegatedResourceAccountIndexStore`/`DelegateResourceActuator.validate()`, similar to `TOKEN_ADDRESS_LIMIT` in the referenced report, or change the query implementation so it is paginated/streamed rather than doing a full unbounded `prefixQuery` scan and in-memory sort on every read.

### Proof of Concept
1. Create N new accounts (N can be large, e.g. thousands), each funded with a small amount of TRX.
2. For each account, call `FreezeBalanceV2Contract` (or `freezeV2` in TVM) with the 1 TRX minimum for BANDWIDTH.
3. For each account, call `DelegateResourceContract` delegating the frozen 1 TRX to the same fixed victim receiver address (`receiverAddress` field), see `DelegateResourceActuator.validate()`/`execute()` at [6](#0-5) .
4. Each call succeeds and adds a new key under `V2_TO_PREFIX+victim+ownerN` in `DelegatedResourceAccountIndexStore`.
5. Query `GetDelegatedResourceAccountIndexV2` (or the gRPC/PBFT/Solidity equivalents) for the victim address; observe the `getWithPrefix` scan cost growing linearly/superlinearly with N, as it must iterate and sort every entry: [7](#0-6) .

Note: I was unable to fully verify whether this unbounded-index-growth issue has already been mitigated by a rate limiter (`RateLimiterServlet` is used on the servlet) or additional restrictions elsewhere in the codebase that were not indexed; a Devin session with full repository access would be needed to confirm current production-hardening around this specific query path.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/DelegateResourceActuator.java (L44-98)
```java
  @Override
  public boolean execute(Object result) throws ContractExeException {
    TransactionResultCapsule ret = (TransactionResultCapsule) result;
    if (Objects.isNull(ret)) {
      throw new RuntimeException(ActuatorConstant.TX_RESULT_NULL);
    }

    long fee = calcFee();
    final DelegateResourceContract delegateResourceContract;
    AccountStore accountStore = chainBaseManager.getAccountStore();
    byte[] ownerAddress;
    try {
      delegateResourceContract = this.any.unpack(DelegateResourceContract.class);
      ownerAddress = getOwnerAddress().toByteArray();
    } catch (InvalidProtocolBufferException e) {
      logger.debug(e.getMessage(), e);
      ret.setStatus(fee, code.FAILED);
      throw new ContractExeException(e.getMessage());
    }

    AccountCapsule ownerCapsule = accountStore
        .get(delegateResourceContract.getOwnerAddress().toByteArray());
    DynamicPropertiesStore dynamicStore = chainBaseManager.getDynamicPropertiesStore();
    long delegateBalance = delegateResourceContract.getBalance();
    boolean lock = delegateResourceContract.getLock();
    long lockPeriod = getLockPeriod(dynamicStore.supportMaxDelegateLockPeriod(),
            delegateResourceContract);
    byte[] receiverAddress = delegateResourceContract.getReceiverAddress().toByteArray();

    // delegate resource to receiver
    switch (delegateResourceContract.getResource()) {
      case BANDWIDTH:
        delegateResource(ownerAddress, receiverAddress, true,
            delegateBalance, lock, lockPeriod);

        ownerCapsule.addDelegatedFrozenV2BalanceForBandwidth(delegateBalance);
        ownerCapsule.addFrozenBalanceForBandwidthV2(-delegateBalance);
        break;
      case ENERGY:
        delegateResource(ownerAddress, receiverAddress, false,
            delegateBalance, lock, lockPeriod);

        ownerCapsule.addDelegatedFrozenV2BalanceForEnergy(delegateBalance);
        ownerCapsule.addFrozenBalanceForEnergyV2(-delegateBalance);
        break;
      default:
        logger.debug("Resource Code Error.");
    }

    accountStore.put(ownerCapsule.createDbKey(), ownerCapsule);

    ret.setStatus(fee, code.SUCESS);

    return true;
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/DelegateResourceActuator.java (L147-150)
```java
    long delegateBalance = delegateResourceContract.getBalance();
    if (delegateBalance < TRX_PRECISION) {
      throw new ContractValidateException("delegateBalance must be greater than or equal to 1 TRX");
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/DelegateResourceProcessor.java (L52-55)
```java
    long delegateBalance = param.getDelegateBalance();
    if (delegateBalance < TRX_PRECISION) {
      throw new ContractValidateException("delegateBalance must be greater than or equal to 1 TRX");
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/DelegateResourceProcessor.java (L166-181)
```java
    //modify DelegatedResourceAccountIndex
    long now = repo.getDynamicPropertiesStore().getLatestBlockHeaderTimestamp();
    byte[] fromKey = Bytes.concat(
        DelegatedResourceAccountIndexStore.getV2_FROM_PREFIX(), ownerAddress, receiverAddress);
    DelegatedResourceAccountIndexCapsule toIndexCapsule =
        new DelegatedResourceAccountIndexCapsule(ByteString.copyFrom(receiverAddress));
    toIndexCapsule.setTimestamp(now);
    repo.updateDelegatedResourceAccountIndex(fromKey, toIndexCapsule);

    byte[] toKey = Bytes.concat(
        DelegatedResourceAccountIndexStore.getV2_TO_PREFIX(), receiverAddress, ownerAddress);
    DelegatedResourceAccountIndexCapsule fromIndexCapsule =
        new DelegatedResourceAccountIndexCapsule(ByteString.copyFrom(ownerAddress));
    fromIndexCapsule.setTimestamp(now);
    repo.updateDelegatedResourceAccountIndex(toKey, fromIndexCapsule);

```

**File:** chainbase/src/main/java/org/tron/core/store/DelegatedResourceAccountIndexStore.java (L114-138)
```java
  public DelegatedResourceAccountIndexCapsule getV2Index(byte[] address) {
    return getWithPrefix(V2_FROM_PREFIX, V2_TO_PREFIX, address);
  }

  private DelegatedResourceAccountIndexCapsule getWithPrefix(byte[] fromPrefix, byte[] toPrefix, byte[] address) {
    DelegatedResourceAccountIndexCapsule tmpIndexCapsule =
        new DelegatedResourceAccountIndexCapsule(ByteString.copyFrom(address));

    byte[] key = Bytes.concat(fromPrefix, address);
    List<DelegatedResourceAccountIndexCapsule> tmpToList =
        new ArrayList<>(this.prefixQuery(key).values());
    tmpToList.sort(Comparator.comparing(DelegatedResourceAccountIndexCapsule::getTimestamp));
    List<ByteString> list = tmpToList.stream()
        .map(DelegatedResourceAccountIndexCapsule::getAccount).collect(Collectors.toList());
    tmpIndexCapsule.setAllToAccounts(list);

    key = Bytes.concat(toPrefix, address);
    List<DelegatedResourceAccountIndexCapsule> tmpFromList =
        new ArrayList<>(this.prefixQuery(key).values());
    tmpFromList.sort(Comparator.comparing(DelegatedResourceAccountIndexCapsule::getTimestamp));
    list = tmpFromList.stream().map(DelegatedResourceAccountIndexCapsule::getAccount).collect(
        Collectors.toList());
    tmpIndexCapsule.setAllFromAccounts(list);
    return tmpIndexCapsule;
  }
```

**File:** framework/src/main/java/org/tron/core/services/http/GetDelegatedResourceAccountIndexV2Servlet.java (L26-69)
```java
  protected void doGet(HttpServletRequest request, HttpServletResponse response) {
    try {
      boolean visible = Util.getVisible(request);
      String address = request.getParameter(VALUE_FIELD_NAME);
      if (visible) {
        address = Util.getHexAddress(address);
      }
      fillResponse(ByteString.copyFrom(ByteArray.fromHexString(address)), visible, response);
    } catch (Exception e) {
      Util.processError(e, response);
    }
  }

  protected void doPost(HttpServletRequest request, HttpServletResponse response) {
    try {
      PostParams params = PostParams.getPostParams(request);
      boolean visible = params.isVisible();
      String input = params.getParams();
      if (visible) {
        JSONObject jsonObject = JSONObject.parseObject(input);
        String value = jsonObject.getString(VALUE_FIELD_NAME);
        jsonObject.put(VALUE_FIELD_NAME, Util.getHexAddress(value));
        input = jsonObject.toJSONString();
      }

      BytesMessage.Builder build = BytesMessage.newBuilder();
      JsonFormat.merge(input, build, visible);

      fillResponse(build.getValue(), visible, response);
    } catch (Exception e) {
      Util.processError(e, response);
    }
  }

  private void fillResponse(ByteString address, boolean visible, HttpServletResponse response)
      throws IOException {
    DelegatedResourceAccountIndex reply =
        wallet.getDelegatedResourceAccountIndexV2(address);
    if (reply != null) {
      response.getWriter().println(JsonFormat.printToString(reply, visible));
    } else {
      response.getWriter().println("{}");
    }
  }
```
