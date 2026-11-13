"""
Simple MQTT Broker — pure Python asyncio, MQTT 3.1.1 only.
Handles: CONNECT, CONNACK, PUBLISH, SUBSCRIBE, SUBACK, UNSUBSCRIBE, DISCONNECT, PING.

Jalankan: python -m app.mqtt_broker
Listens on 0.0.0.0:1883
"""
import asyncio
import logging
import struct
import uuid
from collections import defaultdict

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [MQTT-Broker] %(levelname)s: %(message)s",
)
logger = logging.getLogger("mqtt_broker")

MQTT_PORT = 1883


class MQTTClient:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader = reader
        self.writer = writer
        self.client_id = ""
        self.keepalive = 60
        self.subscriptions: set[str] = set()
        self.peername = writer.get_extra_info("peername")

    async def _read_exact(self, n: int) -> bytes:
        data = b""
        while len(data) < n:
            chunk = await self.reader.read(n - len(data))
            if not chunk:
                raise EOFError("Connection closed")
            data += chunk
        return data

    async def _read_varint(self) -> int:
        """Read MQTT variable-length integer (1-4 bytes)."""
        multiplier = 1
        value = 0
        for _ in range(4):
            byte = (await self._read_exact(1))[0]
            value += (byte & 0x7F) * multiplier
            multiplier *= 128
            if (byte & 0x80) == 0:
                break
        return value

    async def _read_string(self) -> str:
        data = await self._read_exact(2)
        length = struct.unpack(">H", data)[0]
        if length == 0:
            return ""
        return (await self._read_exact(length)).decode("utf-8", errors="replace")

    async def _write_packet(self, packet: bytes):
        self.writer.write(packet)
        await self.writer.drain()

    async def _send_connack(self, return_code: int, mqtt_version: int = 4):
        """MQTT v3.1.1: 0x20, 0x02, 0x00, rc
           MQTT v5.0:    0x20, 0x02, 0x00, rc (same format, flags byte is always 0)
        """
        # For v5, we technically should send properties but we keep it minimal for dev
        await self._write_packet(bytes([0x20, 0x02, 0x00, return_code]))

    async def _send_suback(self, packet_id: int, qos: int):
        # Fixed header: 0x62 (SUBACK), remaining=3, packet_id+1 qos byte
        await self._write_packet(bytes([0x62, 0x03]) + struct.pack(">H", packet_id) + bytes([qos]))

    async def handle(self):
        try:
            # ── CONNECT ──────────────────────────────────────────────────────────
            try:
                header = await self.reader.read(1)
                if not header or (header[0] & 0xF0) != 0x10:
                    return
                remaining = await self._read_varint()
            except (EOFError, Exception):
                return

            try:
                proto_name = await self._read_string()
                proto_level = (await self._read_exact(1))[0]
                connect_flags = (await self._read_exact(1))[0]
                keepalive = struct.unpack(">H", await self._read_exact(2))[0]
                client_id = await self._read_string()
            except EOFError:
                return

            self.client_id = client_id or f"anon-{uuid.uuid4().hex[:8]}"
            self.keepalive = keepalive

            logger.info(f"CONNECT: client={self.client_id} proto={proto_name} v{proto_level} from {self.peername}")
            await self._send_connack(0)

            self.broker.add_client(self)

            # ── Message loop ──────────────────────────────────────────────────────
            while True:
                try:
                    first = await self.reader.read(1)
                    if not first:
                        break
                    packet_type = first[0] & 0xF0
                    remaining = await self._read_varint()

                    if packet_type == 0x30:   # PUBLISH
                        await self._handle_publish(remaining)
                    elif packet_type == 0x82:  # SUBSCRIBE
                        await self._handle_subscribe(remaining)
                    elif packet_type == 0xA2:  # UNSUBSCRIBE
                        await self._handle_unsubscribe(remaining)
                    elif packet_type == 0xE0:  # DISCONNECT
                        logger.info(f"DISCONNECT: {self.client_id}")
                        break
                    elif packet_type == 0xC0:  # PINGREQ
                        await self._write_packet(bytes([0xD0, 0x00]))
                    elif packet_type == 0x40:  # PUBACK
                        await self._read_exact(2)
                    elif packet_type == 0x50:  # PUBREC
                        pkt = await self._read_exact(2)
                        await self._write_packet(bytes([0x60, 0x02]) + pkt)
                    elif packet_type == 0x70:  # PUBREL
                        pkt = await self._read_exact(2)
                        await self._write_packet(bytes([0x7C, 0x02]) + pkt)
                    elif packet_type == 0x10:  # CONNECT (another one)
                        logger.warning(f"Extra CONNECT received, ignoring")
                        await self._read_exact(remaining) if remaining > 0 else None
                    else:
                        logger.warning(f"Unknown packet 0x{first[0]:02X} rem={remaining}")
                        if remaining > 0:
                            await self._read_exact(remaining)

                except EOFError:
                    break
                except Exception as e:
                    logger.error(f"Error handling packet: {e}")
                    break

        except Exception as e:
            logger.error(f"Client error: {e}")
        finally:
            self.broker.remove_client(self)
            self.writer.close()
            await self.writer.wait_closed()

    async def _handle_publish(self, remaining: int):
        # Read topic
        topic_len_bytes = await self._read_exact(2)
        topic_len = struct.unpack(">H", topic_len_bytes)[0]
        topic = (await self._read_exact(topic_len)).decode(errors="replace")
        remaining -= 2 + topic_len

        qos = (await self.reader.read(1))[0]
        remaining -= 1
        qos_level = (qos >> 1) & 0x03

        if qos_level > 0:
            pkt_id = struct.unpack(">H", await self._read_exact(2))[0]
            remaining -= 2
        else:
            pkt_id = None

        payload = await self._read_exact(remaining)

        # Route to subscribers
        await self.broker.route_message(self, topic, payload)

        # Send QoS response
        if qos_level == 1:
            await self._write_packet(bytes([0x40, 0x02]) + struct.pack(">H", pkt_id or 0))
        elif qos_level == 2:
            await self._write_packet(bytes([0x50, 0x02]) + struct.pack(">H", pkt_id or 0))

    async def _handle_subscribe(self, remaining: int):
        pkt_id = struct.unpack(">H", await self._read_exact(2))[0]
        remaining -= 2

        topics = []
        while remaining > 0:
            topic_len = struct.unpack(">H", await self._read_exact(2))[0]
            remaining -= 2
            topic = (await self._read_exact(topic_len)).decode(errors="replace")
            remaining -= topic_len
            qos = (await self._read_exact(1))[0] & 0x03
            remaining -= 1
            topics.append((topic, qos))
            self.subscriptions.add(topic)

        logger.info(f"SUBSCRIBE: {self.client_id} → {topics}")
        # Send SUBACK
        for topic, qos in topics:
            await self._write_packet(bytes([0x62, 0x03]) + struct.pack(">H", pkt_id) + bytes([qos]))

    async def _handle_unsubscribe(self, remaining: int):
        pkt_id = struct.unpack(">H", await self._read_exact(2))[0]
        remaining -= 2
        while remaining > 0:
            topic_len = struct.unpack(">H", await self._read_exact(2))[0]
            remaining -= 2
            topic = (await self._read_exact(topic_len)).decode(errors="replace")
            remaining -= topic_len
            self.subscriptions.discard(topic)
        await self._write_packet(bytes([0x70, 0x02]) + struct.pack(">H", pkt_id))

    async def deliver(self, topic: str, payload: bytes, qos: int = 0, pkt_id: int = 0):
        """Send a PUBLISH to this client."""
        topic_bytes = topic.encode()
        var_header = struct.pack(">H", len(topic_bytes)) + topic_bytes
        if qos > 0:
            var_header += struct.pack(">H", pkt_id)

        full = var_header + payload
        rl = len(full)
        enc = []
        while True:
            b = rl % 128
            rl //= 128
            if rl > 0:
                b |= 0x80
            enc.append(b)
            if rl == 0:
                break
        header = bytes([0x30 | (qos << 1)]) + bytes(enc)
        try:
            await self._write_packet(header + full)
        except Exception as e:
            logger.error(f"Deliver failed to {self.client_id}: {e}")


