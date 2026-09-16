## Title
Unbounded, unrotated VM trace files let any contract-calling transaction exhaust node disk space when `vm.vmTrace` is enabled - (File: `actuator/src/main/java/org/tron/core/vm/VMUtils.java`)

### Summary
CVE-2014-3672 describes an unprivileged guest that fills the host disk because output written by the guest is never rotated or capped. The analogous pattern in java-tron is `VMUtils.saveProgramTraceFile`, which writes one raw, uncapped `vm_trace/<txHash>.json` file per transaction whenever `vm.vmTrace` is turned on, completely outside the logback rolling/size-capped appenders that govern every other log stream in the node.

### Finding Description
When `VMConfig.vmTrace()` is true, `VMActuator.finalization()` builds a full execution trace and persists it to disk after every transaction: [1](#0-0) 

The write path is `VMUtils.saveProgramTraceFile` → `createProgramTraceFile` → `writeStringToFile`: [2](#0-1) 

Unlike the node's application logs (`./logs/tron.log`, `./logs/db/db.log`, etc.), which are all wrapped in `SizeAndTimeBasedRollingPolicy` with `maxFileSize`, `maxHistory` and `totalSizeCap`: [3](#0-2) 

the `vm_trace/<txHash>.json` files are created directly with `File.createNewFile()`/`FileOutputStream` and are never deleted, rotated, deduplicated, or size-limited. Every unique transaction hash produces a new, permanent file. Trace content includes full opcode list, stack, and (unless folded above 320 bytes) full memory dump per step, so a contract using a large memory footprint and many steps produces a large trace file per call, and content is optionally still written uncompressed (`VMConfig.vmTraceCompressed()` is opt-in, default off) — see the compression branch: [4](#0-3) 

Because a new file is keyed by transaction hash, an attacker can trivially generate an unbounded number of distinct hashes (varying nonce/data) and drive `vm_trace/` growth without any deduplication or cap, unlike the capped `logs/` directories.

### Impact Explanation
On any node/validator that has `vm.vmTrace = true` (a documented, supported runtime option, not a CLI-only/toolkit-only debug switch — see `reference.conf`'s `vmTrace` setting and the `--debug` CLI flag family), an unprivileged transaction sender can force unbounded disk growth purely by broadcasting smart-contract-calling transactions, each generating a persistent trace file that is never cleaned up. Sustained abuse fills the disk, which halts RocksDB/LevelDB writes and can crash or freeze the node (denial of service / consensus participation halt for a witness/SR running with tracing enabled for auditing).

### Likelihood Explanation
Requires the node operator to have enabled `vm.vmTrace` (default `false`), so it is not exploitable against the default configuration of every node. However, tracing is a supported production feature used by SRs/exchanges/full-node operators for auditing/debugging, and once enabled, exploitation requires nothing more than normal, unprivileged contract-call transactions — no special permissions, no malicious peer, and no node-internal access. There is no rate limit or cap tying trace-file volume to energy/bandwidth consumed.

### Recommendation
Apply the same bounding used for the logback appenders to `vm_trace` output: enforce a directory-level size cap with LRU/oldest-first eviction, rotate/compress by default (`vmTraceCompressed` should not be opt-in), or bound trace retention (e.g., only keep last N or by size budget) inside `VMUtils.createProgramTraceFile`/`saveProgramTraceFile`. Consider gating trace persistence per unique caller/energy spent, or writing to a bounded ring buffer instead of one permanent file per tx hash.

### Proof of Concept
1. Start a java-tron node with `vm { vmTrace = true }` in `config.conf` (a supported production setting).
2. From any unprivileged account, repeatedly broadcast `TriggerSmartContract` transactions against a contract that uses a large amount of EVM memory (to maximize the "MEMORY" dump in `Program.fullTrace()`/trace serialization) with varying transaction data/nonce so each produces a unique tx hash. [5](#0-4) 
3. Observe `./vm_trace/` on the node growing by one new, uncapped file per transaction, with no rotation or cleanup, eventually exhausting disk space. [2](#0-1)

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/VMActuator.java (L306-318)
```java
    if (VMConfig.vmTrace() && program != null) {
      String traceContent = program.getTrace()
          .result(result.getHReturn())
          .error(result.getException())
          .toString();

      if (VMConfig.vmTraceCompressed()) {
        traceContent = VMUtils.zipAndEncode(traceContent);
      }

      String txHash = Hex.toHexString(rootInternalTx.getHash());
      VMUtils.saveProgramTraceFile(txHash, traceContent);
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/VMUtils.java (L52-98)
```java
  private static File createProgramTraceFile(String txHash) {
    File result = null;

    if (VMConfig.vmTrace()) {

      File file = new File(new File("./", "vm_trace"), txHash + ".json");

      if (file.exists()) {
        if (file.isFile() && file.canWrite()) {
          result = file;
        }
      } else {
        try {
          file.getParentFile().mkdirs();
          if (!file.createNewFile()) {
            logger.error("failed to create file {}", file.getPath());
          }
          result = file;
        } catch (IOException e) {
          // ignored
        }
      }
    }

    return result;
  }

  private static void writeStringToFile(File file, String data) {
    OutputStream out = null;
    try {
      out = new FileOutputStream(file);
      if (data != null) {
        out.write(data.getBytes("UTF-8"));
      }
    } catch (Exception e) {
      logger.error(format("Cannot write to file '%s': ", file.getAbsolutePath()), e);
    } finally {
      closeQuietly(out);
    }
  }

  public static void saveProgramTraceFile(String txHash, String content) {
    File file = createProgramTraceFile(txHash);
    if (file != null) {
      writeStringToFile(file, content);
    }
  }
```

**File:** framework/src/main/resources/logback.xml (L27-45)
```text
  <appender class="ch.qos.logback.core.rolling.RollingFileAppender"
    name="FILE">
    <file>./logs/tron.log</file>
    <rollingPolicy
      class="ch.qos.logback.core.rolling.SizeAndTimeBasedRollingPolicy">
      <!-- rollover daily -->
      <fileNamePattern>./logs/tron-%d{yyyy-MM-dd}.%i.log.gz</fileNamePattern>
      <!-- each file should be at most 500MB, keep 7 days worth of history, but at most 50GB -->
      <maxFileSize>500MB</maxFileSize>
      <maxHistory>7</maxHistory>
      <totalSizeCap>50GB</totalSizeCap>
    </rollingPolicy>
    <encoder>
      <pattern>%d{HH:mm:ss.SSS} %-5level [%t] [%c{1}]\(%F:%L\) %m%n</pattern>
    </encoder>
    <filter class="ch.qos.logback.classic.filter.ThresholdFilter">
      <level>TRACE</level>
    </filter>
  </appender>
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L1515-1554)
```java
  public void fullTrace() {
    if (logger.isTraceEnabled() || listener != null) {

      StringBuilder stackData = new StringBuilder();
      for (int i = 0; i < stack.size(); ++i) {
        stackData.append(" ").append(stack.get(i));
        if (i < stack.size() - 1) {
          stackData.append("\n");
        }
      }

      if (stackData.length() > 0) {
        stackData.insert(0, "\n");
      }

      StringBuilder memoryData = new StringBuilder();
      StringBuilder oneLine = new StringBuilder();
      if (memory.size() > 320) {
        memoryData.append("... Memory Folded.... ")
            .append("(")
            .append(memory.size())
            .append(") bytes");
      } else {
        for (int i = 0; i < memory.size(); ++i) {

          byte value = memory.readByte(i);
          oneLine.append(ByteUtil.oneByteToHexString(value)).append(" ");

          if ((i + 1) % 16 == 0) {
            String tmp = format("[%4s]-[%4s]", Integer.toString(i - 15, 16),
                Integer.toString(i, 16)).replace(" ", "0");
            memoryData.append("").append(tmp).append(" ");
            memoryData.append(oneLine);
            if (i < memory.size()) {
              memoryData.append("\n");
            }
            oneLine.setLength(0);
          }
        }
      }
```
