# Cybus HTTP GET RTT Benchmark

## 1. Description

This repository benchmarks end-to-end RTT for Cybus HTTP `read` endpoints triggered via MQTT request/response topics.

Three commissioning profiles are included:

| Profile | SCF file | MQTT root | Request topic | Response topic | Main difference |
| --- | --- | --- | --- | --- | --- |
| baseline | `commissioning/http-bench-baseline.cw.yml` | `baseline` | `baseline/probe/01/req` | `baseline/probe/01/res` | Standard HTTP connection, `probeInterval: 2000` |
| agentoptions | `commissioning/http-bench-agentOptions.scf.yml` | `agentoptions` | `agentoptions/probe/01/req` | `agentoptions/probe/01/res` | Adds HTTP `agentOptions` tuning (`maxSockets`, `maxFreeSockets`, `timeout`, `keepAliveMsecs`) |
| tight | `commissioning/http-bench-tight.scf.yml` | `tight` | `tight/probe/01/req` | `tight/probe/01/res` | Tight health probe interval (`probeInterval: 100`) |

Main test script:

- `scripts/rtt_read_test.py`
- Supports single-phase reads and reconnect-phase reads.
- Supports wait between reads (`--pause-http-ms` in ms).
- Can optionally deploy SCF via Connectware API (`--deploy-only`).
- Saves run artifacts to `logs/run_<timestamp>_<topic>/`.

## 2. Tests done and test results

Test method used:

1. Connect MQTT client.
2. Send 5 requests (`phase1`).
3. Disconnect MQTT and reconnect.
4. Send 5 more requests (`phase2`).
5. Compare `phase1` vs `phase2`.

Latest completed run set:

- `logs/run_20260213_205549_762084_baseline_probe_01/metadata.json`
- `logs/run_20260213_205549_762234_agentoptions_probe_01/metadata.json`
- `logs/run_20260213_205549_761477_tight_probe_01/metadata.json`

Results from this set:

| Profile | Phase1 payload avg | Phase2 payload avg | Timeout |
| --- | --- | --- | --- |
| baseline | 9.6 ms | 9.8 ms | 0/10 |
| agentoptions | 11.8 ms | 10.4 ms | 0/10 |
| tight | 12.6 ms | 9.8 ms | 0/10 |

Observed behavior:

- The first request after connect/reconnect is often the highest RTT in a phase.
- Keeping MQTT connected and adding pauses (tested up to 32 seconds) did not show an extra delay spike after the pause.
- This indicates first-request spikes are mostly reconnect/cold-path effects (MQTT reconnect path plus possible HTTP socket/TLS warm-up), not only HTTP processing.

## 3. How to use/test yourself

1. Start local echo server:

```bash
docker compose up -d
```

2. Install dependencies:

```bash
python3 -m pip install -r requirements.txt
```

3. Ensure services are deployed and enabled in Connectware:

- `httprttbenchmarkbaseline`
- `httprttbenchmarkagentoptions`
- `httprttbenchmarktight`

4. Run reconnect benchmark for each profile:

```bash
python3 scripts/rtt_read_test.py --topic-root baseline/probe/01 --reads 5 --reads-after-reconnect 5 --mqtt-username admin --mqtt-password admin
python3 scripts/rtt_read_test.py --topic-root agentoptions/probe/01 --reads 5 --reads-after-reconnect 5 --mqtt-username admin --mqtt-password admin
python3 scripts/rtt_read_test.py --topic-root tight/probe/01 --reads 5 --reads-after-reconnect 5 --mqtt-username admin --mqtt-password admin
```

5. Optional: add wait between requests:

```bash
python3 scripts/rtt_read_test.py --topic-root agentoptions/probe/01 --reads 5 --reads-after-reconnect 5 --pause-http-ms 1000 --mqtt-username admin --mqtt-password admin
```

6. Optional: deploy an SCF through API and exit (no RTT reads):

```bash
python3 scripts/rtt_read_test.py \
  --topic-root baseline/probe/01 \
  --service-id httprttbenchmarkbaseline \
  --api-base-url https://localhost \
  --api-key-file /home/omar/Cybus/sick/test_http_client/login_resp.json \
  --api-insecure \
  --commissioning-file commissioning/http-bench-baseline.cw.yml \
  --deploy-only
```

Output files per run:

- `rtt.csv`
- `metadata.json`
- `api_update.json` (only when API deploy is used)
