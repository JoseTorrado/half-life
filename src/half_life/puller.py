# src/half_life/puller.py
import aiohttp, asyncio, gzip, json, io, os, logging
from datetime import datetime, timedelta, timezone

from confluent_kafka import SerializingProducer
from confluent_kafka.schema_registry import SchemaRegistryClient
from confluent_kafka.schema_registry.avro import AvroSerializer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

KAFKA_BROKER = os.getenv("KAFKA_BROKER", "localhost:19092")
SCHEMA_REGISTRY_URL = os.getenv("SCHEMA_REGISTRY_URL", "http://localhost:8081")
SCHEMA_PATH = os.getenv("SCHEMA_PATH", "../avro_schemas")  # relative to /app/src

def load_schema_str(filename: str) -> str:
    with open(f"{SCHEMA_PATH}/{filename}", "r") as f:
        return f.read()

PR_OPEN_SCHEMA_STR  = load_schema_str("pr_open.avsc")
PR_MERGE_SCHEMA_STR = load_schema_str("pr_merge.avsc")

# --- schema registry + serializers ---
sr_client = SchemaRegistryClient({"url": SCHEMA_REGISTRY_URL})

pr_open_serializer = AvroSerializer(
    sr_client,
    PR_OPEN_SCHEMA_STR,
    conf={"auto.register.schemas": True},
)
pr_merge_serializer = AvroSerializer(
    sr_client,
    PR_MERGE_SCHEMA_STR,
    conf={"auto.register.schemas": True},
)

def iso_to_epoch_millis(dtstr: str) -> int:
    """
    GH Archive times are ISO8601 UTC like '2025-07-18T02:00:00Z'.
    We normalize any trailing 'Z' to '+00:00' then parse.
    """
    if dtstr.endswith("Z"):
        dtstr = dtstr[:-1] + "+00:00"
    dt = datetime.fromisoformat(dtstr)
    return int(dt.timestamp() * 1000)

def delivery_cb(err, msg):
    if err is not None:
        logging.error("Delivery failed: %s", err)
    else:
        logging.debug(
            "Delivered %s [%d] @ %d",
            msg.topic(), msg.partition(), msg.offset()
        )

def build_producer(value_serializer):
    return SerializingProducer({
        "bootstrap.servers": KAFKA_BROKER,
        "key.serializer": lambda v, ctx: v.encode("utf-8") if v else None,
        "value.serializer": value_serializer,
        # optional; can help debug:
        "enable.idempotence": False,
        "socket.keepalive.enable": True,
    })

pr_open_producer  = build_producer(pr_open_serializer)
pr_merge_producer = build_producer(pr_merge_serializer)

async def fetch_and_publish(url: str):
    logging.info("Fetching %s", url)
    opened_count = merged_count = 0

    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                logging.error("Fetch failed %s status=%s", url, resp.status)
                return

            raw = await resp.read()
            with gzip.GzipFile(fileobj=io.BytesIO(raw)) as gz:
                for line in gz:
                    event = json.loads(line)
                    if event["type"] != "PullRequestEvent":
                        continue
                    action = event["payload"]["action"]
                    repo   = event["repo"]
                    key = f"{repo['id']}|{event['payload']['number']}"

                    if action == "opened":
                        record = {
                            "event_id":   event["id"],
                            "repo_id":    repo["id"],
                            "repo_name":  repo["name"],
                            "pr_number":  event["payload"]["number"],
                            "opened_at":  iso_to_epoch_millis(event["created_at"]),
                        }
                        pr_open_producer.produce(
                            topic="pr_open",
                            key=key,
                            value=record,
                            on_delivery=delivery_cb,
                        )
                        opened_count += 1

                    elif (action == "closed" and
                          event["payload"].get("pull_request", {}).get("merged", False)):
                        record = {
                            "event_id":   event["id"],
                            "repo_id":    repo["id"],
                            "repo_name":  repo["name"],
                            "pr_number":  event["payload"]["number"],
                            "merged_at":  iso_to_epoch_millis(event["created_at"]),
                        }
                        pr_merge_producer.produce(
                            topic="pr_merge",
                            key=key,
                            value=record,
                            on_delivery=delivery_cb,
                        )
                        merged_count += 1

    pr_open_producer.flush()
    pr_merge_producer.flush()
    logging.info("Published %s pr_open, %s pr_merge.", opened_count, merged_count)

if __name__ == "__main__":
    # default: most recent complete GH Archive hour
    ts = datetime.now(timezone.utc) - timedelta(hours=1)
    url = f"https://data.gharchive.org/{ts:%Y-%m-%d-%H}.json.gz"  # NOTE: %H not %-H for zero-pad (portable)
    asyncio.run(fetch_and_publish(url))
    logging.info("Schema registry URL: %s", SCHEMA_REGISTRY_URL)
