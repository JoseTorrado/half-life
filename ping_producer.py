# ping_producer.py
from confluent_kafka import SerializingProducer
from confluent_kafka.schema_registry import SchemaRegistryClient
from confluent_kafka.schema_registry.avro import AvroSerializer

SCHEMA_REGISTRY_URL = "http://localhost:8081"   # host path if running on host
BOOTSTRAP = "localhost:19092"                   # outside listener

SCHEMA_STR = """
{
  "type": "record",
  "name": "Ping",
  "fields": [{"name": "msg", "type": "string"}]
}
"""

sr_client = SchemaRegistryClient({"url": SCHEMA_REGISTRY_URL})
value_serializer = AvroSerializer(
    sr_client,
    SCHEMA_STR,
    conf={"auto.register.schemas": True},
)

producer = SerializingProducer({
    "bootstrap.servers": BOOTSTRAP,
    "key.serializer": lambda v, ctx: v.encode() if v else None,
    # optional but good practice:
    "value.serializer": value_serializer,
})

producer.produce(
    topic="avro_ping",
    key="ping-1",
    value={"msg": "hello-avro"},
)
producer.flush()
print("✅ sent.")