class MQTTBroker:
    def __init__(self):
        self.clients: dict[str, MQTTClient] = {}
        self._pkt_counter = 0

    def add_client(self, client: MQTTClient):
        self.clients[client.client_id] = client
        logger.info(f"Client connected: {client.client_id} (total: {len(self.clients)})")

    def remove_client(self, client: MQTTClient):
        if client.client_id in self.clients:
            del self.clients[client.client_id]
            logger.info(f"Client removed: {client.client_id} (total: {len(self.clients)})")

    def _match(self, sub: str, topic: str) -> bool:
        sp = sub.split("/")
        tp = topic.split("/")
        si = 0
        for part in sp:
            if part == "#":
                return True
            if si >= len(tp):
                return False
            if part == "+":
                si += 1
            elif part == tp[si]:
                si += 1
            else:
                return False
        return si == len(tp) or si == len(sp)

    async def route_message(self, sender: MQTTClient, topic: str, payload: bytes):
        self._pkt_counter += 1
        pkt_id = self._pkt_counter & 0xFFFF
        for client in list(self.clients.values()):
            for sub in client.subscriptions:
                if self._match(sub, topic):
                    await client.deliver(topic, payload, qos=0, pkt_id=pkt_id)


async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, broker: MQTTBroker):
    client = MQTTClient(reader, writer)
    client.broker = broker
    await client.handle()


async def main():
    broker = MQTTBroker()
    server = await asyncio.start_server(
        lambda r, w: handle_client(r, w, broker),
        "0.0.0.0", MQTT_PORT,
    )
    addr = server.sockets[0].getsockname()
    logger.info(f"✅ MQTT Broker started on {addr[0]}:{addr[1]} (MQTT 3.1.1)")
    logger.info("   Topics: students/+/logs, students/+/location, dashboard/#")
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
