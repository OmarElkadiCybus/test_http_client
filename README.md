# Cybus HTTP GET RTT Benchmark

Simple RTT measurement for Cybus HTTP `read` endpoints via MQTT request/response topics.

## Included files

- `commissioning/http-bench-baseline.cw.yml`
- `scripts/rtt_read_test.py`
- `requirements.txt`
- `docker-compose.yml`

## 1. Start local echo server

```bash
docker compose up -d
```

Server endpoints:

- `http://localhost:8080`
- `https://localhost:8443`

## 2. Deploy SCF

Use static commissioning file:

- `commissioning/http-bench-baseline.cw.yml`

Recommended defaults in that file:

- `targetHost: cybus-http-echo`
- `targetScheme: https`
- `targetPort: 8443`

## 3. Install dependency

```bash
python3 -m pip install -r requirements.txt
```

## 4. Run RTT benchmark

```bash
python3 scripts/rtt_read_test.py \
  --topic-root baseline/probe/01 \
  --reads 7 \
  --mqtt-username admin \
  --mqtt-password admin
```

Reconnect benchmark (5 reads, disconnect MQTT, reconnect, 5 reads):

```bash
python3 scripts/rtt_read_test.py \
  --topic-root baseline/probe/01 \
  --reads 5 \
  --reads-after-reconnect 5 \
  --mqtt-username admin \
  --mqtt-password admin
```

Run the same test for the other two services:

```bash
python3 scripts/rtt_read_test.py --topic-root agentoptions/probe/01 --reads 5 --reads-after-reconnect 5 --mqtt-username admin --mqtt-password admin
python3 scripts/rtt_read_test.py --topic-root tight/probe/01 --reads 5 --reads-after-reconnect 5 --mqtt-username admin --mqtt-password admin
```

## Optional: deploy SCF via API (no templating)

This deploys the static file and exits:

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

When you want to change connection params, edit `commissioning/http-bench-baseline.cw.yml` and deploy again.

## Output per run

Each run creates `logs/run_<timestamp>_<topic>/`:

- `rtt.csv`
- `metadata.json`
- `api_update.json` (only when API deploy is used)
