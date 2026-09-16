### Title
Unauthenticated DoS via JSON-RPC Batch Amplification of Expensive TVM/EnergyEstimation Resolvers - (File: framework/src/main/java/org/tron/core/services/jsonrpc/JsonRpcServlet.java)

### Summary
`JsonRpcServlet` implements JSON-RPC 2.0 batch requests: a single HTTP POST may contain an array of up to `jsonRpcMaxBatchSize` (default 100) independent sub-requests, each dispatched via `rpcServer.handleRequest` in a sequential loop with no per-item deduplication or per-item accounting of computational cost. [1](#0-0)  The outer `RateLimiterServlet.service()` and `GlobalRateLimiter` apply their QPS/IP/permit checks exactly once per HTTP request — they have no visibility into how many logical calls are packed inside a batch array. [2](#0-1)  This is structurally the same amplification pattern as the Directus GraphQL alias bug: a single rate-limited request can trigger many independent invocations of an expensive resolver.

The expensive resolver here is `TronJsonRpcImpl.estimateGas`/`eth_call`, which routes into `Wallet.estimateEnergy` / `Wallet.triggerConstantContract` → `callConstantContract` → TVM execution via `VMActuator`. When `estimateEnergy` is enabled, a single call performs a binary search that re-executes the full contract multiple times (`cleanContextAndTriggerConstantContract` calls repeated in a `while` loop until `low + TRX_PRECISION < high` converges), each iteration running the TVM up to `maxEnergyLimitForConstant` (100,000,000 energy) and up to the constant-call CPU deadline (`constantCallTimeoutMs` or the shared `maxCpuTimeOfOneTx`). [3](#0-2)  Multiplying this per-call amplification by the batch amplification (up to 100 aliased sub-requests in one HTTP POST) allows an unauthenticated client to force hundreds to thousands of full, CPU-bound TVM executions from a single request that consumes only one rate-limiter token.

### Finding Description
- `JsonRpcServlet.doPost` distinguishes single vs. batch requests; for batch requests it iterates every element and independently calls `rpcServer.handleRequest`, with only an aggregate response-size ceiling (`jsonRpcMaxResponseSize`) and a total-item ceiling (`jsonRpcMaxBatchSize`, default 100) — no execution-time or per-method-cost budget exists for the batch as a whole. [4](#0-3) 
- Each batch element can independently target `eth_call` or `eth_estimateGas`. `estimateGas` in `TronJsonRpcImpl` calls either `wallet.estimateEnergy` (binary-search over fee limit, each iteration a full VM run) or `callTriggerConstantContract` (single VM run at `maxEnergyLimitForConstant`). [5](#0-4) 
- `Wallet.estimateEnergy` performs an unbounded-looking (bounded only by `estimateEnergyMaxRetry` on timeout, not on iteration count) sequence of `cleanContextAndTriggerConstantContract` invocations, each a fresh TVM execution against attacker-controlled bytecode/data, each entitled to run up to the constant-call CPU deadline. [6](#0-5) 
- `VMActuator.call()` sets `energyLimit = maxEnergyLimit` for constant calls and computes a CPU deadline via `calculateCpuLimitInUs`, giving each single sub-request execution a large energy/time budget (up to `maxEnergyLimitForConstant = 100000000` and the configured `constantCallTimeoutMs`/`maxCpuTimeOfOneTx`). [7](#0-6) 
- The outer HTTP-level rate limiter (`RateLimiterServlet`/`GlobalRateLimiter`) is only consulted once, in `service()`, before `doPost` is ever invoked; it cannot see or throttle the number of sub-requests inside the batch array. [2](#0-1) 

This mirrors the GraphQL alias-amplification bug class described in the report: authorization/rate-limiting is enforced once per outer request, while the actual expensive work is invoked N times per request through a legitimate protocol feature (aliases in GraphQL, batch array items in JSON-RPC) that the gatekeeping layer does not account for.

### Impact Explanation
An anonymous client sending a single JSON-RPC HTTP POST to the (opt-in) `jsonrpc.httpFullNodeEnable` endpoint can pack up to 100 sub-requests, each invoking `eth_estimateGas`/`eth_call` against attacker-supplied bytecode. Since `estimateEnergy` internally re-executes the contract many times via binary search, and each sub-request execution is entitled to a large energy budget and CPU deadline, a single HTTP request can serialize hundreds of full TVM executions on the servicing thread. Because `JsonRpcServlet` processes batch items sequentially on one request-handling thread, and the JSON-RPC servlet's own thread pool is shared across all API clients, a modest number of such requests can occupy all HTTP worker threads for extended periods, causing service degradation/denial of service for all legitimate `wallet`/`jsonrpc` API users on that node (unauthenticated `AV:N/AC:L/PR:N/UI:N` DoS, matching the advisory's CVSS profile). This does not directly cause loss of funds or consensus impact, but does render the API unable to serve legitimate requests, which the validation rules explicitly count as acceptable impact ("an API the node can no longer serve").

### Likelihood Explanation
High. The batch feature and `estimateEnergy`/`eth_call` paths are standard, publicly documented JSON-RPC functionality; no authentication or special privilege is required, and `jsonRpcMaxBatchSize` defaults to 100, which is already sufficient for meaningful amplification. The `constantCallTimeoutMs`/`maxCpuTimeOfOneTx` bound per-call time but do not bound the aggregate number of calls per HTTP request; the outer rate limiter is trivially bypassed by amplification exactly as in the GraphQL alias scenario.

### Recommendation
Introduce a per-HTTP-request cost budget for JSON-RPC batches: either (a) cap the aggregate CPU/energy time consumed across all sub-requests in a batch (e.g., track cumulative constant-call execution time and abort/exceed-limit remaining items once a request-wide deadline is hit), or (b) explicitly rate-limit/cost-weight expensive methods (`eth_call`, `eth_estimateGas`, and any resolver invoking `triggerConstantContract`/`estimateEnergy`) per batch, independent of the flat `jsonRpcMaxBatchSize` item count, so that N aliased/batched invocations of an expensive method are billed proportionally to the rate limiter rather than as a single request. Consider deduplicating identical sub-requests (same method+params) within a batch, following the same mitigation pattern the Directus fix used for GraphQL aliases.

### Proof of Concept
1. Enable JSON-RPC on a full node (`jsonrpc.httpFullNodeEnable = true`, default port 8545) and `vm.estimateEnergy = true`, `vm.supportConstant = true` (both are documented, commonly enabled configuration options).
2. Deploy or reference an existing contract whose fallback/queried function is computationally heavy but bounded (e.g., a loop that consumes significant energy before completing) at a known address.
3. Send a single POST to `/jsonrpc` with a JSON array of `jsonRpcMaxBatchSize` (up to 100) `eth_estimateGas` (or `eth_call`) sub-requests, each targeting that contract:
```json
[
  {"jsonrpc":"2.0","id":1,"method":"eth_estimateGas","params":[{"to":"0x...","data":"0x..."}]},
  {"jsonrpc":"2.0","id":2,"method":"eth_estimateGas","params":[{"to":"0x...","data":"0x..."}]},
  ... (up to 100 entries)
]
```
4. `JsonRpcServlet.handleBatch` sequentially invokes each sub-request; each `eth_estimateGas` call triggers `Wallet.estimateEnergy`'s binary-search loop of `cleanContextAndTriggerConstantContract` calls, each running the full TVM up to the constant-call CPU deadline. The cumulative work performed by one HTTP request (counted as a single unit against the rate limiter) is on the order of `100 × (binary-search iterations)` full VM executions, exhausting the servlet thread and CPU resources and degrading availability for all other API clients on the node.

### Citations

**File:** framework/src/main/java/org/tron/core/services/jsonrpc/JsonRpcServlet.java (L131-266)
```java
    boolean isBatch = rootNode.isArray();
    if (isBatch && rootNode.isEmpty()) {
      writeJsonRpcError(resp, JsonRpcError.INVALID_REQUEST, "Invalid Request", null, false);
      return;
    }
    int batchSize = parameter.getJsonRpcMaxBatchSize();
    if (isBatch && batchSize > 0 && rootNode.size() > batchSize) {
      writeJsonRpcError(resp, JsonRpcError.EXCEED_LIMIT,
          "Batch size " + rootNode.size() + " exceeds the limit of " + batchSize, null, true);
      return;
    }

    int maxResponseSize = parameter.getJsonRpcMaxResponseSize();
    if (isBatch) {
      handleBatch(resp, rootNode, maxResponseSize);
    } else {
      handleSingle(req, resp, rootNode, body, maxResponseSize);
    }
  }

  private void handleSingle(HttpServletRequest req, HttpServletResponse resp,
      JsonNode rootNode, byte[] body, int maxResponseSize) throws IOException {
    CachedBodyRequestWrapper cachedReq = new CachedBodyRequestWrapper(req, body);
    BufferedResponseWrapper bufferedResp = new BufferedResponseWrapper(
        resp, maxResponseSize);

    try {
      rpcServer.handle(cachedReq, bufferedResp);
    } catch (RuntimeException e) {
      logger.error("RPC execution failed", e);
      writeJsonRpcError(resp, JsonRpcError.INTERNAL_ERROR, "Internal error",
          rootNode.get("id"), false);
      return;
    }

    bufferedResp.commitToResponse();
    if (bufferedResp.isOverflow()) {
      writeJsonRpcError(resp, JsonRpcError.RESPONSE_TOO_LARGE,
          "Response exceeds the limit of " + maxResponseSize + " bytes",
          rootNode.get("id"), false);
    }
  }

  private void handleBatch(HttpServletResponse resp, JsonNode rootNode, int maxResponseSize)
      throws IOException {

    ArrayNode batchResult = MAPPER.createArrayNode();
    int accumulatedSize = 2; // "[]"
    boolean overflow = false;

    for (int i = 0; i < rootNode.size(); i++) {
      JsonNode subRequest = rootNode.get(i);

      if (overflow) {
        if (!subRequest.isObject()) {
          batchResult.add(buildErrorNode(JsonRpcError.INVALID_REQUEST, "Invalid Request", null));
        } else if (subRequest.has("id")) {
          // Notifications (no "id") do not get a response even on overflow.
          batchResult.add(buildErrorNode(JsonRpcError.RESPONSE_TOO_LARGE,
              "Response exceeds the limit of " + maxResponseSize + " bytes",
              subRequest.get("id")));
        }
        continue;
      }

      if (!subRequest.isObject()) {
        ObjectNode errNode = buildErrorNode(JsonRpcError.INVALID_REQUEST, "Invalid Request", null);
        byte[] errBytes = MAPPER.writeValueAsBytes(errNode);
        int addition = errBytes.length + (!batchResult.isEmpty() ? 1 : 0);
        if (maxResponseSize > 0 && accumulatedSize + addition > maxResponseSize) {
          overflow = true;
        } else {
          accumulatedSize += addition;
        }
        batchResult.add(errNode);
        continue;
      }

      byte[] subBody;
      try {
        subBody = MAPPER.writeValueAsBytes(subRequest);
      } catch (JsonProcessingException e) {
        writeJsonRpcError(resp, JsonRpcError.INTERNAL_ERROR, "Internal error", null, true);
        return;
      }

      ByteArrayOutputStream subOutput = new ByteArrayOutputStream();
      try {
        rpcServer.handleRequest(new ByteArrayInputStream(subBody), subOutput);
      } catch (RuntimeException e) {
        logger.error("RPC execution failed for batch sub-request {}", i, e);
        writeJsonRpcError(resp, JsonRpcError.INTERNAL_ERROR, "Internal error", null, true);
        return;
      }

      byte[] responseBytes = subOutput.toByteArray();
      if (responseBytes.length == 0) {
        continue; // notification — no response
      }

      // comma(,) separator between array elements
      int addition = responseBytes.length + (!batchResult.isEmpty() ? 1 : 0);
      if (maxResponseSize > 0 && accumulatedSize + addition > maxResponseSize) {
        overflow = true;
        batchResult.add(buildErrorNode(JsonRpcError.RESPONSE_TOO_LARGE,
            "Response exceeds the limit of " + maxResponseSize + " bytes",
            subRequest.get("id")));
        continue;
      }
      accumulatedSize += addition;

      JsonNode responseNode;
      try {
        responseNode = MAPPER.readTree(responseBytes);
      } catch (IOException e) {
        writeJsonRpcError(resp, JsonRpcError.INTERNAL_ERROR, "Internal error", null, true);
        return;
      }
      batchResult.add(responseNode);
    }

    // JSON-RPC 2.0 §6: MUST NOT return an empty Array when there are no response objects.
    if (batchResult.isEmpty()) {
      resp.setContentType("application/json-rpc");
      resp.setStatus(HttpServletResponse.SC_OK);
      resp.setContentLength(0);
      return;
    }

    byte[] finalBytes = MAPPER.writeValueAsBytes(batchResult);
    resp.setContentType("application/json-rpc");
    resp.setStatus(HttpServletResponse.SC_OK);
    resp.setContentLength(finalBytes.length);
    resp.getOutputStream().write(finalBytes);
    resp.getOutputStream().flush();
  }
```

**File:** framework/src/main/java/org/tron/core/services/http/RateLimiterServlet.java (L103-136)
```java
  @Override
  protected void service(HttpServletRequest req, HttpServletResponse resp)
      throws ServletException, IOException {

    RuntimeData runtimeData = new RuntimeData(req);
    IRateLimiter rateLimiter = container.get(KEY_PREFIX_HTTP, getClass().getSimpleName());

    // Check per-endpoint first to avoid consuming global IP/QPS quota for requests
    // that would be rejected by the per-endpoint limiter anyway. acquirePermit()
    // chooses blocking or non-blocking semantics based on rate.limiter.apiNonBlocking.
    boolean perEndpointAcquired = rateLimiter == null || rateLimiter.acquirePermit(runtimeData);
    boolean acquireResource = perEndpointAcquired && GlobalRateLimiter.acquirePermit(runtimeData);

    String contextPath = req.getContextPath();
    String url = Strings.isNullOrEmpty(req.getServletPath())
        ? MetricLabels.UNDEFINED : contextPath + req.getServletPath();
    // int64_as_string is honored only on GET requests (URL query). POST is intentionally
    // unsupported because reading the body here would consume request.getReader() and
    // break downstream servlets that read it themselves.
    if ("GET".equalsIgnoreCase(req.getMethod())) {
      JsonFormat.setInt64AsString(Util.getInt64AsString(req));
    }
    try {
      resp.setContentType("application/json; charset=utf-8");

      if (acquireResource) {
        Histogram.Timer requestTimer = Metrics.histogramStartTimer(
            MetricKeys.Histogram.HTTP_SERVICE_LATENCY, url);
        super.service(req, resp);
        Metrics.histogramObserve(requestTimer);
      } else {
        resp.getWriter()
            .println(Util.printErrorMsg(new IllegalAccessException("lack of computing resources")));
      }
```

**File:** framework/src/main/java/org/tron/core/Wallet.java (L2999-3087)
```java
    int retry = Args.getInstance().estimateEnergyMaxRetry;

    DynamicPropertiesStore dps = chainBaseManager.getDynamicPropertiesStore();
    long high = dps.getMaxFeeLimit();

    Transaction transaction;

    while (true) {
      try {
        transaction = cleanContextAndTriggerConstantContract(
            triggerSmartContract, txCap, txExtBuilder, txRetBuilder, high);
        break;
      } catch (Program.OutOfTimeException e) {
        retry--;
        if (retry < 0) {
          throw e;
        }
      }
    }

    // If failed, return directly.
    if (transaction.getRet(0).getRet().equals(code.FAILED)) {
      txRetBuilder.setCode(response_code.CONTRACT_EXE_ERROR);
      estimateBuilder.setResult(txRetBuilder);
      return transaction;
    }

    long low = dps.getEnergyFee() * txExtBuilder.getEnergyUsed();

    long twoTimes = low * 2;
    if (twoTimes < high) {
      while (true) {
        try {
          transaction = cleanContextAndTriggerConstantContract(
              triggerSmartContract, txCap, txExtBuilder, txRetBuilder, twoTimes);

          if (transaction.getRet(0).getRet().equals(code.FAILED)) {
            low = twoTimes;
          } else {
            high = twoTimes;
          }

          break;
        } catch (Program.OutOfTimeException e) {
          retry--;
          if (retry < 0) {
            throw e;
          }
        }
      }
    }

    while (low + TRX_PRECISION < high) {
      long mid = (low + high) / 2;

      while (true) {
        try {
          transaction = cleanContextAndTriggerConstantContract(
              triggerSmartContract, txCap, txExtBuilder, txRetBuilder, mid);
          break;
        } catch (Program.OutOfTimeException e) {
          retry--;
          if (retry < 0) {
            throw e;
          }
        }
      }

      if (transaction.getRet(0).getRet().equals(code.FAILED)) {
        low = mid;
      } else {
        high = mid;
      }
    }

    // Retry the binary search result
    transaction = cleanContextAndTriggerConstantContract(
        triggerSmartContract, txCap, txExtBuilder, txRetBuilder, high);
    // Setting estimating result
    estimateBuilder.setResult(txRetBuilder);
    if (transaction.getRet(0).getRet().equals(code.SUCESS)) {
      txRetBuilder.setResult(true);
      txRetBuilder.setCode(response_code.SUCCESS);
      estimateBuilder.setEnergyRequired((long) ceil((double) high / dps.getEnergyFee(),
          dps.disableJavaLangMath()));
    }

    return transaction;
  }
```

**File:** framework/src/main/java/org/tron/core/services/jsonrpc/TronJsonRpcImpl.java (L673-731)
```java
  @Override
  public String estimateGas(CallArguments args) throws JsonRpcInvalidRequestException,
      JsonRpcInvalidParamsException, JsonRpcInternalException {
    byte[] ownerAddress = addressCompatibleToByteArray(args.getFrom());

    ContractType contractType = args.getContractType(wallet);
    if (contractType == ContractType.TransferContract) {
      buildTransferContractTransaction(ownerAddress, new BuildArguments(args));
      return "0x0";
    }

    boolean supportEstimateEnergy = CommonParameter.getInstance().isEstimateEnergy();

    TransactionExtention.Builder trxExtBuilder = TransactionExtention.newBuilder();
    Return.Builder retBuilder = Return.newBuilder();
    EstimateEnergyMessage.Builder estimateBuilder
        = EstimateEnergyMessage.newBuilder();

    try {
      byte[] contractAddress;

      if (contractType == ContractType.TriggerSmartContract) {
        contractAddress = addressCompatibleToByteArray(args.getTo());
      } else {
        contractAddress = new byte[0];
      }

      if (supportEstimateEnergy) {
        estimateEnergy(ownerAddress,
            contractAddress,
            args.parseValue(),
            ByteArray.fromHexString(args.resolveData()),
            trxExtBuilder,
            retBuilder,
            estimateBuilder);
      } else {
        callTriggerConstantContract(ownerAddress,
            contractAddress,
            args.parseValue(),
            ByteArray.fromHexString(args.resolveData()),
            trxExtBuilder,
            retBuilder);
      }

    } catch (ContractValidateException e) {
      String errString = "invalid contract";
      if (e.getMessage() != null) {
        errString = e.getMessage();
      }

      throw new JsonRpcInvalidRequestException(errString);
    } catch (Exception e) {
      String errString = JSON_ERROR;
      if (e.getMessage() != null) {
        errString = e.getMessage().replaceAll("[\"]", "'");
      }

      throw new JsonRpcInternalException(errString);
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/VMActuator.java (L536-552)
```java
      if (isConstantCall) {
        energyLimit = maxEnergyLimit;
      } else {
        AccountCapsule creator = rootRepository
            .getAccount(deployedContract.getInstance().getOriginAddress().toByteArray());
        energyLimit = getTotalEnergyLimit(creator, caller, contract, feeLimit, callValue);
      }

      long thisTxCPULimitInUs = calculateCpuLimitInUs(isConstantCall,
          rootRepository.getDynamicPropertiesStore().getMaxCpuTimeOfOneTx(),
          getCpuLimitInUsRatio(), CommonParameter.getInstance().getConstantCallTimeoutMs());
      long vmStartInUs = System.nanoTime() / VMConstant.ONE_THOUSAND;
      long vmShouldEndInUs = vmStartInUs + thisTxCPULimitInUs;
      ProgramInvoke programInvoke = ProgramInvokeFactory
          .createProgramInvoke(TrxType.TRX_CONTRACT_CALL_TYPE, executorType, trx,
              tokenValue, tokenId, blockCap.getInstance(), rootRepository, vmStartInUs,
              vmShouldEndInUs, energyLimit);
```
