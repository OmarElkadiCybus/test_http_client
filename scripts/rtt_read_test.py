#!/usr/bin/env python3
import argparse
import csv
import json
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import paho.mqtt.client as mqtt


def now_ms() -> int:
    return int(time.time() * 1000)


def parse_timestamp_ms(value):
    if value is None:
        return None

    if isinstance(value, (int, float)):
        return int(value)

    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit():
            return int(stripped)

        try:
            iso_value = stripped.replace("Z", "+00:00")
            dt = datetime.fromisoformat(iso_value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
        except ValueError:
            return None

    return None


def summarize_rtts(values):
    if not values:
        return {
            "count": 0,
            "min": None,
            "avg": None,
            "max": None,
            "p50": None,
            "p95": None,
        }

    sorted_values = sorted(values)
    count = len(sorted_values)

    def percentile(p):
        if count == 1:
            return sorted_values[0]
        index = int(round((count - 1) * p))
        return sorted_values[index]

    return {
        "count": count,
        "min": sorted_values[0],
        "avg": round(sum(sorted_values) / count, 3),
        "max": sorted_values[-1],
        "p50": percentile(0.50),
        "p95": percentile(0.95),
    }


def load_json_object(path: str) -> dict:
    if not path:
        return {}

    content = Path(path).read_text(encoding="utf-8").strip()
    if not content.startswith("{"):
        return {}

    parsed = json.loads(content)
    if isinstance(parsed, dict):
        return parsed
    return {}


def resolve_mqtt_credentials(args):
    if args.mqtt_username is not None:
        return args.mqtt_username, args.mqtt_password, "args"

    if not args.mqtt_auth_file:
        return None, None, "none"

    try:
        parsed = load_json_object(args.mqtt_auth_file)
    except (FileNotFoundError, json.JSONDecodeError):
        return None, None, "none"

    username = None
    password = None

    for key in ("mqttUsername", "mqttUser", "username", "user"):
        value = parsed.get(key)
        if isinstance(value, str) and value.strip():
            username = value.strip()
            break

    for key in ("mqttPassword", "password", "pass"):
        value = parsed.get(key)
        if isinstance(value, str):
            password = value
            break

    if username is None:
        return None, None, "none"

    return username, password, f"file:{args.mqtt_auth_file}"


class ResponseStore:
    def __init__(self):
        self._cv = threading.Condition()
        self._by_id = {}

    def put(self, message_id, topic, payload, recv_ms):
        if not message_id:
            return
        with self._cv:
            self._by_id[message_id] = {
                "topic": topic,
                "payload": payload,
                "recv_ms": recv_ms,
            }
            self._cv.notify_all()

    def pop_wait(self, message_id, timeout_s):
        deadline = time.monotonic() + timeout_s
        with self._cv:
            while True:
                if message_id in self._by_id:
                    return self._by_id.pop(message_id)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cv.wait(remaining)


def build_parser():
    parser = argparse.ArgumentParser(
        description="Trigger GET/read requests via Cybus MQTT topic and record RTT per response."
    )

    parser.add_argument("--mqtt-host", default="localhost")
    parser.add_argument("--mqtt-port", type=int, default=1883)
    parser.add_argument("--mqtt-username", default=None)
    parser.add_argument("--mqtt-password", default=None)
    parser.add_argument(
        "--mqtt-auth-file",
        default="",
        help="Optional JSON file containing MQTT credentials, e.g. mqttUsername/mqttPassword.",
    )

    parser.add_argument("--topic-root", default="baseline/probe/01")
    parser.add_argument("--reads", type=int, default=7)
    parser.add_argument(
        "--reads-after-reconnect",
        type=int,
        default=0,
        help="If > 0, disconnect MQTT after first batch and run this many reads in a second batch.",
    )
    parser.add_argument("--request-timeout", type=float, default=10.0)
    parser.add_argument(
        "--pause-http-ms",
        type=int,
        default=0,
        help="Pause between HTTP probe requests in milliseconds (default: 0).",
    )
    parser.add_argument(
        "--wib",
        type=int,
        default=None,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--pause-ms",
        type=int,
        default=None,
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--qos", type=int, default=0)
    parser.add_argument("--csv", default="")
    parser.add_argument(
        "--send-query",
        action="store_true",
        help="Send query params with each req (no HTTP body), useful for server-side echo debugging.",
    )

    return parser


def run_single_read(client, responses, req_topic, request_timeout, qos, send_query, iteration_label):
    request_id = str(uuid.uuid4())
    trigger_ms = now_ms()

    req_payload = {"id": request_id}
    if send_query:
        req_payload["query"] = {
            "requestId": request_id,
            "triggerTsMs": trigger_ms,
            "iteration": iteration_label,
        }

    client.publish(req_topic, json.dumps(req_payload), qos=qos)
    response = responses.pop_wait(request_id, request_timeout)
    if response is None:
        return {
            "request_id": request_id,
            "trigger_ms": trigger_ms,
            "response_timestamp_ms": "",
            "response_received_ms": "",
            "rtt_payload_ms": "",
            "rtt_receive_ms": "",
            "status": "timeout",
        }

    payload = response["payload"]
    recv_ms = response["recv_ms"]
    response_ts_ms = parse_timestamp_ms(payload.get("timestamp"))
    rtt_payload_ms = response_ts_ms - trigger_ms if response_ts_ms is not None else None
    rtt_receive_ms = recv_ms - trigger_ms

    return {
        "request_id": request_id,
        "trigger_ms": trigger_ms,
        "response_timestamp_ms": response_ts_ms if response_ts_ms is not None else "",
        "response_received_ms": recv_ms,
        "rtt_payload_ms": rtt_payload_ms if rtt_payload_ms is not None else "",
        "rtt_receive_ms": rtt_receive_ms,
        "status": "ok",
    }


def connect_mqtt_client(args, res_topic):
    connected = threading.Event()
    responses = ResponseStore()
    mqtt_username, mqtt_password, mqtt_auth_source = resolve_mqtt_credentials(args)
    if args.mqtt_auth_file and mqtt_auth_source == "none":
        print(
            "warning: --mqtt-auth-file did not provide MQTT credentials "
            "(expected keys like mqttUsername/mqttPassword)",
            file=sys.stderr,
        )

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    if mqtt_username is not None:
        client.username_pw_set(mqtt_username, mqtt_password)

    def on_connect(_client, _userdata, _flags, reason_code, _properties=None):
        if reason_code != 0:
            message = f"connect failed reason_code={reason_code}"
            if str(reason_code).lower() == "not authorized":
                message += " (set --mqtt-username/--mqtt-password)"
            print(message, file=sys.stderr)
            return
        _client.subscribe(res_topic, qos=args.qos)
        connected.set()
        print(f"connected mqtt={args.mqtt_host}:{args.mqtt_port} subscribed={res_topic}")

    def on_message(_client, _userdata, msg):
        recv_ms = now_ms()
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except Exception:
            print(f"ignored non-json message topic={msg.topic}")
            return

        message_id = str(payload.get("id", ""))
        responses.put(message_id, msg.topic, payload, recv_ms)

    client.on_connect = on_connect
    client.on_message = on_message

    client.connect(args.mqtt_host, args.mqtt_port, keepalive=30)
    client.loop_start()

    if not connected.wait(timeout=10):
        client.loop_stop()
        client.disconnect()
        raise RuntimeError("failed to connect/subscribe within 10s")

    return client, responses


def main():
    args = build_parser().parse_args()
    wait_in_between_ms = args.pause_http_ms
    if args.wib is not None:
        wait_in_between_ms = args.wib
        print("warning: --wib is deprecated; use --pause-http-ms", file=sys.stderr)
    if args.pause_ms is not None:
        wait_in_between_ms = args.pause_ms
        print("warning: --pause-ms is deprecated; use --pause-http-ms", file=sys.stderr)

    if args.reads_after_reconnect < 0:
        print("--reads-after-reconnect must be >= 0", file=sys.stderr)
        return 2

    req_topic = f"{args.topic_root}/req"
    res_topic = f"{args.topic_root}/res"
    safe_topic = args.topic_root.replace("/", "_")
    run_stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    run_dir = Path("logs") / f"run_{run_stamp}_{safe_topic}"
    run_dir.mkdir(parents=True, exist_ok=True)

    results = []
    phase_specs = [("phase1", args.reads)]
    if args.reads_after_reconnect > 0:
        phase_specs.append(("phase2", args.reads_after_reconnect))

    iteration = 1
    for phase_index, (phase_name, phase_reads) in enumerate(phase_specs, start=1):
        if phase_reads <= 0:
            continue

        try:
            client, responses = connect_mqtt_client(args, res_topic)
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 2

        try:
            for phase_read in range(1, phase_reads + 1):
                row = run_single_read(
                    client,
                    responses,
                    req_topic,
                    args.request_timeout,
                    args.qos,
                    args.send_query,
                    f"{phase_name}-{phase_read}",
                )
                row["iteration"] = iteration
                row["phase"] = phase_name
                row["phase_read"] = phase_read
                results.append(row)

                if row["status"] == "ok":
                    print(
                        f"{phase_name}:{phase_read:02d} ok request_id={row['request_id']} "
                        f"rtt_payload_ms={row['rtt_payload_ms']} rtt_receive_ms={row['rtt_receive_ms']}"
                    )
                else:
                    print(f"{phase_name}:{phase_read:02d} timeout request_id={row['request_id']}")

                iteration += 1
                time.sleep(wait_in_between_ms / 1000)
        finally:
            client.loop_stop()
            client.disconnect()

        if phase_index < len(phase_specs):
            print("mqtt disconnected after phase1; reconnecting for phase2...")

    payload_rtts = [r["rtt_payload_ms"] for r in results if isinstance(r["rtt_payload_ms"], int)]
    receive_rtts = [r["rtt_receive_ms"] for r in results if isinstance(r["rtt_receive_ms"], int)]

    phase_stats = {}
    for phase_name, _phase_reads in phase_specs:
        phase_rows = [r for r in results if r.get("phase") == phase_name]
        phase_payload_rtts = [r["rtt_payload_ms"] for r in phase_rows if isinstance(r["rtt_payload_ms"], int)]
        phase_receive_rtts = [r["rtt_receive_ms"] for r in phase_rows if isinstance(r["rtt_receive_ms"], int)]
        phase_stats[phase_name] = {
            "readsOk": len(phase_payload_rtts),
            "readsTimeout": len([r for r in phase_rows if r["status"] != "ok"]),
            "payload": summarize_rtts(phase_payload_rtts),
            "receive": summarize_rtts(phase_receive_rtts),
        }

    print("\nsummary")
    print(f"reads={len(results)} ok={len(payload_rtts)} timeout={len([r for r in results if r['status'] != 'ok'])}")
    if payload_rtts:
        print(
            "payload_rtt_ms "
            f"min={min(payload_rtts)} avg={sum(payload_rtts)/len(payload_rtts):.2f} max={max(payload_rtts)}"
        )
    if receive_rtts:
        print(
            "receive_rtt_ms "
            f"min={min(receive_rtts)} avg={sum(receive_rtts)/len(receive_rtts):.2f} max={max(receive_rtts)}"
        )
    for phase_name in [name for name, _count in phase_specs]:
        phase_payload = phase_stats[phase_name]["payload"]
        phase_receive = phase_stats[phase_name]["receive"]
        if phase_payload["count"] > 0:
            print(
                f"{phase_name}_payload_rtt_ms "
                f"min={phase_payload['min']} avg={phase_payload['avg']:.2f} max={phase_payload['max']}"
            )
        if phase_receive["count"] > 0:
            print(
                f"{phase_name}_receive_rtt_ms "
                f"min={phase_receive['min']} avg={phase_receive['avg']:.2f} max={phase_receive['max']}"
            )
    if "phase1" in phase_stats and "phase2" in phase_stats:
        p1 = phase_stats["phase1"]["payload"]
        p2 = phase_stats["phase2"]["payload"]
        if p1["count"] > 0 and p2["count"] > 0:
            print(f"delta_payload_avg_ms_phase2_minus_phase1={round(p2['avg'] - p1['avg'], 3)}")

    csv_name = Path(args.csv).name if args.csv else "rtt.csv"
    output = run_dir / csv_name

    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "phase",
                "phase_read",
                "iteration",
                "request_id",
                "trigger_ms",
                "response_timestamp_ms",
                "response_received_ms",
                "rtt_payload_ms",
                "rtt_receive_ms",
                "status",
            ],
        )
        writer.writeheader()
        writer.writerows(results)

    metadata = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "benchmark": {
            "topicRoot": args.topic_root,
            "phaseReads": {
                "phase1": args.reads,
                "phase2": args.reads_after_reconnect,
            },
            "readsPlanned": args.reads + args.reads_after_reconnect,
            "readsOk": len(payload_rtts),
            "readsTimeout": len([r for r in results if r["status"] != "ok"]),
            "requestTimeoutSec": args.request_timeout,
            "waitBetweenReadsMs": wait_in_between_ms,
        },
        "rttMs": {
            "payload": summarize_rtts(payload_rtts),
            "receive": summarize_rtts(receive_rtts),
        },
        "phaseRttMs": phase_stats,
        "artifacts": {
            "csvPath": str(output),
        },
    }

    with (run_dir / "metadata.json").open("w", encoding="utf-8") as handle:
        json.dump(metadata, handle, indent=2)

    print(f"saved_run_dir={run_dir}")
    print(f"saved_csv={output}")
    print(f"saved_metadata={run_dir / 'metadata.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
